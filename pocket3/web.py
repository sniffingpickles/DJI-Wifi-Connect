"""Web UI for DJI Pocket 3 Control Suite.

Flask-based web interface with:
- Live MJPEG video preview (H.264 decoded via ffmpeg)
- PTZ gimbal control (keyboard + on-screen buttons)
- Camera commands (photo, record, mode switch)
- Real-time status updates (battery, gimbal, video stats)
- WiFi/BLE connection management with progress display
"""

import asyncio
import logging
import queue
import subprocess
import threading
import time

from flask import Flask, Response, jsonify, request, render_template_string

logger = logging.getLogger("pocket3.web")


class MJPEGStreamer:
    """Decode H.264 stream to JPEG frames via ffmpeg for MJPEG web streaming."""

    def __init__(self):
        self._ffmpeg: subprocess.Popen | None = None
        self._latest_frame: bytes | None = None
        self._frame_lock = threading.Lock()
        self._running = False
        self._reader_thread: threading.Thread | None = None
        self._h264_queue: queue.Queue = queue.Queue(maxsize=200)

    def start(self):
        """Start ffmpeg decoder process."""
        if self._running:
            return
        self._running = True
        cmd = [
            "ffmpeg",
            "-f", "h264",
            "-framerate", "60",
            "-i", "pipe:0",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-q:v", "5",
            "-r", "30",
            "-an",
            "pipe:1",
        ]
        self._ffmpeg = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._reader_thread = threading.Thread(
            target=self._read_frames, daemon=True, name="mjpeg-reader")
        self._reader_thread.start()
        writer = threading.Thread(
            target=self._write_h264, daemon=True, name="mjpeg-writer")
        writer.start()
        logger.info("MJPEG streamer started")

    def on_video_data(self, data: bytes):
        """Feed raw H.264 data (called from UDP rx loop)."""
        try:
            self._h264_queue.put_nowait(data)
        except queue.Full:
            pass

    def _write_h264(self):
        """Drain H.264 queue and write to ffmpeg stdin."""
        while self._running and self._ffmpeg:
            try:
                data = self._h264_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._ffmpeg.stdin.write(data)
                self._ffmpeg.stdin.flush()
            except (BrokenPipeError, OSError):
                break

    def _read_frames(self):
        """Read JPEG frames from ffmpeg stdout by finding SOI/EOI markers."""
        buf = bytearray()
        while self._running and self._ffmpeg:
            try:
                chunk = self._ffmpeg.stdout.read(4096)
            except Exception:
                break
            if not chunk:
                break
            buf.extend(chunk)

            while True:
                # Find JPEG SOI marker (FFD8)
                soi = buf.find(b'\xff\xd8')
                if soi < 0:
                    buf.clear()
                    break
                # Find JPEG EOI marker (FFD9) after SOI
                eoi = buf.find(b'\xff\xd9', soi + 2)
                if eoi < 0:
                    # Trim everything before SOI
                    if soi > 0:
                        del buf[:soi]
                    break
                # Extract complete JPEG
                jpeg = bytes(buf[soi:eoi + 2])
                del buf[:eoi + 2]
                with self._frame_lock:
                    self._latest_frame = jpeg

    def get_frame(self) -> bytes | None:
        """Get the latest decoded JPEG frame."""
        with self._frame_lock:
            return self._latest_frame

    def stop(self):
        """Stop the streamer."""
        self._running = False
        if self._ffmpeg:
            try:
                self._ffmpeg.stdin.close()
            except Exception:
                pass
            try:
                self._ffmpeg.terminate()
                self._ffmpeg.wait(timeout=3)
            except Exception:
                pass
            self._ffmpeg = None
        logger.info("MJPEG streamer stopped")


class ConnectionManager:
    """Manages the full BLE → WiFi → UDP → Video connection lifecycle."""

    STEPS = [
        ("ble_scan", "Scanning for camera via Bluetooth..."),
        ("ble_pair", "Pairing with camera..."),
        ("wifi_wait", "Waiting for camera WiFi AP (~20s)..."),
        ("wifi_join", "Joining camera WiFi network..."),
        ("wifi_check", "Verifying camera is reachable..."),
        ("udp_handshake", "UDP handshake with camera..."),
        ("video_start", "Starting video stream..."),
        ("done", "Connected!"),
    ]

    def __init__(self, ssid: str, password: str, camera_ip: str = "192.168.2.1",
                 ble_timeout: float = 15.0):
        self.ssid = ssid
        self.password = password
        self.camera_ip = camera_ip
        self.ble_timeout = ble_timeout

        # Connection state
        self.client = None
        self.gimbal = None
        self.camera = None
        self.video = None
        self.streamer = None

        # Progress tracking
        self._step = ""
        self._step_idx = -1
        self._status = "disconnected"  # disconnected, connecting, connected, error
        self._message = "Not connected"
        self._error = ""
        self._start_time = 0.0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def progress(self) -> dict:
        with self._lock:
            elapsed = time.time() - self._start_time if self._start_time else 0
            return {
                "status": self._status,
                "step": self._step,
                "step_idx": self._step_idx,
                "total_steps": len(self.STEPS),
                "message": self._message,
                "error": self._error,
                "elapsed_s": round(elapsed, 1),
            }

    def _set_step(self, idx: int, msg: str = ""):
        with self._lock:
            self._step_idx = idx
            if idx < len(self.STEPS):
                self._step = self.STEPS[idx][0]
                self._message = msg or self.STEPS[idx][1]
            else:
                self._step = "done"
                self._message = msg or "Connected!"

    def connect(self, skip_ble: bool = False, skip_wifi: bool = False):
        """Start connection in background thread."""
        with self._lock:
            if self._status == "connecting":
                return
            if self._status == "connected":
                return
            self._status = "connecting"
            self._error = ""
            self._start_time = time.time()

        self._thread = threading.Thread(
            target=self._connect_thread,
            args=(skip_ble, skip_wifi),
            daemon=True, name="connect")
        self._thread.start()

    def _connect_thread(self, skip_ble: bool, skip_wifi: bool):
        try:
            step_offset = 0

            # --- BLE ---
            if not skip_ble:
                self._set_step(0)  # ble_scan
                from .ble import DjiPocket3BLE
                from .main import _ble_activate_once

                ble = DjiPocket3BLE()
                try:
                    self._set_step(0, "Scanning for DJI Pocket 3 via Bluetooth...")

                    async def do_ble():
                        return await _ble_activate_once(
                            ble, self.ssid, self.password, self.ble_timeout)

                    # _ble_activate_once handles scan + pair + wifi join + check
                    # We need to update steps as it progresses
                    # Since it's async, run it with progress updates
                    loop = asyncio.new_event_loop()

                    async def ble_with_progress():
                        devices = await ble.scan(timeout=self.ble_timeout)
                        pocket3_devices = [(d, m) for d, m in devices
                                           if m == "OsmoPocket3"]
                        if not pocket3_devices:
                            if devices:
                                pocket3_devices = devices
                            else:
                                raise RuntimeError("No DJI BLE devices found")

                        device, model = pocket3_devices[0]
                        self._set_step(1, f"Pairing with {device.name}...")
                        await ble.connect(device, model)
                        await ble.activate_and_pair()

                        self._set_step(2, "Waiting for camera WiFi AP to activate (~20s)...")
                        await asyncio.sleep(20)

                        if not skip_wifi:
                            self._set_step(3, f"Joining WiFi: {self.ssid}...")
                            from .wifi import connect_wifi, wait_for_camera
                            wifi_ok = await loop.run_in_executor(
                                None, lambda: connect_wifi(
                                    self.ssid, self.password, timeout=30.0))
                            if not wifi_ok:
                                raise RuntimeError("WiFi join failed")

                            self._set_step(4, "Checking camera reachability...")
                            cam_ok = await loop.run_in_executor(
                                None, lambda: wait_for_camera(timeout=10.0))
                            if not cam_ok:
                                raise RuntimeError("Camera not reachable")

                    try:
                        loop.run_until_complete(ble_with_progress())
                    finally:
                        try:
                            loop.run_until_complete(ble.disconnect())
                        except Exception:
                            pass
                        loop.close()
                    step_offset = 5
                except Exception:
                    try:
                        loop = asyncio.new_event_loop()
                        loop.run_until_complete(ble.disconnect())
                        loop.close()
                    except Exception:
                        pass
                    raise

            elif not skip_wifi:
                # Skip BLE, just do WiFi
                self._set_step(3, f"Joining WiFi: {self.ssid}...")
                from .wifi import connect_wifi, wait_for_camera
                if not connect_wifi(self.ssid, self.password, timeout=30.0):
                    raise RuntimeError("WiFi join failed")
                self._set_step(4, "Checking camera reachability...")
                if not wait_for_camera(timeout=10.0):
                    raise RuntimeError("Camera not reachable")
                step_offset = 5
            else:
                step_offset = 5

            # --- UDP Handshake ---
            self._set_step(5, "UDP handshake with camera...")
            from .udp_protocol import DjiUdpClient
            from .video import VideoReceiver
            from .gimbal import GimbalController
            from .camera import CameraController

            client = DjiUdpClient(camera_ip=self.camera_ip)

            if not client.connect():
                raise RuntimeError("UDP handshake failed")

            # --- Setup components ---
            video = VideoReceiver()
            client.set_video_callback(video.on_video_data)

            streamer = MJPEGStreamer()
            client.add_video_callback(streamer.on_video_data)
            streamer.start()

            gimbal = GimbalController(client)
            camera = CameraController(client)

            client.start()
            gimbal.start()
            camera.start()

            self._set_step(6, "Starting video stream...")
            time.sleep(0.5)
            client.start_video()

            # Store references
            self.client = client
            self.gimbal = gimbal
            self.camera = camera
            self.video = video
            self.streamer = streamer

            self._set_step(7, "Connected!")
            with self._lock:
                self._status = "connected"

            logger.info("Connection complete!")

        except Exception as e:
            logger.error(f"Connection failed: {e}")
            with self._lock:
                self._status = "error"
                self._error = str(e)
                self._message = f"Failed: {e}"

    def disconnect(self):
        """Disconnect from camera."""
        if self.camera:
            self.camera.stop()
            self.camera = None
        if self.gimbal:
            self.gimbal.stop()
            self.gimbal = None
        if self.streamer:
            self.streamer.stop()
            self.streamer = None
        if self.video:
            self.video.stop()
            self.video = None
        if self.client:
            self.client.stop()
            self.client = None
        with self._lock:
            self._status = "disconnected"
            self._message = "Disconnected"
            self._step = ""
            self._step_idx = -1

    def shutdown(self):
        """Clean shutdown."""
        self.disconnect()


def create_app(connection_manager: ConnectionManager | None = None,
               client=None, gimbal=None, camera=None, video=None, streamer=None):
    """Create Flask app with all routes."""
    app = Flask(__name__)
    app.config["conn"] = connection_manager
    # Direct references (for non-GUI CLI mode, unused in GUI mode)
    app.config["client"] = client
    app.config["gimbal"] = gimbal
    app.config["camera"] = camera
    app.config["video"] = video
    app.config["streamer"] = streamer

    @app.route("/")
    def index():
        return render_template_string(HTML_TEMPLATE)

    def _conn():
        """Get ConnectionManager or None."""
        return app.config.get("conn")

    def _get(attr):
        """Get component from ConnectionManager or direct config."""
        cm = _conn()
        if cm:
            return getattr(cm, attr, None)
        return app.config.get(attr)

    @app.route("/api/connect", methods=["POST"])
    def connect():
        cm = _conn()
        if not cm:
            return jsonify({"error": "No connection manager"}), 500
        data = request.get_json(force=True) if request.data else {}
        skip_ble = data.get("skip_ble", False)
        skip_wifi = data.get("skip_wifi", False)
        cm.connect(skip_ble=skip_ble, skip_wifi=skip_wifi)
        return jsonify({"ok": True})

    @app.route("/api/disconnect", methods=["POST"])
    def disconnect():
        cm = _conn()
        if cm:
            cm.disconnect()
        return jsonify({"ok": True})

    @app.route("/api/connect/status")
    def connect_status():
        cm = _conn()
        if cm:
            return jsonify(cm.progress)
        return jsonify({"status": "disconnected"})

    @app.route("/api/video_feed")
    def video_feed():
        def generate():
            while True:
                s = _get("streamer")
                if s:
                    frame = s.get_frame()
                    if frame:
                        yield (b"--frame\r\n"
                               b"Content-Type: image/jpeg\r\n\r\n" +
                               frame + b"\r\n")
                time.sleep(0.033)  # ~30fps
        return Response(generate(),
                        mimetype="multipart/x-mixed-replace; boundary=frame")

    @app.route("/api/status")
    def status():
        c = _get("client")
        g = _get("gimbal")
        cam = _get("camera")
        v = _get("video")
        result = {"connected": False}

        if c:
            s = c.stats
            result["connected"] = c._running
            result["rx_packets"] = s["rx_packets"]
            result["tx_packets"] = s["tx_packets"]
            result["video_frames"] = s["video_frames"]
            result["duml_packets"] = s["duml_packets"]

        if v:
            result["video_bytes"] = v.byte_count
            result["video_mb"] = round(v.byte_count / 1024 / 1024, 1)

        if g:
            gs = g.state
            result["gimbal"] = {
                "yaw": round(gs.yaw, 1),
                "pitch": round(gs.pitch, 1),
                "roll": round(gs.roll, 1),
            }

        if cam:
            cs = cam.status
            result["camera"] = {
                "mode": cs.mode_name,
                "recording": cs.recording,
                "battery": cs.battery_percent,
                "sd_inserted": cs.sd_inserted,
                "recording_time": cs.recording_time_s,
            }

        return jsonify(result)

    @app.route("/api/gimbal", methods=["POST"])
    def gimbal_control():
        g = _get("gimbal")
        if not g:
            return jsonify({"error": "Not connected"}), 503

        data = request.get_json(force=True)
        action = data.get("action", "")

        if action == "set_speed":
            yaw = float(data.get("yaw", 0))
            pitch = float(data.get("pitch", 0))
            g.set_speed(yaw=yaw, pitch=pitch)
        elif action == "stop":
            g.stop_movement()
        elif action == "nudge":
            yaw = float(data.get("yaw", 0))
            pitch = float(data.get("pitch", 0))
            duration = float(data.get("duration", 0.3))
            g.nudge(yaw=yaw, pitch=pitch, duration=duration)
        else:
            return jsonify({"error": f"Unknown action: {action}"}), 400

        return jsonify({"ok": True})

    @app.route("/api/camera/<action>", methods=["POST"])
    def camera_action(action):
        cam = _get("camera")
        if not cam:
            return jsonify({"error": "Not connected"}), 503

        if action == "photo":
            cam.take_photo()
        elif action == "record_start":
            cam.start_recording()
        elif action == "record_stop":
            cam.stop_recording()
        elif action == "record_toggle":
            cam.toggle_recording()
        elif action == "mode":
            data = request.get_json(force=True)
            mode = int(data.get("mode", 1))
            cam.set_mode(mode)
        elif action == "raw":
            data = request.get_json(force=True)
            cam.send_raw(
                cmd_set=int(data.get("cmd_set", 2)),
                cmd_id=int(data.get("cmd_id", 0)),
                payload=bytes.fromhex(data.get("payload", "")),
                receiver_type=int(data.get("receiver_type", 1)),
                receiver_id=int(data.get("receiver_id", 0)),
            )
        else:
            return jsonify({"error": f"Unknown action: {action}"}), 400

        return jsonify({"ok": True})

    return app


# ---------------------------------------------------------------------------
# HTML / CSS / JS (inline template for single-file deployment)
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>DJI Pocket 3 Control</title>
<style>
  :root {
    --bg: #0f1117;
    --surface: #1a1d27;
    --surface2: #242834;
    --border: #2e3340;
    --text: #e4e6ed;
    --text2: #8b8fa3;
    --accent: #4c8dff;
    --accent-hover: #6ba1ff;
    --red: #ff4d6a;
    --green: #34d399;
    --orange: #f59e0b;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    height: 100vh;
    overflow: hidden;
  }
  .app {
    display: grid;
    grid-template-rows: 48px 1fr auto;
    height: 100vh;
  }
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 20px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
  }
  .header h1 { font-size: 15px; font-weight: 600; letter-spacing: 0.5px; }
  .status-dot {
    width: 8px; height: 8px; border-radius: 50%;
    display: inline-block; margin-right: 8px;
  }
  .status-dot.connected { background: var(--green); }
  .status-dot.connecting { background: var(--orange); animation: pulse 1s infinite; }
  .status-dot.disconnected { background: var(--red); }
  .status-dot.error { background: var(--red); }
  @keyframes pulse { 50% { opacity: 0.4; } }
  .header-right {
    display: flex; align-items: center; gap: 16px;
    font-size: 13px; color: var(--text2);
  }
  .main {
    display: grid;
    grid-template-columns: 1fr 280px;
    overflow: hidden;
  }
  /* Video */
  .video-container {
    display: flex; align-items: center; justify-content: center;
    background: #000; position: relative;
  }
  .video-container img { max-width: 100%; max-height: 100%; object-fit: contain; }
  .video-overlay {
    position: absolute; top: 12px; left: 16px;
    font-size: 12px; color: rgba(255,255,255,0.6); pointer-events: none;
  }
  .video-overlay .rec { color: var(--red); font-weight: 700; animation: blink 1s infinite; }
  @keyframes blink { 50% { opacity: 0; } }

  /* Connect screen (shown in video area when disconnected) */
  .connect-screen {
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; gap: 24px; padding: 40px;
    text-align: center; max-width: 480px;
  }
  .connect-screen h2 { font-size: 20px; font-weight: 600; }
  .connect-screen .subtitle { color: var(--text2); font-size: 13px; line-height: 1.6; }
  .connect-btn {
    padding: 14px 48px; font-size: 15px; font-weight: 600;
    background: var(--accent); color: #fff; border: none; border-radius: 8px;
    cursor: pointer; transition: background 0.15s;
  }
  .connect-btn:hover { background: var(--accent-hover); }
  .connect-btn:disabled { background: var(--surface2); color: var(--text2); cursor: default; }
  .connect-options {
    display: flex; gap: 16px; font-size: 12px; color: var(--text2);
  }
  .connect-options label { cursor: pointer; display: flex; align-items: center; gap: 4px; }
  .connect-options input[type=checkbox] { accent-color: var(--accent); }

  /* Progress */
  .progress-container { width: 100%; max-width: 360px; }
  .progress-bar-bg {
    width: 100%; height: 4px; background: var(--surface2);
    border-radius: 2px; overflow: hidden; margin-bottom: 16px;
  }
  .progress-bar {
    height: 100%; background: var(--accent); border-radius: 2px;
    transition: width 0.3s ease;
  }
  .progress-bar.error { background: var(--red); }
  .progress-steps { text-align: left; font-size: 12px; line-height: 2; }
  .progress-steps .step { color: var(--text2); }
  .progress-steps .step.active { color: var(--accent); font-weight: 500; }
  .progress-steps .step.done { color: var(--green); }
  .progress-steps .step.error { color: var(--red); }
  .progress-steps .step::before { margin-right: 8px; }
  .progress-steps .step.done::before { content: '\2713'; color: var(--green); }
  .progress-steps .step.active::before { content: '\25CB'; color: var(--accent); }
  .progress-steps .step.pending::before { content: '\25CB'; color: var(--border); }
  .progress-steps .step.error::before { content: '\2717'; color: var(--red); }
  .progress-elapsed {
    margin-top: 12px; font-size: 11px; color: var(--text2);
  }
  .error-msg {
    margin-top: 12px; padding: 10px 14px; background: rgba(255,77,106,0.1);
    border: 1px solid var(--red); border-radius: 6px;
    color: var(--red); font-size: 12px; text-align: left;
  }

  /* Sidebar */
  .sidebar {
    background: var(--surface); border-left: 1px solid var(--border);
    overflow-y: auto; padding: 16px;
    display: flex; flex-direction: column; gap: 16px;
  }
  .panel { background: var(--surface2); border-radius: 8px; padding: 14px; }
  .panel h3 {
    font-size: 11px; text-transform: uppercase;
    letter-spacing: 1px; color: var(--text2); margin-bottom: 12px;
  }
  .panel.disabled { opacity: 0.4; pointer-events: none; }
  /* PTZ */
  .ptz-grid {
    display: grid; grid-template-columns: 1fr 1fr 1fr;
    gap: 6px; max-width: 180px; margin: 0 auto;
  }
  .ptz-btn {
    width: 52px; height: 44px; background: var(--bg);
    border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); font-size: 18px; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
    transition: background 0.1s; user-select: none;
  }
  .ptz-btn:hover { background: var(--surface); }
  .ptz-btn:active { background: var(--accent); border-color: var(--accent); }
  .ptz-btn.stop-btn { font-size: 12px; font-weight: 600; }
  .speed-control {
    display: flex; align-items: center; gap: 8px;
    margin-top: 10px; font-size: 12px; color: var(--text2);
  }
  .speed-control input[type=range] { flex: 1; accent-color: var(--accent); }
  .gimbal-info {
    display: grid; grid-template-columns: 1fr 1fr 1fr;
    gap: 8px; margin-top: 8px;
  }
  .gimbal-axis { text-align: center; font-size: 11px; color: var(--text2); }
  .gimbal-axis .value { font-size: 16px; font-weight: 600; color: var(--text); }
  /* Camera */
  .cam-buttons { display: flex; flex-direction: column; gap: 8px; }
  .cam-btn {
    padding: 10px 14px; background: var(--bg);
    border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); font-size: 13px; cursor: pointer;
    text-align: left; transition: background 0.1s;
  }
  .cam-btn:hover { background: var(--surface); }
  .cam-btn.recording { border-color: var(--red); color: var(--red); }
  .cam-btn .icon { margin-right: 8px; display: inline-flex; vertical-align: middle; }
  .cam-btn .icon svg { width: 16px; height: 16px; }
  .header-right svg { width: 14px; height: 14px; vertical-align: -2px; margin-right: 2px; }
  .mode-select { display: flex; gap: 4px; margin-top: 8px; }
  .mode-btn {
    flex: 1; padding: 6px 4px; background: var(--bg);
    border: 1px solid var(--border); border-radius: 4px;
    color: var(--text2); font-size: 11px; cursor: pointer; text-align: center;
  }
  .mode-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  /* Status bar */
  .statusbar {
    display: flex; align-items: center; gap: 24px;
    padding: 8px 20px; background: var(--surface);
    border-top: 1px solid var(--border); font-size: 12px; color: var(--text2);
  }
  .statusbar .stat-value { color: var(--text); font-weight: 500; margin-left: 4px; }
</style>
</head>
<body>
<div class="app">
  <div class="header">
    <h1>
      <span class="status-dot disconnected" id="statusDot"></span>
      DJI Pocket 3
    </h1>
    <div class="header-right">
      <span id="batteryInfo">--</span>
      <span id="videoStats">--</span>
    </div>
  </div>

  <div class="main">
    <div class="video-container" id="videoContainer">
      <!-- Video feed (hidden until connected) -->
      <img id="videoFeed" style="display:none">
      <!-- Connect screen (shown when disconnected) -->
      <div class="connect-screen" id="connectScreen">
        <h2>DJI Pocket 3</h2>
        <div class="subtitle">
          Connect to your camera to start live video, PTZ control, and camera commands.<br>
          The full connection process (BLE + WiFi) can take <strong>up to 90 seconds</strong>.
        </div>
        <div class="connect-options">
          <label><input type="checkbox" id="skipBle"> Skip BLE (WiFi already active)</label>
          <label><input type="checkbox" id="skipWifi"> Skip WiFi (already connected)</label>
        </div>
        <button class="connect-btn" id="connectBtn" onclick="startConnect()">
          Connect to Camera
        </button>
        <!-- Progress (shown during connection) -->
        <div class="progress-container" id="progressContainer" style="display:none">
          <div class="progress-bar-bg"><div class="progress-bar" id="progressBar" style="width:0%"></div></div>
          <div class="progress-steps" id="progressSteps"></div>
          <div class="progress-elapsed" id="progressElapsed"></div>
        </div>
        <div class="error-msg" id="errorMsg" style="display:none"></div>
      </div>
      <div class="video-overlay">
        <span id="recIndicator"></span>
      </div>
    </div>

    <div class="sidebar">
      <div class="panel" id="ptzPanel">
        <h3>PTZ Control</h3>
        <div class="ptz-grid">
          <div></div>
          <button class="ptz-btn" onmousedown="ptzStart(0,1)" onmouseup="ptzStop()" ontouchstart="ptzStart(0,1)" ontouchend="ptzStop()">&#9650;</button>
          <div></div>
          <button class="ptz-btn" onmousedown="ptzStart(-1,0)" onmouseup="ptzStop()" ontouchstart="ptzStart(-1,0)" ontouchend="ptzStop()">&#9664;</button>
          <button class="ptz-btn stop-btn" onclick="ptzStop()">STOP</button>
          <button class="ptz-btn" onmousedown="ptzStart(1,0)" onmouseup="ptzStop()" ontouchstart="ptzStart(1,0)" ontouchend="ptzStop()">&#9654;</button>
          <div></div>
          <button class="ptz-btn" onmousedown="ptzStart(0,-1)" onmouseup="ptzStop()" ontouchstart="ptzStart(0,-1)" ontouchend="ptzStop()">&#9660;</button>
          <div></div>
        </div>
        <div class="speed-control">
          <span>Speed</span>
          <input type="range" id="speedSlider" min="1" max="10" value="3" oninput="updateSpeed()">
          <span id="speedValue">0.3</span>
        </div>
        <div class="gimbal-info">
          <div class="gimbal-axis"><div class="value" id="gimbalYaw">--</div>Yaw</div>
          <div class="gimbal-axis"><div class="value" id="gimbalPitch">--</div>Pitch</div>
          <div class="gimbal-axis"><div class="value" id="gimbalRoll">--</div>Roll</div>
        </div>
      </div>

      <div class="panel" id="cameraPanel">
        <h3>Camera</h3>
        <div class="cam-buttons">
          <button class="cam-btn" onclick="cameraAction('photo')">
            <span class="icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg></span> Take Photo
          </button>
          <button class="cam-btn" id="recordBtn" onclick="cameraAction('record_toggle')">
            <span class="icon" id="recordIcon"><svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="8"/></svg></span> Start Recording
          </button>
        </div>
        <div class="mode-select">
          <button class="mode-btn" data-mode="0" onclick="setMode(0)">Photo</button>
          <button class="mode-btn active" data-mode="1" onclick="setMode(1)">Video</button>
          <button class="mode-btn" data-mode="3" onclick="setMode(3)">Slo-Mo</button>
          <button class="mode-btn" data-mode="4" onclick="setMode(4)">TL</button>
        </div>
      </div>

      <div class="panel">
        <h3>Status</h3>
        <div style="font-size:12px;color:var(--text2);line-height:1.8" id="statusPanel">
          Not connected
        </div>
      </div>
    </div>
  </div>

  <div class="statusbar">
    <span>RX:<span class="stat-value" id="statRx">0</span></span>
    <span>TX:<span class="stat-value" id="statTx">0</span></span>
    <span>Video:<span class="stat-value" id="statFrames">0</span> frames</span>
    <span>Data:<span class="stat-value" id="statMB">0</span> MB</span>
    <span>Mode:<span class="stat-value" id="statMode">--</span></span>
  </div>
</div>

<script>
let ptzSpeed = 0.3;
let isConnected = false;

const STEPS = [
  'Scanning for camera via Bluetooth',
  'Pairing with camera',
  'Waiting for WiFi AP (~20s)',
  'Joining camera WiFi',
  'Verifying camera reachable',
  'UDP handshake',
  'Starting video stream',
  'Connected'
];

function api(url, data) {
  return fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data || {})
  }).then(r => r.json()).catch(() => ({}));
}

function startConnect() {
  const skipBle = document.getElementById('skipBle').checked;
  const skipWifi = document.getElementById('skipWifi').checked;
  document.getElementById('connectBtn').disabled = true;
  document.getElementById('connectBtn').textContent = 'Connecting...';
  document.getElementById('progressContainer').style.display = 'block';
  document.getElementById('errorMsg').style.display = 'none';
  api('/api/connect', {skip_ble: skipBle, skip_wifi: skipWifi});
  pollConnectStatus();
}

function pollConnectStatus() {
  fetch('/api/connect/status').then(r => r.json()).then(d => {
    const pct = Math.max(0, Math.min(100, (d.step_idx / d.total_steps) * 100));
    const bar = document.getElementById('progressBar');
    bar.style.width = pct + '%';
    bar.className = 'progress-bar' + (d.status === 'error' ? ' error' : '');

    // Build step list
    let html = '';
    STEPS.forEach((name, i) => {
      let cls = 'pending';
      if (d.status === 'error' && i === d.step_idx) cls = 'error';
      else if (i < d.step_idx) cls = 'done';
      else if (i === d.step_idx) cls = 'active';
      html += '<div class="step ' + cls + '">' + name + '</div>';
    });
    document.getElementById('progressSteps').innerHTML = html;
    document.getElementById('progressElapsed').textContent =
      'Elapsed: ' + d.elapsed_s + 's';

    if (d.status === 'connecting') {
      setTimeout(pollConnectStatus, 500);
    } else if (d.status === 'connected') {
      onConnected();
    } else if (d.status === 'error') {
      document.getElementById('connectBtn').disabled = false;
      document.getElementById('connectBtn').textContent = 'Retry Connection';
      document.getElementById('errorMsg').style.display = 'block';
      document.getElementById('errorMsg').textContent = d.error;
    }
  }).catch(() => setTimeout(pollConnectStatus, 1000));
}

function onConnected() {
  isConnected = true;
  document.getElementById('connectScreen').style.display = 'none';
  const img = document.getElementById('videoFeed');
  img.src = '/api/video_feed';
  img.style.display = 'block';
  document.getElementById('ptzPanel').classList.remove('disabled');
  document.getElementById('cameraPanel').classList.remove('disabled');
}

function ptzStart(yaw, pitch) {
  if (!isConnected) return;
  api('/api/gimbal', {action: 'set_speed', yaw: yaw * ptzSpeed, pitch: pitch * ptzSpeed});
}
function ptzStop() {
  if (!isConnected) return;
  api('/api/gimbal', {action: 'stop'});
}
function updateSpeed() {
  ptzSpeed = document.getElementById('speedSlider').value / 10;
  document.getElementById('speedValue').textContent = ptzSpeed.toFixed(1);
}
function cameraAction(action) {
  if (!isConnected) return;
  api('/api/camera/' + action);
}
function setMode(mode) {
  if (!isConnected) return;
  api('/api/camera/mode', {mode: mode});
  document.querySelectorAll('.mode-btn').forEach(b =>
    b.classList.toggle('active', parseInt(b.dataset.mode) === mode));
}

// Keyboard
document.addEventListener('keydown', e => {
  if (e.repeat || !isConnected) return;
  switch(e.key) {
    case 'ArrowUp': case 'w': case 'W': ptzStart(0,1); break;
    case 'ArrowDown': case 's': case 'S': ptzStart(0,-1); break;
    case 'ArrowLeft': case 'a': case 'A': ptzStart(-1,0); break;
    case 'ArrowRight': case 'd': case 'D': ptzStart(1,0); break;
    case ' ': ptzStop(); e.preventDefault(); break;
    case 'p': case 'P': cameraAction('photo'); break;
    case 'o': case 'O': cameraAction('record_toggle'); break;
  }
});
document.addEventListener('keyup', e => {
  if (!isConnected) return;
  if (['ArrowUp','ArrowDown','ArrowLeft','ArrowRight','w','W','a','A','s','S','d','D'].includes(e.key))
    ptzStop();
});

// Status poll
setInterval(() => {
  fetch('/api/status').then(r => r.json()).then(d => {
    const dot = document.getElementById('statusDot');
    if (d.connected) {
      dot.className = 'status-dot connected';
      if (!isConnected) onConnected();
    } else {
      // Check connect status for connecting state
      fetch('/api/connect/status').then(r => r.json()).then(cs => {
        dot.className = 'status-dot ' + (cs.status === 'connecting' ? 'connecting' : 'disconnected');
      }).catch(() => {});
    }

    document.getElementById('statRx').textContent = d.rx_packets || 0;
    document.getElementById('statTx').textContent = d.tx_packets || 0;
    document.getElementById('statFrames').textContent = d.video_frames || 0;
    document.getElementById('statMB').textContent = d.video_mb || 0;

    if (d.gimbal) {
      document.getElementById('gimbalYaw').textContent = d.gimbal.yaw + '\u00B0';
      document.getElementById('gimbalPitch').textContent = d.gimbal.pitch + '\u00B0';
      document.getElementById('gimbalRoll').textContent = d.gimbal.roll + '\u00B0';
    }
    if (d.camera) {
      document.getElementById('statMode').textContent = d.camera.mode;
      const recBtn = document.getElementById('recordBtn');
      if (d.camera.recording) {
        recBtn.className = 'cam-btn recording';
        recBtn.innerHTML = '<span class="icon"><svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="1"/></svg></span> Stop Recording';
        document.getElementById('recIndicator').innerHTML = '<span class="rec">\u25CF REC</span> ';
      } else {
        recBtn.className = 'cam-btn';
        recBtn.innerHTML = '<span class="icon"><svg viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="8"/></svg></span> Start Recording';
        document.getElementById('recIndicator').textContent = '';
      }
      if (d.camera.battery >= 0)
        document.getElementById('batteryInfo').innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="1" y="6" width="18" height="12" rx="2"/><line x1="23" y1="10" x2="23" y2="14"/></svg> ' + d.camera.battery + '%';
      document.querySelectorAll('.mode-btn').forEach(b => {
        const m = {0:'Photo',1:'Video',3:'SlowMo',4:'Timelapse'};
        b.classList.toggle('active', m[parseInt(b.dataset.mode)] === d.camera.mode);
      });
      let sh = 'Mode: '+d.camera.mode+'<br>';
      if (d.camera.battery>=0) sh += 'Battery: '+d.camera.battery+'%<br>';
      if (d.camera.recording) sh += 'Recording: '+d.camera.recording_time+'s<br>';
      sh += 'SD: '+(d.camera.sd_inserted?'Inserted':'None')+'<br>';
      sh += 'Video: '+(d.video_mb||0)+' MB';
      document.getElementById('statusPanel').innerHTML = sh;
    }
    document.getElementById('videoStats').textContent =
      (d.video_frames||0)+' frames | '+(d.video_mb||0)+' MB';
  }).catch(() => {});
}, 500);

// Disable panels initially
document.getElementById('ptzPanel').classList.add('disabled');
document.getElementById('cameraPanel').classList.add('disabled');
</script>
</body>
</html>
"""
