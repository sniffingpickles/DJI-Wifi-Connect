#!/usr/bin/env python3
"""DJI Pocket 3 standalone control suite.

Full Mimo replacement:
1. BLE scan & pair → activates camera
2. WiFi connect → joins camera AP
3. UDP protocol → handshake on port 9004
4. Video feed → H.264 piped to ffplay
5. PTZ control → keyboard-driven gimbal

Usage:
    python -m pocket3.main              # Full flow: BLE → WiFi → connect → view
    python -m pocket3.main --no-ble     # Skip BLE (WiFi already active)
    python -m pocket3.main --no-video   # Skip video viewer
    python -m pocket3.main --record     # Record H.264 to file
    python -m pocket3.main --ptz-only   # Just PTZ control (WiFi already connected)
"""

import argparse
import asyncio
import logging
import sys
import time
import threading
import struct

from .ble import DjiPocket3BLE
from .wifi import connect_wifi, wait_for_camera, get_current_ssid, DEFAULT_SSID, DEFAULT_PASSWORD
from .udp_protocol import DjiUdpClient
from .gimbal import GimbalController
from .camera import CameraController
from .video import VideoReceiver
from .duml import DEV_TYPES, CMD_SETS

logger = logging.getLogger("pocket3")


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(name)-16s %(levelname)-5s %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")
    # Quiet down bleak
    logging.getLogger("bleak").setLevel(logging.WARNING)


def duml_logger(pkt: dict, direction: str):
    """Log DUML packets in a readable format."""
    sender = DEV_TYPES.get(pkt["sender_type"], f"Dev{pkt['sender_type']}")
    receiver = DEV_TYPES.get(pkt["receiver_type"], f"Dev{pkt['receiver_type']}")
    cmd_set_name = CMD_SETS.get(pkt["cmd_set"], f"Set{pkt['cmd_set']}")
    cmd_type_str = {0: "REQ", 1: "ACK", 2: "PUSH"}.get(pkt["cmd_type"], f"T{pkt['cmd_type']}")
    payload_hex = pkt["payload"].hex(" ")[:60] if pkt["payload"] else ""
    logger.debug(
        f"{direction} {sender}({pkt['sender_id']})->{receiver}({pkt['receiver_id']}) "
        f"{cmd_type_str} {cmd_set_name}:0x{pkt['cmd_id']:02x} "
        f"seq={pkt['seq']} ({len(pkt['payload'])}B) {payload_hex}"
    )


async def _ble_activate_once(ble: DjiPocket3BLE, ssid: str, password: str,
                             timeout: float = 15.0) -> bool:
    """Single BLE scan+pair+WiFi attempt. Raises on failure."""
    devices = await ble.scan(timeout=timeout)
    pocket3_devices = [(d, m) for d, m in devices if m == "OsmoPocket3"]
    if not pocket3_devices:
        if devices:
            logger.warning(f"No Pocket 3 found, but found: {[(d.name, m) for d, m in devices]}")
            pocket3_devices = devices
        else:
            raise RuntimeError("No DJI BLE devices found")

    device, model = pocket3_devices[0]
    logger.info(f"Using: {device.name} ({model})")

    await ble.connect(device, model)
    await ble.activate_and_pair()

    logger.info("BLE paired. Waiting ~20s for WiFi AP to activate (BLE stays connected)...")
    await asyncio.sleep(20)

    # Join WiFi while BLE is still connected
    wifi_ok = await asyncio.get_event_loop().run_in_executor(
        None, lambda: connect_wifi(ssid, password, timeout=30.0)
    )
    if not wifi_ok:
        raise RuntimeError("WiFi join failed while BLE was connected")

    cam_ok = await asyncio.get_event_loop().run_in_executor(
        None, lambda: wait_for_camera(timeout=10.0)
    )
    if not cam_ok:
        raise RuntimeError("Camera not reachable after WiFi join")

    logger.info("WiFi connected! Disconnecting BLE...")
    return True


async def ble_activate_and_wifi(ssid: str, password: str, timeout: float = 15.0,
                                max_retries: int = 3) -> bool:
    """Scan, pair via BLE, and join WiFi. Retries if BLE gets stuck."""
    for attempt in range(1, max_retries + 1):
        ble = DjiPocket3BLE()
        try:
            if attempt > 1:
                logger.info(f"BLE attempt {attempt}/{max_retries}...")
            return await _ble_activate_once(ble, ssid, password, timeout)
        except Exception as e:
            logger.warning(f"BLE attempt {attempt}/{max_retries} failed: {e}")
            if attempt < max_retries:
                logger.info("Retrying BLE in 3s...")
                await asyncio.sleep(3)
        finally:
            try:
                await ble.disconnect()
            except Exception:
                pass
    logger.error(f"BLE failed after {max_retries} attempts")
    return False


def keyboard_control(gimbal: GimbalController, camera: CameraController,
                     client: DjiUdpClient, video: VideoReceiver):
    """Interactive keyboard control loop."""
    import select
    import tty
    import termios

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    print("\n" + "=" * 60)
    print("  DJI Pocket 3 - Control Suite")
    print("=" * 60)
    print("  Arrow keys / WASD : Pan & Tilt gimbal")
    print("  Q/E               : Pan left/right (slow)")
    print("  R/F               : Tilt up/down (slow)")
    print("  Space             : Stop gimbal movement")
    print("  1-9               : Set gimbal speed")
    print("  P                 : Take photo")
    print("  O                 : Toggle recording")
    print("  M                 : Cycle camera mode")
    print("  V                 : Toggle video viewer")
    print("  I                 : Show info/stats")
    print("  Esc / Ctrl+C      : Quit")
    print("=" * 60)

    speed = 0.3
    viewer_active = video._ffplay_proc is not None
    modes = [0, 1, 3, 4]  # Photo, Video, SlowMo, Timelapse
    mode_idx = 1  # Start on Video

    try:
        tty.setcbreak(fd)
        while True:
            if select.select([sys.stdin], [], [], 0.1)[0]:
                ch = sys.stdin.read(1)

                if ch == '\x1b':  # Escape sequence
                    if select.select([sys.stdin], [], [], 0.05)[0]:
                        ch2 = sys.stdin.read(1)
                        if ch2 == '[':
                            ch3 = sys.stdin.read(1)
                            if ch3 == 'A':    # Up arrow
                                gimbal.set_speed(pitch=speed)
                            elif ch3 == 'B':  # Down arrow
                                gimbal.set_speed(pitch=-speed)
                            elif ch3 == 'C':  # Right arrow
                                gimbal.set_speed(yaw=speed)
                            elif ch3 == 'D':  # Left arrow
                                gimbal.set_speed(yaw=-speed)
                    else:
                        break  # Plain Escape = quit

                elif ch in ('w', 'W'):
                    gimbal.set_speed(pitch=speed)
                elif ch in ('s', 'S'):
                    gimbal.set_speed(pitch=-speed)
                elif ch in ('a', 'A'):
                    gimbal.set_speed(yaw=-speed)
                elif ch in ('d', 'D'):
                    gimbal.set_speed(yaw=speed)
                elif ch in ('q', 'Q'):
                    gimbal.set_speed(yaw=-speed * 0.3)
                elif ch in ('e', 'E'):
                    gimbal.set_speed(yaw=speed * 0.3)
                elif ch in ('r', 'R'):
                    gimbal.set_speed(pitch=speed * 0.3)
                elif ch in ('f', 'F'):
                    gimbal.set_speed(pitch=-speed * 0.3)
                elif ch == ' ':
                    gimbal.stop_movement()
                    print("  [STOP]")
                elif ch.isdigit() and ch != '0':
                    speed = int(ch) / 10.0
                    print(f"  Speed: {speed:.1f}")
                elif ch in ('p', 'P'):
                    camera.take_photo()
                    print("  [PHOTO]")
                elif ch in ('o', 'O'):
                    camera.toggle_recording()
                    print(f"  [{'STOP REC' if camera.status.recording else 'START REC'}]")
                elif ch in ('m', 'M'):
                    mode_idx = (mode_idx + 1) % len(modes)
                    camera.set_mode(modes[mode_idx])
                    from .camera import MODE_NAMES
                    print(f"  Mode: {MODE_NAMES.get(modes[mode_idx], '?')}")
                elif ch in ('v', 'V'):
                    if viewer_active:
                        video.stop_viewer()
                        viewer_active = False
                        print("  Video viewer OFF")
                    else:
                        video.start_viewer()
                        viewer_active = True
                        print("  Video viewer ON")
                elif ch in ('i', 'I'):
                    s = client.stats
                    print(f"\n  Camera: {camera.status.summary()}")
                    print(f"  Gimbal: {gimbal.state}")
                    print(f"  Video:  {video.frame_count} frames, "
                          f"{video.byte_count / 1024 / 1024:.1f} MB")
                    print(f"  Net:    rx={s['rx_packets']} tx={s['tx_packets']} "
                          f"duml={s['duml_packets']}")
                elif ch == '\x03':  # Ctrl+C
                    break

    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        gimbal.stop_movement()
        print("\nShutting down...")


def main():
    parser = argparse.ArgumentParser(description="DJI Pocket 3 Control Suite")
    parser.add_argument("--no-ble", action="store_true", help="Skip BLE activation")
    parser.add_argument("--no-wifi", action="store_true", help="Skip WiFi connection")
    parser.add_argument("--no-video", action="store_true", help="Skip video viewer")
    parser.add_argument("--record", type=str, default=None, help="Record H.264 to file")
    parser.add_argument("--ptz-only", action="store_true", help="PTZ only (assume connected)")
    parser.add_argument("--ssid", type=str, default=DEFAULT_SSID, help="Camera WiFi SSID")
    parser.add_argument("--password", type=str, default=DEFAULT_PASSWORD, help="Camera WiFi password")
    parser.add_argument("--camera-ip", type=str, default="192.168.2.1", help="Camera IP")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")
    parser.add_argument("--ble-timeout", type=float, default=15.0, help="BLE scan timeout")
    parser.add_argument("--gui", action="store_true", help="Launch web UI (http://localhost:5555)")
    parser.add_argument("--port", type=int, default=5555, help="Web UI port (default: 5555)")
    args = parser.parse_args()

    setup_logging(args.verbose)

    # GUI mode: start web server first, connection happens on-demand via UI
    if args.gui:
        from .web import create_app, MJPEGStreamer, ConnectionManager
        conn_mgr = ConnectionManager(
            ssid=args.ssid, password=args.password,
            camera_ip=args.camera_ip, ble_timeout=args.ble_timeout,
        )
        app = create_app(connection_manager=conn_mgr)
        logger.info(f"=== Web UI: http://localhost:{args.port} ===")
        import webbrowser
        webbrowser.open(f"http://localhost:{args.port}")
        try:
            app.run(host="0.0.0.0", port=args.port, threaded=True,
                    use_reloader=False)
        except KeyboardInterrupt:
            pass
        finally:
            conn_mgr.shutdown()
            logger.info("Done.")
        return

    if args.ptz_only:
        args.no_ble = True
        args.no_wifi = True

    # Step 1+2: BLE activation + WiFi (combined to keep BLE alive during WiFi join)
    if not args.no_ble:
        logger.info("=== Step 1+2: BLE Activation + WiFi ===")
        success = asyncio.run(ble_activate_and_wifi(
            args.ssid, args.password, timeout=args.ble_timeout
        ))
        if not success:
            logger.warning("BLE+WiFi failed. Trying WiFi-only fallback...")
            if not args.no_wifi:
                if not connect_wifi(args.ssid, args.password):
                    logger.error("WiFi connection failed!")
                    sys.exit(1)
                if not wait_for_camera():
                    logger.error("Camera not reachable!")
                    sys.exit(1)
    elif not args.no_wifi:
        logger.info("=== Step 2: WiFi Connection ===")
        current = get_current_ssid()
        if current == args.ssid:
            logger.info(f"Already on {args.ssid}")
        else:
            if not connect_wifi(args.ssid, args.password):
                logger.error("WiFi connection failed!")
                sys.exit(1)
        if not wait_for_camera():
            logger.error("Camera not reachable!")
            sys.exit(1)

    # Step 3: UDP protocol connection
    logger.info("=== Step 3: UDP Protocol ===")
    client = DjiUdpClient(camera_ip=args.camera_ip)
    client.set_duml_catch_all(duml_logger)

    if not client.connect():
        logger.error("UDP handshake failed!")
        sys.exit(1)

    # Step 4: Video receiver - set up BEFORE starting loops
    video = VideoReceiver()
    client.set_video_callback(video.on_video_data)

    if args.record:
        video.start_recording(args.record)

    if not args.no_video:
        video.start_viewer()

    # Step 5: Gimbal + Camera controllers
    gimbal = GimbalController(client)
    camera = CameraController(client)

    # Start rx/ack loops immediately so we catch the initial video burst
    client.start()
    gimbal.start()
    camera.start()

    # Send Start Live View command (Pocket 3 requires explicit command unlike drones)
    time.sleep(0.5)  # Let camera settle after handshake
    client.start_video()

    logger.info("=== Ready! Press 'I' for stats, 'P' photo, 'O' record, arrows for PTZ ===")

    # Log stats every 5s
    def stats_logger():
        while client._running:
            time.sleep(5)
            s = client.stats
            logger.info(f"Stats: rx={s['rx_packets']} tx={s['tx_packets']} "
                        f"video={s['video_frames']} duml={s['duml_packets']} "
                        f"videoMB={video.byte_count/1024/1024:.1f} "
                        f"| {camera.status.summary()}")
    stats_thread = threading.Thread(target=stats_logger, daemon=True)
    stats_thread.start()

    try:
        if sys.stdin.isatty():
            keyboard_control(gimbal, camera, client, video)
        else:
            logger.info("Running (Ctrl+C to stop)...")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        pass

    logger.info("Shutting down...")
    camera.stop()
    gimbal.stop()
    video.stop()
    client.stop()
    logger.info("Done.")


if __name__ == "__main__":
    main()
