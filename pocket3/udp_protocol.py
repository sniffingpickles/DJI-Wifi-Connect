"""DJI proprietary UDP protocol client for Pocket 3.

Handles the UDP transport layer on port 9004:
- Handshake with session establishment
- Packet framing (type 0x00-0x06)
- ACK management
- Video/telemetry/command multiplexing

Reference: https://github.com/samuelsadok/dji_protocol/blob/master/udp_protocol.md
"""

import struct
import socket
import threading
import time
import random
import logging
from collections import deque

from .duml import build_duml, parse_duml, CMD_TYPE_REQ, CMD_TYPE_ACK, CMD_TYPE_PUSH, CMD_TYPE_WRITE

logger = logging.getLogger("pocket3.udp")

CAMERA_IP = "192.168.2.1"
CAMERA_PORT = 9004

# DJI UDP packet types
TYPE_HANDSHAKE = 0x00
TYPE_TELEMETRY = 0x01
TYPE_VIDEO = 0x02
TYPE_ACK_TELEMETRY = 0x03
TYPE_ACK = 0x04
TYPE_COMMAND = 0x05
TYPE_ACK6 = 0x06

TYPE_NAMES = {
    0: "HANDSHAKE", 1: "TELEMETRY", 2: "VIDEO", 3: "ACK_TELEM",
    4: "ACK", 5: "COMMAND", 6: "ACK6",
}


def _seq_ahead(new_seq: int, old_seq: int) -> bool:
    """Return True if new_seq is ahead of old_seq with 16-bit wrap handling."""
    diff = (new_seq - old_seq) & 0xFFFF
    return 0 < diff < 0x8000


def _build_header(pkt_len: int, session_id: int, seq: int, pkt_type: int) -> bytes:
    """Build 8-byte DJI UDP header."""
    b = bytearray(8)
    pkt_len |= 0x8000  # Bit 15 always set
    b[0] = pkt_len & 0xFF
    b[1] = (pkt_len >> 8) & 0xFF
    b[2] = session_id & 0xFF
    b[3] = (session_id >> 8) & 0xFF
    b[4] = seq & 0xFF
    b[5] = (seq >> 8) & 0xFF
    b[6] = pkt_type & 0xFF
    xor = 0
    for i in range(7):
        xor ^= b[i]
    b[7] = xor
    return bytes(b)


def _parse_header(data: bytes) -> dict | None:
    """Parse 8-byte DJI UDP header."""
    if len(data) < 8:
        return None
    pkt_len = (data[0] | (data[1] << 8)) & 0x7FFF
    session_id = data[2] | (data[3] << 8)
    seq = data[4] | (data[5] << 8)
    pkt_type = data[6]
    xor_check = data[7]
    xor_calc = 0
    for i in range(7):
        xor_calc ^= data[i]
    return {
        "length": pkt_len,
        "session_id": session_id,
        "seq": seq,
        "type": pkt_type,
        "type_name": TYPE_NAMES.get(pkt_type, f"UNK{pkt_type}"),
        "xor_ok": xor_calc == xor_check,
        "data": data[8:] if len(data) > 8 else b"",
    }


class DumlCallback:
    """Callback registration for DUML command responses."""
    def __init__(self, cmd_set: int, cmd_id: int, callback):
        self.cmd_set = cmd_set
        self.cmd_id = cmd_id
        self.callback = callback


class DjiUdpClient:
    """DJI UDP protocol client."""

    def __init__(self, camera_ip: str = CAMERA_IP, camera_port: int = CAMERA_PORT):
        self.camera_ip = camera_ip
        self.camera_port = camera_port
        self.session_id = random.randint(0, 0xFFFF)
        self.sock: socket.socket | None = None
        self._running = False
        self._rx_thread: threading.Thread | None = None
        self._ack_thread: threading.Thread | None = None

        # Sequence counters
        self._cmd_seq = 0        # initialized from seed in connect()
        self._cmd_seq_lock = threading.Lock()
        self._msg_seq = 1        # separate msg_seq counter for type 5 body

        # Received packet tracking for ACKs
        # ACK contains windows for: type 2 (video rx), type 3 (acked telem rx), type 5 (cmd tx)
        self._video_rx_seq = 0       # type 2: video receive window
        self._acktelem_rx_seq = 0    # type 3: acked telemetry receive window (NOT type 1!)
        self._cmd_rx_seq = 0         # type 5 received from camera (if any)

        # Callbacks
        self._video_callbacks = []   # list of (data: bytes) -> None
        self._duml_callbacks: list[DumlCallback] = []
        self._duml_catch_all = None  # (duml_pkt: dict, direction: str) -> None

        # Stats
        self.stats = {
            "rx_packets": 0, "tx_packets": 0,
            "rx_bytes": 0, "tx_bytes": 0,
            "video_frames": 0, "duml_packets": 0,
        }
        self._rx_type_counts = {}  # packet type -> count
        self._udp_rx_count = 0     # UDP-only rx counter

    def set_video_callback(self, callback):
        """Set primary callback for video data: callback(data: bytes)"""
        self._video_callbacks.insert(0, callback)

    def add_video_callback(self, callback):
        """Add additional callback for video data: callback(data: bytes)"""
        self._video_callbacks.append(callback)

    def set_duml_catch_all(self, callback):
        """Set callback for all DUML packets: callback(pkt: dict, direction: str)"""
        self._duml_catch_all = callback

    def register_duml_callback(self, cmd_set: int, cmd_id: int, callback):
        """Register callback for specific DUML command: callback(pkt: dict)"""
        self._duml_callbacks.append(DumlCallback(cmd_set, cmd_id, callback))

    def _next_cmd_seq(self) -> int:
        with self._cmd_seq_lock:
            seq = self._cmd_seq
            self._cmd_seq = (self._cmd_seq + 8) & 0xFFFF  # +8 per cmd like Mimo
            return seq

    def _next_msg_seq(self) -> int:
        with self._cmd_seq_lock:
            seq = self._msg_seq
            self._msg_seq = (self._msg_seq + 1) & 0xFFFF
            return seq

    def _check_port_conflict(self):
        """Warn if another process (e.g. DJIMimo) has port 9004 bound."""
        import subprocess
        try:
            result = subprocess.run(
                ["lsof", "-i", f":{self.camera_port}"],
                capture_output=True, text=True
            )
            for line in result.stdout.splitlines()[1:]:  # skip header
                parts = line.split()
                if parts and parts[0] != "Python" and parts[0] != "python3":
                    logger.warning(
                        f"Port {self.camera_port} in use by {parts[0]} (PID {parts[1]}). "
                        f"Kill it or handshake will fail!"
                    )
        except Exception:
            pass

    def connect(self, timeout: float = 10.0) -> bool:
        """Perform UDP handshake with camera."""
        logger.info(f"Connecting to {self.camera_ip}:{self.camera_port} "
                     f"(session={self.session_id:#06x})...")

        # Check for conflicting processes on port 9004
        self._check_port_conflict()

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)
        # Large recv buffer: video at ~300KB/s needs headroom
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)

        # Bind to WiFi interface IP to ensure packets go out the right interface
        try:
            import subprocess
            iface_ip = subprocess.run(
                ["ipconfig", "getifaddr", "en1"],
                capture_output=True, text=True
            ).stdout.strip()
            if iface_ip and iface_ip.startswith("192.168."):
                self.sock.bind((iface_ip, 0))
                logger.debug(f"Bound to WiFi IP {iface_ip}")
        except Exception:
            pass

        # Build handshake packet with payload (48 bytes total)
        # Payload captured from DJI Mimo app talking to Pocket 3
        seed = random.randint(0, 0xFFFF) & 0xFFF8  # Lower 3 bits = 0
        self._video_rx_seq = seed
        self._acktelem_rx_seq = seed
        self._cmd_seq = seed
        self._msg_seq = 1

        # Exact handshake payload from Mimo capture (40 bytes)
        hs_payload = bytearray([
            seed & 0xFF, (seed >> 8) & 0xFF,        # seq seed
            0x64, 0x00, 0x64, 0x00, 0xC0, 0x05,
            0x14, 0x00, 0x00, 0x64, 0x00, 0x00, 0x01, 0x90,
            0x01, 0xC0, 0x05, 0x14, 0x00, 0x00, 0x64, 0x00,
            0x14, 0x00, 0x64, 0x00, 0xC0, 0x05, 0x14, 0x00,
            0x00, 0x64, 0x00, 0x01, 0x01, 0x04, 0x01, 0x02,
        ])

        total_len = 8 + len(hs_payload)
        handshake = _build_header(total_len, self.session_id, 0, TYPE_HANDSHAKE) + bytes(hs_payload)
        logger.debug(f"TX handshake ({len(handshake)}B): {handshake.hex()}")

        try:
            self.sock.sendto(handshake, (self.camera_ip, self.camera_port))
            self.stats["tx_packets"] += 1
            self.stats["tx_bytes"] += len(handshake)

            # Wait for handshake response (8 or 9 bytes for Pocket 3)
            data, addr = self.sock.recvfrom(4096)
            self.stats["rx_packets"] += 1
            self.stats["rx_bytes"] += len(data)

            hdr = _parse_header(data)
            if hdr and hdr["type"] == TYPE_HANDSHAKE:
                # Adopt camera's session_id for all subsequent packets
                camera_session = hdr["session_id"]
                logger.info(f"Handshake OK! seed={seed} (0x{seed:04x}) "
                            f"our_session=0x{self.session_id:04x} "
                            f"camera_session=0x{camera_session:04x}")
                self.session_id = camera_session
                return True
            else:
                logger.error(f"Bad handshake response ({len(data)}B): {data.hex()}")
                return False

        except socket.timeout:
            logger.error("Handshake timeout!")
            return False

    def reconnect(self):
        """Re-send handshake on existing socket to trigger fresh video burst."""
        seed = random.randint(0, 0xFFFF) & 0xFFF8
        self._video_rx_seq = seed
        self._acktelem_rx_seq = seed
        self._cmd_seq = seed
        self._msg_seq = 1

        hs_payload = bytearray([
            seed & 0xFF, (seed >> 8) & 0xFF,
            0x64, 0x00, 0x64, 0x00, 0xC0, 0x05,
            0x14, 0x00, 0x00, 0x64, 0x00, 0x00, 0x01, 0x90,
            0x01, 0xC0, 0x05, 0x14, 0x00, 0x00, 0x64, 0x00,
            0x14, 0x00, 0x64, 0x00, 0xC0, 0x05, 0x14, 0x00,
            0x00, 0x64, 0x00, 0x01, 0x01, 0x04, 0x01, 0x02,
        ])
        total_len = 8 + len(hs_payload)
        pkt = _build_header(total_len, self.session_id, 0, TYPE_HANDSHAKE) + bytes(hs_payload)
        logger.info(f"Re-handshake (seed={seed:#06x})")
        if self.sock:
            try:
                self.sock.sendto(pkt, (self.camera_ip, self.camera_port))
            except OSError:
                pass

    def start(self):
        """Start receive and ACK threads."""
        if self._running:
            return
        self._running = True
        self.sock.settimeout(0.5)

        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx_thread.start()

        self._ack_thread = threading.Thread(target=self._ack_loop, daemon=True)
        self._ack_thread.start()

        logger.info("Protocol loops started")

    def stop(self):
        """Stop all threads."""
        self._running = False
        if self._rx_thread:
            self._rx_thread.join(timeout=2.0)
        if self._ack_thread:
            self._ack_thread.join(timeout=2.0)
        if self.sock:
            self.sock.close()
            self.sock = None
        logger.info("Protocol stopped")

    def _rx_loop(self):
        """Receive loop - handles all incoming packets."""
        while self._running:
            try:
                data, addr = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break

            self.stats["rx_packets"] += 1
            self.stats["rx_bytes"] += len(data)

            hdr = _parse_header(data)
            if not hdr:
                continue

            pkt_type = hdr["type"]
            self._rx_type_counts[pkt_type] = self._rx_type_counts.get(pkt_type, 0) + 1
            self._udp_rx_count += 1
            if self._udp_rx_count <= 5 or self._udp_rx_count % 200 == 0:
                logger.info(f"UDP pkt#{self._udp_rx_count} type={pkt_type} "
                            f"({TYPE_NAMES.get(pkt_type, '?')}) seq={hdr['seq']} "
                            f"len={hdr['length']} counts={dict(self._rx_type_counts)}")

            if pkt_type == TYPE_VIDEO:
                self.stats["video_frames"] += 1
                if _seq_ahead(hdr["seq"], self._video_rx_seq):
                    self._video_rx_seq = hdr["seq"]
                # Video packet: bytes 0x08-0x0F = window info, 0x10-0x13 = frame info
                # H.264 data starts at offset 0x14 (12 bytes into payload)
                raw = hdr["data"]  # data[8:]
                if len(raw) > 12:
                    h264_data = raw[12:]  # skip window(8) + frame_info(4)
                    if self.stats["video_frames"] <= 3:
                        logger.info(f"Video pkt #{self.stats['video_frames']}: "
                                    f"seq={hdr['seq']} {len(h264_data)}B H.264")
                    elif self.stats["video_frames"] % 1000 == 0:
                        logger.debug(f"Video pkt #{self.stats['video_frames']}: "
                                     f"seq={hdr['seq']} {len(h264_data)}B H.264")
                    for cb in self._video_callbacks:
                        try:
                            cb(h264_data)
                        except Exception:
                            pass

            elif pkt_type == TYPE_TELEMETRY:
                # Type 1: unacked telemetry - no ACK needed, just parse DUML
                raw = hdr["data"]
                if len(raw) >= 20 and self._udp_rx_count <= 3:
                    v_ws, v_we = struct.unpack_from("<HH", raw, 0)
                    t_ws, t_we = struct.unpack_from("<HH", raw, 8)
                    c_ws, c_we = struct.unpack_from("<HH", raw, 16)
                    logger.debug(f"Type1 windows: vid=({v_ws},{v_we}) "
                                 f"tel=({t_ws},{t_we}) cmd=({c_ws},{c_we})")
                self._process_duml(raw, "RX")

            elif pkt_type == TYPE_ACK_TELEMETRY:
                # Type 3: acked telemetry - MUST track seq for ACK window!
                old_seq = self._acktelem_rx_seq
                if _seq_ahead(hdr["seq"], self._acktelem_rx_seq):
                    self._acktelem_rx_seq = hdr["seq"]
                self._process_duml(hdr["data"], "RX")

            elif pkt_type == TYPE_COMMAND:
                if _seq_ahead(hdr["seq"], self._cmd_rx_seq):
                    self._cmd_rx_seq = hdr["seq"]
                self._process_duml(hdr["data"], "RX")

            elif pkt_type == TYPE_ACK:
                pass  # ACK from camera, no action needed

    def _process_duml(self, data: bytes, direction: str):
        """Extract and dispatch DUML packets from payload."""
        packets = parse_duml(data)
        for pkt in packets:
            self.stats["duml_packets"] += 1

            if self._duml_catch_all:
                try:
                    self._duml_catch_all(pkt, direction)
                except Exception as e:
                    logger.error(f"DUML catch-all error: {e}")

            for cb in self._duml_callbacks:
                if cb.cmd_set == pkt["cmd_set"] and cb.cmd_id == pkt["cmd_id"]:
                    try:
                        cb.callback(pkt)
                    except Exception as e:
                        logger.error(f"DUML callback error: {e}")


    def _ack_loop(self):
        """Periodically send ACK packets."""
        while self._running:
            time.sleep(0.02)  # ACK every 20ms (~50/sec, matching Mimo's ~56/sec)
            try:
                self._send_ack()
            except Exception:
                pass

    def _send_ack(self):
        """Send ACK packet (type 0x04) - 26-byte payload matching Mimo format.

        Each section is 8 bytes: start(2B) + end(2B) + resend_count(2B) + resend_extra(2B)
        Then 2B MB payload length.
        """
        payload = bytearray()
        # Type 2 (video) receive window - 8 bytes
        payload += struct.pack("<HHHH", self._video_rx_seq, self._video_rx_seq, 0, 0)
        # Type 3 (acked telemetry) receive window - 8 bytes
        payload += struct.pack("<HHHH", self._acktelem_rx_seq, self._acktelem_rx_seq, 0, 0)
        # Type 5 (command) send window - 8 bytes
        cmd_seq = self._cmd_seq
        payload += struct.pack("<HHHH", cmd_seq, cmd_seq, 0, 0)
        # MB payload length (0 = no DUML data in ACK) - 2 bytes
        payload += struct.pack("<H", 0)

        total_len = 8 + len(payload)
        header = _build_header(total_len, self.session_id, 0, TYPE_ACK)
        pkt = header + bytes(payload)

        if self.sock:
            try:
                self.sock.sendto(pkt, (self.camera_ip, self.camera_port))
                self.stats["tx_packets"] += 1
                self.stats["tx_bytes"] += len(pkt)
            except OSError:
                pass

    def send_duml(self, sender_type: int, sender_id: int,
                  receiver_type: int, receiver_id: int,
                  cmd_set: int, cmd_id: int,
                  payload: bytes = b"",
                  cmd_type: int = CMD_TYPE_REQ) -> int:
        """Send a DUML command via type 5 (COMMAND) packet. Returns sequence number."""
        seq = self._next_cmd_seq()
        duml_pkt = build_duml(
            sender_type=sender_type, sender_id=sender_id,
            receiver_type=receiver_type, receiver_id=receiver_id,
            cmd_set=cmd_set, cmd_id=cmd_id,
            payload=payload, seq=seq, cmd_type=cmd_type,
        )

        # Type 5 command packet layout (from protocol doc + Mimo PCAP):
        # header(8) + send_window(4) + resend_state(4) + counter(1) + 0x01(1) + flags(2) + duml_data
        # CRITICAL: byte 9 of body MUST be 0x01 (not 0x00) or camera drops packet!
        msg_seq = self._next_msg_seq()
        cmd_payload = bytearray()
        # Send window: win_start lags, win_end = current seq
        win_start = (seq - 32) & 0xFFFF  # lag ~4 packets like Mimo
        win_end = seq
        cmd_payload += struct.pack("<HH", win_start, win_end)
        # Resend state (none)
        cmd_payload += struct.pack("<HH", 0, 0)
        # Counter (u8) + constant 0x01 (u8) + flags 0x0060 (u16le)
        # Mimo pattern: XX 01 60 00 (where XX increments per packet)
        cmd_payload += struct.pack("<BBH", msg_seq & 0xFF, 0x01, 0x0060)
        # DUML payload
        cmd_payload += duml_pkt

        total_len = 8 + len(cmd_payload)
        header = _build_header(total_len, self.session_id, seq, TYPE_COMMAND)
        pkt = header + bytes(cmd_payload)

        if self._duml_catch_all:
            # Also notify catch-all about outgoing
            for dp in parse_duml(duml_pkt):
                try:
                    self._duml_catch_all(dp, "TX")
                except Exception:
                    pass

        if self.sock:
            self.sock.sendto(pkt, (self.camera_ip, self.camera_port))
            self.stats["tx_packets"] += 1
            self.stats["tx_bytes"] += len(pkt)

        return seq

    def _send_duml_ack(self, pkt: dict):
        """Send DUML ACK response to an incoming REQ/WRITE command.
        Camera drops connection if REQ commands go unanswered."""
        self.send_duml(
            sender_type=pkt["receiver_type"], sender_id=pkt["receiver_id"],
            receiver_type=pkt["sender_type"], receiver_id=pkt["sender_id"],
            cmd_set=pkt["cmd_set"], cmd_id=pkt["cmd_id"],
            payload=bytes([0x00]),  # status=0 (success)
            cmd_type=CMD_TYPE_ACK,
        )

    def start_video(self):
        """Send Mimo startup commands to trigger video streaming.

        From VPN capture of actual Mimo app (March 2026):
        1. PUSH General:0x88 to DM368(1) - registration with timestamp
        2. WRITE General:0x81 to DM368(2) - 64B APP identity
        3. WRITE General:0x82 to DM368(2) - 1B video enable
        4. PUSH General:0x4f to DM368(2) - 9B video heartbeat (01 00 XX 00 00 ff ff ff ff)
           This is the actual video heartbeat at ~5Hz, NOT Camera:0x8e!
        """
        logger.info("Sending Mimo startup commands (from VPN capture)...")

        # Step 1: DM368(1) registration heartbeat (0x88)
        # Mimo payload: 17 00 46 23 73 41 50 50 00 00 00 00 00 02
        self.send_duml_push(
            receiver_type=8, receiver_id=1,
            cmd_set=0x00, cmd_id=0x88,
            payload=bytes([0x17, 0x00, 0x46, 0x23, 0x73, 0x41, 0x50, 0x50,
                           0x00, 0x00, 0x00, 0x00, 0x00, 0x02]),
        )

        # Step 2: DM368(2) registration - cmd_type=4 (WRITE)
        # MUST send 0x81 first, then 0x82 (Mimo order)
        self._send_dm368_register()

        # Step 3: Start the video heartbeat thread (General:0x4f to DM368(2))
        self._video_heartbeat_running = True
        self._video_hb_counter = 0
        self._video_hb_thread = threading.Thread(
            target=self._video_heartbeat_loop, daemon=True
        )
        self._video_hb_thread.start()
        logger.info("Video heartbeat started (General:0x4f to DM368(2))")

    def _send_dm368_register(self):
        """Send DM368(2) registration commands with cmd_type=4 (WRITE).
        Mimo sends 0x81 first, then 0x82."""
        # cmd=0x81: announce app identity (FIRST in Mimo)
        app_payload = bytearray(64)
        app_payload[0] = 0x00
        app_payload[1:4] = b"APP"
        self.send_duml(
            sender_type=2, sender_id=0,
            receiver_type=8, receiver_id=2,  # DM368(2)
            cmd_set=0x00, cmd_id=0x81,
            payload=bytes(app_payload),
            cmd_type=CMD_TYPE_WRITE,
        )
        # cmd=0x82: register as video client (SECOND in Mimo)
        self.send_duml(
            sender_type=2, sender_id=0,
            receiver_type=8, receiver_id=2,  # DM368(2)
            cmd_set=0x00, cmd_id=0x82,
            payload=bytes([0x00]),
            cmd_type=CMD_TYPE_WRITE,
        )

    def _video_heartbeat_loop(self):
        """Send video heartbeat: General:0x4f to DM368(2) at ~5Hz.

        From Mimo VPN capture:
        - Payload: 01 00 XX 00 00 ff ff ff ff (XX = counter, increments each pair)
        - Each counter value sent twice (original + retry), ~200ms apart
        - DM368 registration (0x81/0x82) re-sent every ~2 seconds
        - DM368(1) 0x88 heartbeat re-sent every ~5 seconds
        """
        tick = 0
        while self._running and getattr(self, '_video_heartbeat_running', False):
            try:
                # Video heartbeat: General:0x4f to DM368(2) - THE key command
                counter = self._video_hb_counter & 0xFF
                hb_payload = struct.pack("<BBBBBI", 0x01, 0x00, counter, 0x00, 0x00, 0xFFFFFFFF)
                self.send_duml_push(
                    receiver_type=8, receiver_id=2,  # DM368(2)
                    cmd_set=0x00, cmd_id=0x4f,
                    payload=hb_payload,
                )
                # Mimo sends each counter twice, then increments
                if tick % 2 == 1:
                    self._video_hb_counter += 1

                # Re-send DM368 registration every ~2 seconds (10 ticks at 5Hz)
                if tick % 10 == 0 and tick > 0:
                    self._send_dm368_register()

                # Re-send DM368(1) 0x88 heartbeat every ~5 seconds
                if tick % 25 == 0 and tick > 0:
                    self.send_duml_push(
                        receiver_type=8, receiver_id=1,
                        cmd_set=0x00, cmd_id=0x88,
                        payload=bytes([0x17, 0x00, 0x68, 0x23, 0x69, 0x41, 0x50, 0x50,
                                       0x00, 0x00, 0x00, 0x00, 0x00, 0x02]),
                    )

                tick += 1
                time.sleep(0.2)  # ~5Hz (Mimo sends 0x4f every ~200ms)
            except Exception as e:
                logger.warning(f"Video heartbeat error (continuing): {e}")
                time.sleep(0.5)

    def send_duml_push(self, receiver_type: int, receiver_id: int,
                       cmd_set: int, cmd_id: int,
                       payload: bytes = b"") -> int:
        """Convenience: send DUML PUSH from App(2,0) to receiver."""
        return self.send_duml(
            sender_type=2, sender_id=0,
            receiver_type=receiver_type, receiver_id=receiver_id,
            cmd_set=cmd_set, cmd_id=cmd_id,
            payload=payload, cmd_type=CMD_TYPE_PUSH,
        )

    def send_duml_req(self, receiver_type: int, receiver_id: int,
                      cmd_set: int, cmd_id: int,
                      payload: bytes = b"") -> int:
        """Convenience: send DUML REQ from App(2,0) to receiver."""
        return self.send_duml(
            sender_type=2, sender_id=0,
            receiver_type=receiver_type, receiver_id=receiver_id,
            cmd_set=cmd_set, cmd_id=cmd_id,
            payload=payload, cmd_type=CMD_TYPE_REQ,
        )
