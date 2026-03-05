# DJI Pocket 3 Control Suite

An educational Python project exploring the DJI Pocket 3's WiFi communication protocol. Implements BLE pairing, UDP video streaming, PTZ gimbal control, and camera commands using publicly documented DJI protocol standards.

Built for learning purposes, to understand how camera control protocols work at the transport layer. Tested on macOS 14+.

## What it does

- BLE scan/pair to activate the camera's WiFi AP
- Join the camera WiFi network (macOS `networksetup`)
- UDP protocol handshake and packet framing on port 9004
- H.264 video receive (1280x720 @ 60fps), pipe to ffplay or write to file
- DUML command layer for gimbal speed control, camera shutter, record, mode switch
- Camera status parsing (battery, recording state, SD card, gimbal attitude)
- Web UI with MJPEG live preview, PTZ controls, and camera commands

## Requirements

- Python 3.11+
- macOS (WiFi joining uses `networksetup`; Linux would need equivalent)
- ffmpeg/ffplay (video display)
- Bluetooth LE hardware

## Install

```bash
git clone https://github.com/sniffingpickles/DJI-Wifi-Connect.git
cd DJI-Wifi-Connect
pip install -r requirements.txt
brew install ffmpeg   # or: sudo apt install ffmpeg
```

Update the WiFi credentials in `pocket3/wifi.py` to match your camera (found in the camera's network settings):

```python
DEFAULT_SSID = "OsmoPocket3-XXXX"
DEFAULT_PASSWORD = "your_password"
```

## Usage

```bash
# Full flow: BLE pair, join WiFi, start video + PTZ
python -m pocket3

# Skip BLE (camera WiFi AP already active)
python -m pocket3 --no-ble

# Already on camera WiFi
python -m pocket3 --no-ble --no-wifi

# Web UI (opens browser at localhost:5555)
python -m pocket3 --gui

# Record to file without viewer
python -m pocket3 --no-video --record output.h264

# Debug logging
python -m pocket3 -v
```

### CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--ssid` | `OsmoPocket3-D6B1` | Camera WiFi SSID |
| `--password` | `hRiGeGppd8ip` | Camera WiFi password |
| `--camera-ip` | `192.168.2.1` | Camera IP |
| `--ble-timeout` | `15.0` | BLE scan timeout in seconds |
| `--record FILE` | | Write raw H.264 to file |
| `--no-ble` | | Skip BLE activation |
| `--no-wifi` | | Skip WiFi join |
| `--no-video` | | Don't launch ffplay |
| `--gui` | | Launch web UI |
| `--port` | `5555` | Web UI port |
| `-v` | | Debug logging |

### Keyboard controls (CLI mode)

| Key | Action |
|-----|--------|
| W / Up | Tilt up |
| S / Down | Tilt down |
| A / Left | Pan left |
| D / Right | Pan right |
| Q / E | Slow pan left/right |
| R / F | Slow tilt up/down |
| Space | Stop gimbal |
| 1-9 | Set gimbal speed |
| P | Take photo |
| O | Toggle recording |
| M | Cycle camera mode |
| V | Toggle video viewer |
| I | Print status |
| Esc | Quit |

### Web UI

`--gui` starts a Flask server and opens the browser. The UI provides:

- MJPEG live video (H.264 decoded via ffmpeg subprocess)
- PTZ d-pad with speed slider (also responds to WASD/arrow keys)
- Photo/record/mode buttons
- Real-time status (battery, gimbal angles, packet counts)
- Connection flow with step-by-step progress (BLE + WiFi takes up to 90s)

Connection can skip BLE or WiFi via checkboxes if the camera AP is already active or you're already on the camera network.

## File structure

```
pocket3/
  __init__.py
  __main__.py       python -m pocket3 entry point
  main.py           CLI argument parsing, keyboard control loop
  ble.py            BLE scan, GATT pairing (bleak), WiFi AP activation
  wifi.py           macOS WiFi join via networksetup, ping check
  udp_protocol.py   UDP framing, handshake, ACK loop, DUML dispatch
  duml.py           DUML packet build/parse, CRC8/CRC16
  video.py          H.264 receiver, ffplay pipe, file writer
  gimbal.py         PTZ speed commands, attitude feedback parsing
  camera.py         Camera heartbeats, status parsing, shutter/record/mode
  web.py            Flask web UI, MJPEG streamer, connection manager
```

## Protocol overview

The camera communicates over UDP on port 9004 using the DUML command layer, which is well-documented across the DJI ecosystem through publicly available open-source projects.

### Connection sequence

1. BLE scan for DJI device advertisements
2. GATT pairing to activate the camera's WiFi AP
3. Wait ~20s for camera WiFi to become available
4. Join WiFi network (SSID/password printed on the camera body)
5. UDP handshake to `192.168.2.1:9004`
6. Register as a client and start video heartbeat
7. Receive H.264 video and DUML telemetry
8. Send periodic ACKs to maintain the connection

### UDP packet header (8 bytes, little-endian)

| Offset | Field |
|--------|-------|
| 0-1 | Bit 15 = 1, bits 14:0 = packet length |
| 2-3 | Session ID |
| 4-5 | Sequence number |
| 6 | Packet type |
| 7 | XOR checksum of bytes 0-6 |

Packet types: 0x00 handshake, 0x01 telemetry, 0x02 video, 0x03 acked telemetry, 0x04 ACK, 0x05 command.

### DUML packet format

| Offset | Field |
|--------|-------|
| 0 | `0x55` SOF |
| 1-2 | Length (10 bits) + version (6 bits) |
| 3 | CRC8 of bytes 0-2 |
| 4 | Sender: `(device_id << 5) \| device_type` |
| 5 | Receiver: `(device_id << 5) \| device_type` |
| 6-7 | Sequence number |
| 8 | Cmd type (bits 7:5) + encrypt (bits 2:0) |
| 9 | Command set |
| 10 | Command ID |
| 11+ | Payload |
| last 2 | CRC16 |

### Key commands

| Cmd set | Cmd ID | Direction | Purpose |
|---------|--------|-----------|---------|
| 0 (General) | 0x4F | TX | Video heartbeat, ~5Hz |
| 0 | 0x81 | TX | DM368 app registration |
| 0 | 0x82 | TX | Video client registration |
| 0 | 0x88 | TX | DM368 keepalive |
| 2 (Camera) | 0x80 | RX | Camera status push, ~12Hz |
| 2 | 0x8E | TX | Camera heartbeat |
| 2 | 0xA0 | TX | Camera state query |
| 2 | 0x01 | TX | Take photo |
| 2 | 0x20 | TX | Start recording |
| 2 | 0x21 | TX | Stop recording |
| 2 | 0x02 | TX | Set camera mode |
| 4 (Gimbal) | 0x01 | TX | Speed control (10-byte payload) |
| 4 | 0x05 | RX | Attitude feedback, ~10Hz |

### Implementation notes

- Use the camera's session ID from the handshake response for all subsequent packets.
- Only one handshake per connection. The camera binds sequence windows to the initial seed.
- 16-bit sequence numbers with wrap-around handling.
- ACKs at ~50Hz to maintain a stable connection.
- Large UDP receive buffer (>= 1MB) to handle video throughput.
- Non-blocking video callbacks to avoid stalling the receive loop.

## API usage

```python
from pocket3.udp_protocol import DjiUdpClient
from pocket3.video import VideoReceiver
from pocket3.gimbal import GimbalController
from pocket3.camera import CameraController

client = DjiUdpClient(camera_ip="192.168.2.1")
client.connect()

video = VideoReceiver()
client.set_video_callback(video.on_video_data)
video.start_recording("output.h264")

gimbal = GimbalController(client)
camera = CameraController(client)

client.start()
gimbal.start()
camera.start()
client.start_video()

gimbal.set_speed(yaw=0.3, pitch=0.0)
camera.take_photo()
camera.start_recording()
camera.set_mode(1)  # 0=Photo, 1=Video, 3=SlowMo, 4=Timelapse

camera.stop()
gimbal.stop()
video.stop()
client.stop()
```

## Troubleshooting

**BLE pairing fails**: Camera must be powered on and not connected to Mimo. The suite retries 3 times automatically. Increase timeout with `--ble-timeout 30`.

**WiFi join fails**: The camera WiFi AP activates ~20s after BLE pairing. Verify SSID/password match your camera. On macOS, the terminal may need WiFi permission in System Settings > Privacy.

**Handshake timeout**: Check that no other process is bound to UDP port 9004. Use `--no-ble --no-wifi` if already on the camera network.

**Video stutter in ffplay**: ffplay display lag does not affect file recording. Use `--no-video --record output.h264` for clean capture.

## References

- [dji-firmware-tools](https://github.com/o-gs/dji-firmware-tools) - DJI firmware analysis tools
- [dji_protocol](https://github.com/samuelsadok/dji_protocol) - UDP protocol documentation
- [Moblin](https://github.com/eerimoq/moblin) - Open-source DJI BLE integration
- [dji-wifi-tools](https://github.com/Toemsel/dji-wifi-tools) - WiFi protocol tooling

## Disclaimer

This project is for **educational and research purposes only**. It is not affiliated with or endorsed by DJI. All protocol information is derived from publicly available open-source projects and documentation. No proprietary software was decompiled or reverse-engineered in the creation of this project. Use at your own risk; the authors are not responsible for any misuse or damage.

## License

MIT. See [LICENSE](LICENSE).
