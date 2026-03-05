"""BLE communication with DJI Pocket 3.

Based on Moblin iOS app: https://github.com/eerimoq/moblin
Uses bleak for cross-platform BLE.
"""

import asyncio
import struct
import logging

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData

logger = logging.getLogger("pocket3.ble")

# BLE UUIDs
FFF4_UUID = "0000fff4-0000-1000-8000-00805f9b34fb"  # Notify (camera -> app)
FFF5_UUID = "0000fff5-0000-1000-8000-00805f9b34fb"  # Write (app -> camera)

# DJI manufacturer IDs
DJI_COMPANY_ID = bytes([0xAA, 0x08])
XTRA_COMPANY_ID = bytes([0xAA, 0xF7])

# Model bytes at offset 2-3 in manufacturer data
MODELS = {
    b"\x10\x00": "OsmoAction2",
    b"\x12\x00": "OsmoAction3",
    b"\x14\x00": "OsmoAction4",
    b"\x15\x00": "OsmoAction5Pro",
    b"\x17\x00": "Osmo360",
    b"\x18\x00": "OsmoAction6",
    b"\x20\x00": "OsmoPocket3",
}

# Transaction IDs and targets (from Moblin)
PAIR_ID = 0x8092
PAIR_TARGET = 0x0702
PAIR_TYPE = 0x450740

STOP_STREAM_ID = 0xEAC8
STOP_STREAM_TARGET = 0x0802
STOP_STREAM_TYPE = 0x8E0240

PREPARE_LIVESTREAM_ID = 0x8C12
PREPARE_LIVESTREAM_TARGET = 0x0802
PREPARE_LIVESTREAM_TYPE = 0xE10240

SETUP_WIFI_ID = 0x8C19
SETUP_WIFI_TARGET = 0x0702
SETUP_WIFI_TYPE = 0x470740

START_STREAM_ID = 0x8C2C
START_STREAM_TARGET = 0x0802
START_STREAM_TYPE = 0x780840

# Pair payload hash (from Moblin)
PAIR_HASH = bytes([
    0x20, 0x32, 0x38, 0x34, 0x61, 0x65, 0x35, 0x62,
    0x38, 0x64, 0x37, 0x36, 0x62, 0x33, 0x33, 0x37,
    0x35, 0x61, 0x30, 0x34, 0x61, 0x36, 0x34, 0x31,
    0x37, 0x61, 0x64, 0x37, 0x31, 0x62, 0x65, 0x61,
    0x33,
])

PAIR_PIN = "mbln"


def _ble_crc8(data: bytes) -> int:
    """CRC8 for BLE messages (poly=0x31, init=0xEE, refIn/refOut=true)."""
    crc = 0x77  # After reflection of init=0xEE
    table = [
        0x00, 0x5E, 0xBC, 0xE2, 0x61, 0x3F, 0xDD, 0x83,
        0xC2, 0x9C, 0x7E, 0x20, 0xA3, 0xFD, 0x1F, 0x41,
        0x9D, 0xC3, 0x21, 0x7F, 0xFC, 0xA2, 0x40, 0x1E,
        0x5F, 0x01, 0xE3, 0xBD, 0x3E, 0x60, 0x82, 0xDC,
        0x23, 0x7D, 0x9F, 0xC1, 0x42, 0x1C, 0xFE, 0xA0,
        0xE1, 0xBF, 0x5D, 0x03, 0x80, 0xDE, 0x3C, 0x62,
        0xBE, 0xE0, 0x02, 0x5C, 0xDF, 0x81, 0x63, 0x3D,
        0x7C, 0x22, 0xC0, 0x9E, 0x1D, 0x43, 0xA1, 0xFF,
        0x46, 0x18, 0xFA, 0xA4, 0x27, 0x79, 0x9B, 0xC5,
        0x84, 0xDA, 0x38, 0x66, 0xE5, 0xBB, 0x59, 0x07,
        0xDB, 0x85, 0x67, 0x39, 0xBA, 0xE4, 0x06, 0x58,
        0x19, 0x47, 0xA5, 0xFB, 0x78, 0x26, 0xC4, 0x9A,
        0x65, 0x3B, 0xD9, 0x87, 0x04, 0x5A, 0xB8, 0xE6,
        0xA7, 0xF9, 0x1B, 0x45, 0xC6, 0x98, 0x7A, 0x24,
        0xF8, 0xA6, 0x44, 0x1A, 0x99, 0xC7, 0x25, 0x7B,
        0x3A, 0x64, 0x86, 0xD8, 0x5B, 0x05, 0xE7, 0xB9,
        0x8C, 0xD2, 0x30, 0x6E, 0xED, 0xB3, 0x51, 0x0F,
        0x4E, 0x10, 0xF2, 0xAC, 0x2F, 0x71, 0x93, 0xCD,
        0x11, 0x4F, 0xAD, 0xF3, 0x70, 0x2E, 0xCC, 0x92,
        0xD3, 0x8D, 0x6F, 0x31, 0xB2, 0xEC, 0x0E, 0x50,
        0xAF, 0xF1, 0x13, 0x4D, 0xCE, 0x90, 0x72, 0x2C,
        0x6D, 0x33, 0xD1, 0x8F, 0x0C, 0x52, 0xB0, 0xEE,
        0x32, 0x6C, 0x8E, 0xD0, 0x53, 0x0D, 0xEF, 0xB1,
        0xF0, 0xAE, 0x4C, 0x12, 0x91, 0xCF, 0x2D, 0x73,
        0xCA, 0x94, 0x76, 0x28, 0xAB, 0xF5, 0x17, 0x49,
        0x08, 0x56, 0xB4, 0xEA, 0x69, 0x37, 0xD5, 0x8B,
        0x57, 0x09, 0xEB, 0xB5, 0x36, 0x68, 0x8A, 0xD4,
        0x95, 0xCB, 0x29, 0x77, 0xF4, 0xAA, 0x48, 0x16,
        0xE9, 0xB7, 0x55, 0x0B, 0x88, 0xD6, 0x34, 0x6A,
        0x2B, 0x75, 0x97, 0xC9, 0x4A, 0x14, 0xF6, 0xA8,
        0x74, 0x2A, 0xC8, 0x96, 0x15, 0x4B, 0xA9, 0xF7,
        0xB6, 0xE8, 0x0A, 0x54, 0xD7, 0x89, 0x6B, 0x35,
    ]
    for b in data:
        crc = table[(crc ^ b) & 0xFF]
    return crc


def _ble_crc16(data: bytes) -> int:
    """CRC16 for BLE messages (poly=0x1021, init=0x496C, refIn/refOut=true)."""
    crc = 0x3692  # Reflected init of 0x496C
    table = [
        0x0000, 0x1189, 0x2312, 0x329B, 0x4624, 0x57AD, 0x6536, 0x74BF,
        0x8C48, 0x9DC1, 0xAF5A, 0xBED3, 0xCA6C, 0xDBE5, 0xE97E, 0xF8F7,
        0x1081, 0x0108, 0x3393, 0x221A, 0x56A5, 0x472C, 0x75B7, 0x643E,
        0x9CC9, 0x8D40, 0xBFDB, 0xAE52, 0xDAED, 0xCB64, 0xF9FF, 0xE876,
        0x2102, 0x308B, 0x0210, 0x1399, 0x6726, 0x76AF, 0x4434, 0x55BD,
        0xAD4A, 0xBCC3, 0x8E58, 0x9FD1, 0xEB6E, 0xFAE7, 0xC87C, 0xD9F5,
        0x3183, 0x200A, 0x1291, 0x0318, 0x77A7, 0x662E, 0x54B5, 0x453C,
        0xBDCB, 0xAC42, 0x9ED9, 0x8F50, 0xFBEF, 0xEA66, 0xD8FD, 0xC974,
        0x4204, 0x538D, 0x6116, 0x709F, 0x0420, 0x15A9, 0x2732, 0x36BB,
        0xCE4C, 0xDFC5, 0xED5E, 0xFCD7, 0x8868, 0x99E1, 0xAB7A, 0xBAF3,
        0x5285, 0x430C, 0x7197, 0x601E, 0x14A1, 0x0528, 0x37B3, 0x263A,
        0xDECD, 0xCF44, 0xFDDF, 0xEC56, 0x98E9, 0x8960, 0xBBFB, 0xAA72,
        0x6306, 0x728F, 0x4014, 0x519D, 0x2522, 0x34AB, 0x0630, 0x17B9,
        0xEF4E, 0xFEC7, 0xCC5C, 0xDDD5, 0xA96A, 0xB8E3, 0x8A78, 0x9BF1,
        0x7387, 0x620E, 0x5095, 0x411C, 0x35A3, 0x242A, 0x16B1, 0x0738,
        0xFFCF, 0xEE46, 0xDCDD, 0xCD54, 0xB9EB, 0xA862, 0x9AF9, 0x8B70,
        0x8408, 0x9581, 0xA71A, 0xB693, 0xC22C, 0xD3A5, 0xE13E, 0xF0B7,
        0x0840, 0x19C9, 0x2B52, 0x3ADB, 0x4E64, 0x5FED, 0x6D76, 0x7CFF,
        0x9489, 0x8500, 0xB79B, 0xA612, 0xD2AD, 0xC324, 0xF1BF, 0xE036,
        0x18C1, 0x0948, 0x3BD3, 0x2A5A, 0x5EE5, 0x4F6C, 0x7DF7, 0x6C7E,
        0xA50A, 0xB483, 0x8618, 0x9791, 0xE32E, 0xF2A7, 0xC03C, 0xD1B5,
        0x2942, 0x38CB, 0x0A50, 0x1BD9, 0x6F66, 0x7EEF, 0x4C74, 0x5DFD,
        0xB58B, 0xA402, 0x9699, 0x8710, 0xF3AF, 0xE226, 0xD0BD, 0xC134,
        0x39C3, 0x284A, 0x1AD1, 0x0B58, 0x7FE7, 0x6E6E, 0x5CF5, 0x4D7C,
        0xC60C, 0xD785, 0xE51E, 0xF497, 0x8028, 0x91A1, 0xA33A, 0xB2B3,
        0x4A44, 0x5BCD, 0x6956, 0x78DF, 0x0C60, 0x1DE9, 0x2F72, 0x3EFB,
        0xD68D, 0xC704, 0xF59F, 0xE416, 0x90A9, 0x8120, 0xB3BB, 0xA232,
        0x5AC5, 0x4B4C, 0x79D7, 0x685E, 0x1CE1, 0x0D68, 0x3FF3, 0x2E7A,
        0xE70E, 0xF687, 0xC41C, 0xD595, 0xA12A, 0xB0A3, 0x8238, 0x93B1,
        0x6B46, 0x7ACF, 0x4854, 0x59DD, 0x2D62, 0x3CEB, 0x0E70, 0x1FF9,
        0xF78F, 0xE606, 0xD49D, 0xC514, 0xB1AB, 0xA022, 0x92B9, 0x8330,
        0x7BC7, 0x6A4E, 0x58D5, 0x495C, 0x3DE3, 0x2C6A, 0x1EF1, 0x0F78,
    ]
    for b in data:
        crc = (crc >> 8) ^ table[(crc ^ b) & 0xFF]
    return crc & 0xFFFF


def _pack_string(s: str) -> bytes:
    data = s.encode("utf-8")
    return bytes([len(data)]) + data


def build_ble_message(target: int, msg_id: int, msg_type: int, payload: bytes) -> bytes:
    """Build a DJI BLE message."""
    total_len = 13 + len(payload)
    buf = bytearray()
    buf.append(0x55)
    buf.append(total_len & 0xFF)
    buf.append(0x04)  # version
    buf.append(_ble_crc8(bytes(buf[:3])))
    buf += struct.pack("<H", target)
    buf += struct.pack("<H", msg_id)
    # type is 24-bit LE
    buf.append(msg_type & 0xFF)
    buf.append((msg_type >> 8) & 0xFF)
    buf.append((msg_type >> 16) & 0xFF)
    buf += payload
    buf += struct.pack("<H", _ble_crc16(bytes(buf)))
    return bytes(buf)


def parse_ble_message(data: bytes) -> dict:
    """Parse a DJI BLE message."""
    if len(data) < 13 or data[0] != 0x55:
        raise ValueError(f"Invalid BLE message: {data.hex()}")
    length = data[1]
    if len(data) != length:
        raise ValueError(f"Length mismatch: got {len(data)}, expected {length}")
    if data[2] != 0x04:
        raise ValueError(f"Bad version: {data[2]}")
    if _ble_crc8(data[:3]) != data[3]:
        raise ValueError("CRC8 mismatch")
    crc16 = struct.unpack_from("<H", data, length - 2)[0]
    if _ble_crc16(data[:length - 2]) != crc16:
        raise ValueError("CRC16 mismatch")
    target = struct.unpack_from("<H", data, 4)[0]
    msg_id = struct.unpack_from("<H", data, 6)[0]
    msg_type = data[8] | (data[9] << 8) | (data[10] << 16)
    payload = data[11:length - 2]
    return {"target": target, "id": msg_id, "type": msg_type, "payload": payload}


def is_dji_device(advertisement_data: AdvertisementData) -> tuple:
    """Check if BLE advertisement is from a DJI device. Returns (is_dji, model_name)."""
    mfr = advertisement_data.manufacturer_data
    for company_id, data in mfr.items():
        full = struct.pack("<H", company_id) + data
        if full[:2] in (DJI_COMPANY_ID, XTRA_COMPANY_ID) and len(full) >= 4:
            model = MODELS.get(full[2:4], "Unknown")
            return True, model
    return False, None


class DjiPocket3BLE:
    """BLE controller for DJI Pocket 3."""

    def __init__(self):
        self.client: BleakClient | None = None
        self.device: BLEDevice | None = None
        self.model: str = "Unknown"
        self._response_event = asyncio.Event()
        self._last_response: dict | None = None
        self._battery: int | None = None

    async def scan(self, timeout: float = 10.0) -> list:
        """Scan for DJI devices. Returns list of (device, model)."""
        found = {}
        found_event = asyncio.Event()
        logger.info("Scanning for DJI BLE devices...")

        def callback(device: BLEDevice, adv: AdvertisementData):
            is_dji, model = is_dji_device(adv)
            if is_dji and device.address not in found:
                logger.info(f"  Found: {device.name} ({device.address}) - {model}")
                found[device.address] = (device, model)
                if model == "OsmoPocket3":
                    found_event.set()

        scanner = BleakScanner(detection_callback=callback)
        await scanner.start()
        try:
            await asyncio.wait_for(found_event.wait(), timeout=timeout)
            await asyncio.sleep(0.5)  # Brief extra scan for other devices
        except asyncio.TimeoutError:
            pass
        await scanner.stop()
        result = list(found.values())
        logger.info(f"Scan complete: {len(result)} DJI devices found")
        return result

    async def connect(self, device: BLEDevice, model: str = "Unknown"):
        """Connect to a DJI device."""
        self.device = device
        self.model = model
        logger.info(f"Connecting to {device.name} ({device.address})...")
        self.client = BleakClient(device)
        await self.client.connect()
        logger.info("Connected. Discovering services...")

        # Subscribe to FFF4 notifications
        await self.client.start_notify(FFF4_UUID, self._on_notification)
        logger.info("Subscribed to FFF4 notifications")

    def _on_notification(self, sender, data: bytearray):
        """Handle BLE notification from camera."""
        try:
            msg = parse_ble_message(bytes(data))
            logger.debug(f"RX BLE: target={msg['target']:#06x} id={msg['id']:#06x} "
                         f"type={msg['type']:#08x} payload={msg['payload'].hex()}")

            # Battery info (type 0x020D00)
            if msg["type"] == 0x020D00 and len(msg["payload"]) >= 21:
                self._battery = msg["payload"][20]
                logger.info(f"Battery: {self._battery}%")

            self._last_response = msg
            self._response_event.set()
        except Exception as e:
            logger.warning(f"RX BLE corrupt: {bytes(data).hex()} - {e}")

    async def _send(self, target: int, msg_id: int, msg_type: int, payload: bytes):
        """Send a BLE message."""
        msg = build_ble_message(target, msg_id, msg_type, payload)
        logger.debug(f"TX BLE: {msg.hex()}")
        await self.client.write_gatt_char(FFF5_UUID, msg, response=False)

    async def _send_and_wait(self, target, msg_id, msg_type, payload, timeout=10.0) -> dict:
        """Send message and wait for response with matching id."""
        self._response_event.clear()
        await self._send(target, msg_id, msg_type, payload)
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"No response for id={msg_id:#06x}")
            self._response_event.clear()
            try:
                await asyncio.wait_for(self._response_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                raise TimeoutError(f"No response for id={msg_id:#06x}")
            if self._last_response and self._last_response["id"] == msg_id:
                return self._last_response

    async def pair(self) -> bool:
        """Pair with the camera."""
        logger.info("Pairing...")
        payload = PAIR_HASH + _pack_string(PAIR_PIN)
        resp = await self._send_and_wait(PAIR_TARGET, PAIR_ID, PAIR_TYPE, payload)
        if resp["payload"] == bytes([0, 1]):
            logger.info("Already paired!")
            return True
        logger.info("Pair request sent, camera should confirm")
        return True

    async def stop_streaming(self):
        """Send stop streaming command (used as cleanup)."""
        logger.info("Sending stop streaming (cleanup)...")
        payload = bytes([0x01, 0x01, 0x1A, 0x00, 0x01, 0x02])
        try:
            resp = await self._send_and_wait(
                STOP_STREAM_TARGET, STOP_STREAM_ID, STOP_STREAM_TYPE, payload, timeout=5.0
            )
            logger.info(f"Stop streaming response: {resp['payload'].hex()}")
        except TimeoutError:
            logger.warning("Stop streaming timed out (may be OK)")

    async def prepare_livestream(self):
        """Tell camera to prepare for livestreaming."""
        logger.info("Preparing livestream...")
        payload = bytes([0x1A])
        resp = await self._send_and_wait(
            PREPARE_LIVESTREAM_TARGET, PREPARE_LIVESTREAM_ID, PREPARE_LIVESTREAM_TYPE, payload
        )
        logger.info(f"Prepare livestream response: {resp['payload'].hex()}")

    async def setup_wifi(self, ssid: str, password: str):
        """Tell camera to connect to a WiFi network."""
        logger.info(f"Setting up WiFi: SSID={ssid}")
        payload = _pack_string(ssid) + _pack_string(password)
        resp = await self._send_and_wait(
            SETUP_WIFI_TARGET, SETUP_WIFI_ID, SETUP_WIFI_TYPE, payload
        )
        if resp["payload"] == bytes([0x00, 0x00]):
            logger.info("WiFi setup OK!")
        else:
            logger.error(f"WiFi setup failed: {resp['payload'].hex()}")
            raise RuntimeError("WiFi setup failed")

    async def start_rtmp_stream(self, rtmp_url: str, resolution: str = "1080p",
                                 fps: int = 30, bitrate_kbps: int = 6000):
        """Start RTMP streaming from camera."""
        res_byte = {"480p": 0x47, "720p": 0x04, "1080p": 0x0A}.get(resolution, 0x0A)
        fps_byte = {25: 2, 30: 3}.get(fps, 3)
        url_data = rtmp_url.encode("utf-8")

        payload = bytearray()
        payload.append(0x00)  # payload1
        payload.append(0x2E)  # byte1 (0x2A for OA5+)
        payload.append(0x00)  # payload2
        payload.append(res_byte)
        payload += struct.pack("<H", bitrate_kbps)
        payload += bytes([0x02, 0x00])  # payload3
        payload.append(fps_byte)
        payload += bytes([0x00, 0x00, 0x00])  # payload4
        payload.append(len(url_data))
        payload.append(0x00)
        payload += url_data

        logger.info(f"Starting RTMP stream to {rtmp_url}")
        resp = await self._send_and_wait(
            START_STREAM_TARGET, START_STREAM_ID, START_STREAM_TYPE, bytes(payload)
        )
        logger.info(f"Start stream response: {resp['payload'].hex()}")

    async def activate_and_pair(self) -> bool:
        """Pair with camera. WiFi AP activates ~20s after pairing."""
        await self.pair()
        logger.info("Paired. WiFi AP will activate in ~20s.")
        return True

    async def disconnect(self):
        """Disconnect BLE."""
        if self.client and self.client.is_connected:
            await self.client.disconnect()
            logger.info("BLE disconnected")

    @property
    def battery(self) -> int | None:
        return self._battery
