"""Gimbal/PTZ control for DJI Pocket 3.

Decoded from PCAP capture:
- Control: DUML set=4, cmd=0x01 (10-byte payload, speed mode)
- Attitude: DUML set=4, cmd=0x05 (49-byte push from camera ~10Hz)
"""

import struct
import threading
import time
import logging

logger = logging.getLogger("pocket3.gimbal")

# DUML device types
DEV_APP = 2
DEV_GIMBAL = 4

# Gimbal command IDs
CMD_GIMBAL_CONTROL = 0x01
CMD_GIMBAL_ATTITUDE = 0x05
CMD_GIMBAL_STATUS = 0x27
CMD_GIMBAL_LIMITS = 0x38
CMD_GIMBAL_CONFIG = 0x50
CMD_GIMBAL_MODE = 0x1C

# Speed center value
SPEED_CENTER = 1024
SPEED_MIN = 0
SPEED_MAX = 2048

# Flags from captured data
CONTROL_FLAGS = 0x8000
CONTROL_EXTRA = 0x0042


class GimbalState:
    """Current gimbal state from attitude pushes."""

    def __init__(self):
        self.yaw = 0.0    # degrees
        self.pitch = 0.0   # degrees
        self.roll = 0.0    # degrees
        self.timestamp = 0
        self._lock = threading.Lock()

    def update(self, payload: bytes):
        """Update from cmd=0x05 attitude push (49 bytes)."""
        if len(payload) < 6:
            return
        with self._lock:
            self.yaw = struct.unpack_from("<h", payload, 0)[0] / 10.0
            self.roll = struct.unpack_from("<h", payload, 2)[0] / 10.0
            self.pitch = struct.unpack_from("<h", payload, 4)[0] / 10.0
            if len(payload) >= 14:
                self.timestamp = struct.unpack_from("<H", payload, 12)[0]

    def __repr__(self):
        return f"Gimbal(yaw={self.yaw:.1f}° pitch={self.pitch:.1f}° roll={self.roll:.1f}°)"


class GimbalController:
    """PTZ gimbal controller for DJI Pocket 3."""

    def __init__(self, udp_client):
        """
        Args:
            udp_client: DjiUdpClient instance
        """
        self.client = udp_client
        self.state = GimbalState()
        self._continuous_yaw = 0.0    # -1.0 to 1.0
        self._continuous_pitch = 0.0  # -1.0 to 1.0
        self._control_thread: threading.Thread | None = None
        self._running = False
        self._send_interval = 0.033  # ~30Hz control rate

        # Register attitude callback
        self.client.register_duml_callback(4, CMD_GIMBAL_ATTITUDE, self._on_attitude)

    def _on_attitude(self, pkt: dict):
        """Handle gimbal attitude push."""
        self.state.update(pkt["payload"])

    def start(self):
        """Start continuous control thread."""
        if self._running:
            return
        self._running = True
        self._control_thread = threading.Thread(target=self._control_loop, daemon=True)
        self._control_thread.start()
        logger.info("Gimbal control started")

    def stop(self):
        """Stop control thread."""
        self._running = False
        if self._control_thread:
            self._control_thread.join(timeout=2.0)
        logger.info("Gimbal control stopped")

    def _control_loop(self):
        """Send continuous gimbal speed commands."""
        while self._running:
            yaw_speed = int(SPEED_CENTER + self._continuous_yaw * (SPEED_MAX - SPEED_CENTER))
            pitch_speed = int(SPEED_CENTER + self._continuous_pitch * (SPEED_MAX - SPEED_CENTER))

            yaw_speed = max(SPEED_MIN, min(SPEED_MAX, yaw_speed))
            pitch_speed = max(SPEED_MIN, min(SPEED_MAX, pitch_speed))

            self._send_control(yaw_speed, 0, pitch_speed)
            time.sleep(self._send_interval)

    def _send_control(self, yaw: int, roll: int, pitch: int):
        """Send raw gimbal control command."""
        payload = struct.pack("<HHHHH", yaw, roll, pitch, CONTROL_FLAGS, CONTROL_EXTRA)
        self.client.send_duml_req(
            receiver_type=DEV_GIMBAL, receiver_id=0,
            cmd_set=4, cmd_id=CMD_GIMBAL_CONTROL,
            payload=payload,
        )

    def set_speed(self, yaw: float = 0.0, pitch: float = 0.0):
        """Set continuous gimbal speed.

        Args:
            yaw: -1.0 (left) to 1.0 (right), 0.0 = stop
            pitch: -1.0 (down) to 1.0 (up), 0.0 = stop
        """
        self._continuous_yaw = max(-1.0, min(1.0, yaw))
        self._continuous_pitch = max(-1.0, min(1.0, pitch))

    def pan_left(self, speed: float = 0.3):
        """Pan left at given speed (0-1)."""
        self.set_speed(yaw=-speed)

    def pan_right(self, speed: float = 0.3):
        """Pan right at given speed (0-1)."""
        self.set_speed(yaw=speed)

    def tilt_up(self, speed: float = 0.3):
        """Tilt up at given speed (0-1)."""
        self.set_speed(pitch=speed)

    def tilt_down(self, speed: float = 0.3):
        """Tilt down at given speed (0-1)."""
        self.set_speed(pitch=-speed)

    def stop_movement(self):
        """Stop all gimbal movement."""
        self.set_speed(0.0, 0.0)

    def nudge(self, yaw: float = 0.0, pitch: float = 0.0, duration: float = 0.5):
        """Move gimbal for a short duration then stop.

        Args:
            yaw: -1.0 to 1.0
            pitch: -1.0 to 1.0
            duration: seconds
        """
        self.set_speed(yaw, pitch)
        threading.Timer(duration, self.stop_movement).start()
