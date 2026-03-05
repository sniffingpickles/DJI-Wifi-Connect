"""Camera control and status for DJI Pocket 3.

Status parsing from Mimo PCAP analysis:
- Camera:0x80 (60B) - main camera status, pushed ~12Hz
- Camera:0xdc (22B) - SD card / storage status, pushed ~3Hz
- Camera:0x8e (9B)  - heartbeat response
- Camera:0xa0 (28B) - camera state (photo count, recording time, etc.)
- ESC:0x02 (34B)    - battery status

Heartbeats sent by Mimo:
- Camera:0x8e PUSH to Cam(0) @ ~15Hz - camera heartbeat
- Camera:0xa0 PUSH to Cam(0) @ ~4Hz  - camera state query
- Camera:0x61 PUSH to Cam(0) @ ~1Hz  - camera status poll
"""

import struct
import threading
import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("pocket3.camera")

# Camera modes (byte 0 of Camera:0x80)
MODE_PHOTO = 0
MODE_VIDEO = 1
MODE_PLAYBACK = 2
MODE_SLOW_MO = 3
MODE_TIMELAPSE = 4
MODE_PANO = 5

MODE_NAMES = {
    MODE_PHOTO: "Photo",
    MODE_VIDEO: "Video",
    MODE_PLAYBACK: "Playback",
    MODE_SLOW_MO: "SlowMo",
    MODE_TIMELAPSE: "Timelapse",
    MODE_PANO: "Panorama",
}


@dataclass
class CameraStatus:
    """Parsed camera status from Camera:0x80 push."""
    mode: int = 0
    mode_name: str = "Unknown"
    recording: bool = False
    sd_inserted: bool = False
    sd_free_mb: int = 0
    recording_time_s: int = 0
    photo_count: int = 0
    battery_percent: int = -1
    battery_mv: int = 0
    raw_80: bytes = field(default_factory=bytes, repr=False)
    raw_dc: bytes = field(default_factory=bytes, repr=False)
    raw_battery: bytes = field(default_factory=bytes, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def summary(self) -> str:
        parts = [f"Mode={self.mode_name}"]
        if self.recording:
            parts.append(f"REC {self.recording_time_s}s")
        if self.battery_percent >= 0:
            parts.append(f"Bat={self.battery_percent}%")
        if self.sd_inserted:
            parts.append(f"SD={self.sd_free_mb}MB free")
        return " | ".join(parts)


class CameraController:
    """Camera control and status for DJI Pocket 3."""

    def __init__(self, udp_client):
        self.client = udp_client
        self.status = CameraStatus()
        self._running = False
        self._heartbeat_thread: threading.Thread | None = None
        self._hb_counter = 0

        # Register callbacks for incoming camera status
        self.client.register_duml_callback(2, 0x80, self._on_camera_status)
        self.client.register_duml_callback(2, 0xDC, self._on_storage_status)
        self.client.register_duml_callback(13, 0x02, self._on_battery_status)
        self.client.register_duml_callback(2, 0xA0, self._on_camera_state)

    def start(self):
        """Start camera heartbeat loops."""
        if self._running:
            return
        self._running = True
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="camera-hb")
        self._heartbeat_thread.start()
        logger.info("Camera heartbeats started")

    def stop(self):
        """Stop camera heartbeat loops."""
        self._running = False
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=2.0)
        logger.info("Camera heartbeats stopped")

    # ---- Status parsing ----

    def _on_camera_status(self, pkt: dict):
        """Parse Camera:0x80 status push (60B, ~12Hz from camera)."""
        payload = pkt["payload"]
        if len(payload) < 10:
            return
        with self.status._lock:
            self.status.raw_80 = payload
            mode = payload[0] & 0x0F
            self.status.mode = mode
            self.status.mode_name = MODE_NAMES.get(mode, f"Mode{mode}")
            self.status.recording = bool(payload[1] & 0x01)

    def _on_storage_status(self, pkt: dict):
        """Parse Camera:0xDC storage push (22B, ~3Hz from camera)."""
        payload = pkt["payload"]
        if len(payload) < 10:
            return
        with self.status._lock:
            self.status.raw_dc = payload
            self.status.sd_inserted = bool(payload[0] & 0x01)

    def _on_battery_status(self, pkt: dict):
        """Parse ESC:0x02 battery push (34B, ~1Hz from camera)."""
        payload = pkt["payload"]
        if len(payload) < 3:
            return
        with self.status._lock:
            self.status.raw_battery = payload
            self.status.battery_mv = struct.unpack_from("<H", payload, 1)[0]
            # Battery percentage at byte 20 (confirmed from packet analysis)
            if len(payload) >= 21:
                bp = payload[20]
                if 0 <= bp <= 100:
                    self.status.battery_percent = bp

    def _on_camera_state(self, pkt: dict):
        """Parse Camera:0xA0 state response (28B)."""
        payload = pkt["payload"]
        if len(payload) < 10:
            return
        with self.status._lock:
            if len(payload) >= 8:
                self.status.recording_time_s = struct.unpack_from("<H", payload, 6)[0]

    # ---- Heartbeats (replicating Mimo's pattern) ----

    def _heartbeat_loop(self):
        """Send camera heartbeats matching Mimo's pattern."""
        tick = 0
        while self._running:
            try:
                # Camera:0x8e heartbeat @ ~5Hz (every 200ms)
                # Mimo sends at ~15Hz but 5Hz is sufficient
                self._send_camera_heartbeat()

                # Camera:0xa0 state query @ ~1Hz (every 5 ticks)
                if tick % 5 == 0:
                    self._send_state_query()

                # Camera:0x61 status poll @ ~0.5Hz (every 10 ticks)
                if tick % 10 == 0:
                    self._send_status_poll()

                tick += 1
                time.sleep(0.2)
            except Exception as e:
                logger.warning(f"Camera heartbeat error (continuing): {e}")
                time.sleep(0.5)

    def _send_camera_heartbeat(self):
        """Camera:0x8e PUSH to Cam(0) - camera keepalive."""
        # Mimo payload: 00 01 14 00 (mode byte, flags, resolution code, extra)
        payload = struct.pack("<BBBB", 0x00, 0x01, 0x14, 0x00)
        self.client.send_duml_push(
            receiver_type=1, receiver_id=0,  # Camera(0)
            cmd_set=2, cmd_id=0x8E,
            payload=payload,
        )

    def _send_state_query(self):
        """Camera:0xa0 PUSH to Cam(0) - query camera state."""
        self.client.send_duml_push(
            receiver_type=1, receiver_id=0,  # Camera(0)
            cmd_set=2, cmd_id=0xA0,
            payload=b"",
        )

    def _send_status_poll(self):
        """Camera:0x61 PUSH to Cam(0) - status poll."""
        self.client.send_duml_push(
            receiver_type=1, receiver_id=0,  # Camera(0)
            cmd_set=2, cmd_id=0x61,
            payload=b"",
        )

    # ---- Camera commands ----

    def take_photo(self):
        """Take a photo. Sends Camera:0x01 REQ to Cam(0).

        Note: Command derived from DJI protocol standard. The exact
        payload may need adjustment for Pocket 3.
        """
        logger.info("Taking photo...")
        self.client.send_duml_req(
            receiver_type=1, receiver_id=0,
            cmd_set=2, cmd_id=0x01,
            payload=b"",
        )

    def start_recording(self):
        """Start video recording. Sends Camera:0x20 REQ to Cam(0).

        Note: Command derived from DJI protocol standard. The exact
        payload may need adjustment for Pocket 3.
        """
        logger.info("Start recording...")
        self.client.send_duml_req(
            receiver_type=1, receiver_id=0,
            cmd_set=2, cmd_id=0x20,
            payload=b"",
        )

    def stop_recording(self):
        """Stop video recording. Sends Camera:0x21 REQ to Cam(0).

        Note: Command derived from DJI protocol standard. The exact
        payload may need adjustment for Pocket 3.
        """
        logger.info("Stop recording...")
        self.client.send_duml_req(
            receiver_type=1, receiver_id=0,
            cmd_set=2, cmd_id=0x21,
            payload=b"",
        )

    def toggle_recording(self):
        """Toggle video recording on/off based on current state."""
        if self.status.recording:
            self.stop_recording()
        else:
            self.start_recording()

    def set_mode(self, mode: int):
        """Switch camera mode (photo/video/slowmo/timelapse).

        Args:
            mode: MODE_PHOTO(0), MODE_VIDEO(1), MODE_SLOW_MO(3), MODE_TIMELAPSE(4)

        Note: Command derived from DJI protocol standard.
        """
        mode_name = MODE_NAMES.get(mode, f"Mode{mode}")
        logger.info(f"Setting camera mode: {mode_name}")
        self.client.send_duml_req(
            receiver_type=1, receiver_id=0,
            cmd_set=2, cmd_id=0x02,
            payload=bytes([mode]),
        )

    def send_raw(self, cmd_set: int, cmd_id: int, payload: bytes = b"",
                 receiver_type: int = 1, receiver_id: int = 0):
        """Send a raw DUML command for experimentation.

        Args:
            cmd_set: DUML command set (e.g. 2 for Camera)
            cmd_id: DUML command ID
            payload: Command payload bytes
            receiver_type: Target device type (1=Camera, 4=Gimbal, 8=DM368)
            receiver_id: Target device ID
        """
        logger.info(f"Raw cmd: set=0x{cmd_set:02x} id=0x{cmd_id:02x} "
                    f"payload={payload.hex()} -> dev={receiver_type}({receiver_id})")
        self.client.send_duml_req(
            receiver_type=receiver_type, receiver_id=receiver_id,
            cmd_set=cmd_set, cmd_id=cmd_id,
            payload=payload,
        )
