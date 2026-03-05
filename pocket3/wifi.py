"""WiFi connection manager for DJI Pocket 3.

Handles OS-level WiFi connection to the camera's AP.
macOS: uses networksetup
Linux: uses nmcli
"""

import subprocess
import platform
import time
import logging
import socket

logger = logging.getLogger("pocket3.wifi")

DEFAULT_SSID = "OsmoPocket3-D6B1"
DEFAULT_PASSWORD = "hRiGeGppd8ip"
CAMERA_IP = "192.168.2.1"


def _run(cmd: list[str], check: bool = False) -> subprocess.CompletedProcess:
    logger.debug(f"Running: {' '.join(cmd)}")
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def get_wifi_interface() -> str | None:
    """Get the WiFi interface name."""
    system = platform.system()
    if system == "Darwin":
        # macOS: find WiFi interface
        result = _run(["networksetup", "-listallhardwareports"])
        lines = result.stdout.splitlines()
        for i, line in enumerate(lines):
            if "Wi-Fi" in line or "AirPort" in line:
                for j in range(i + 1, min(i + 3, len(lines))):
                    if lines[j].startswith("Device:"):
                        return lines[j].split(":")[1].strip()
        return "en0"  # fallback
    elif system == "Linux":
        result = _run(["ip", "link", "show"])
        for line in result.stdout.splitlines():
            if "wlan" in line or "wlp" in line:
                parts = line.split(":")
                if len(parts) >= 2:
                    return parts[1].strip()
        return "wlan0"
    return None


def get_current_ssid() -> str | None:
    """Get currently connected WiFi SSID."""
    system = platform.system()
    if system == "Darwin":
        iface = get_wifi_interface() or "en0"
        result = _run(["networksetup", "-getairportnetwork", iface])
        if "Current Wi-Fi Network:" in result.stdout:
            return result.stdout.split(":", 1)[1].strip()
        # macOS 15+: try ipconfig for quick check
        result = _run(["ipconfig", "getifaddr", iface])
        if result.returncode == 0 and result.stdout.strip().startswith("192.168.2."):
            return DEFAULT_SSID  # We're on the camera network
    elif system == "Linux":
        result = _run(["nmcli", "-t", "-f", "active,ssid", "dev", "wifi"])
        for line in result.stdout.splitlines():
            if line.startswith("yes:"):
                return line.split(":", 1)[1]
    return None


def _camera_reachable() -> bool:
    """Quick check if camera responds to ping."""
    result = _run(["ping", "-c", "1", "-t", "2", CAMERA_IP])
    return result.returncode == 0


def connect_wifi(ssid: str = DEFAULT_SSID, password: str = DEFAULT_PASSWORD,
                 timeout: float = 30.0) -> bool:
    """Connect to a WiFi network.

    Returns True if connected successfully.
    """
    current = get_current_ssid()
    if current == ssid:
        logger.info(f"Already connected to {ssid}")
        return True

    logger.info(f"Connecting to WiFi: {ssid}...")
    system = platform.system()

    if system == "Darwin":
        iface = get_wifi_interface() or "en0"
        result = _run([
            "networksetup", "-setairportnetwork", iface, ssid, password
        ])
        if result.returncode != 0:
            logger.error(f"WiFi connect failed: {result.stderr}")
            return False

    elif system == "Linux":
        # Try nmcli
        result = _run(["nmcli", "dev", "wifi", "connect", ssid, "password", password])
        if result.returncode != 0:
            logger.error(f"WiFi connect failed: {result.stderr}")
            return False

    else:
        logger.error(f"Unsupported OS: {system}")
        return False

    # Wait for connection — check both SSID and camera reachability
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(1)
        current_ssid = get_current_ssid()
        if current_ssid == ssid:
            logger.info(f"Connected to {ssid}")
            time.sleep(1)
            return True
        # Fallback: if SSID detection fails but camera is pingable, we're connected
        if _camera_reachable():
            logger.info(f"Connected to {ssid} (detected via ping)")
            return True

    logger.error(f"Timeout connecting to {ssid}")
    return False


def wait_for_camera(timeout: float = 30.0) -> bool:
    """Wait until camera is reachable on 192.168.2.1."""
    logger.info(f"Waiting for camera at {CAMERA_IP}...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.0)
            sock.sendto(b"\x00", (CAMERA_IP, CAMERA_PORT))
            sock.close()
            logger.info("Camera is reachable!")
            return True
        except OSError:
            time.sleep(1)
    logger.error("Camera not reachable")
    return False


CAMERA_PORT = 9004


def disconnect_wifi():
    """Disconnect from current WiFi (useful for cleanup)."""
    system = platform.system()
    if system == "Darwin":
        iface = get_wifi_interface() or "en0"
        _run(["networksetup", "-setairportpower", iface, "off"])
        time.sleep(1)
        _run(["networksetup", "-setairportpower", iface, "on"])
    elif system == "Linux":
        _run(["nmcli", "dev", "disconnect", get_wifi_interface() or "wlan0"])
