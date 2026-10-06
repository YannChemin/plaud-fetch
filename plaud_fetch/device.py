"""Bluetooth access to the Note Pro: scan, pair, list, download, delete, release.

Built on ``plaud_pc.client.PlaudBleClient`` (plaud-direct-pc-research), which
implements the device's RSA + ChaCha20-Poly1305 handshake and the file
transfer. :class:`PlaudSession` keeps one connection open for a whole fetch
run, since every connection repeats the handshake.

Handshake, as observed on a Note Pro V1.7.0:

1. The client sends its PLAUD-issued serial signature (command 65040, or 65056
   to force-clear the enrolled client key).
2. The device answers 65041 when no client key is enrolled (the client then
   sends its RSA public key), then 65042 with session keys encrypted to the
   enrolled key.
3. The client sends its bind identity (CMD 1). The device stays silent and
   drops the link when another account still owns it.
"""

from __future__ import annotations

import asyncio
import struct
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Self

from cryptography.exceptions import InvalidTag
from plaud_pc.advertisement import parse_manufacturer_data
from plaud_pc.client import NOTIFY_UUID, SERVICE_UUID, DeviceFile, PlaudBleClient, ReadOnlySnapshot
from plaud_pc.protocol import ProtocolError

from .meeting import local_start, recording_name

# Delete one recording, as Plaud SDK v1.0.57 (PlaudDeviceAgent.deleteFile)
# sends it: request CMD 30 = [session_id:u32 LE]; the device answers on CMD 31
# with [session_id:u32 LE][status:u8] (port version >= 7, else [status:u8]);
# status 0 is success. CMD 29 stops a transfer; never confuse them.
CMD_DELETE = 30
CMD_DELETE_RESPONSE = 31
DELETE_PAYLOAD_FORMAT = "<I"
# Pre-handshake replies to the signature.
KEY_REQUESTED = 65041  # No client key enrolled: send one.


class DeviceError(RuntimeError):
    """The device cannot be reached, refused a session or refused a command."""


@dataclass(frozen=True)
class Recording:
    """One recording in the device's file list."""

    file: DeviceFile

    @property
    def session_id(self) -> int:
        """Device session id: the recording start in Unix seconds."""
        return self.file.session_id

    @property
    def start(self) -> datetime:
        """Recording start, local time."""
        return local_start(self.file.session_id)

    @property
    def name(self) -> str:
        """File name stem, ``YYYY-MM-DD_HH_MM_SS``."""
        return recording_name(self.start)


def parse_delete_response(payload: bytes, port_version: int) -> tuple[int | None, int]:
    """Read the device's answer to a delete.

    :param payload: CMD 31 payload, after the command header
    :param port_version: protocol version reported by the handshake
    :returns: (session id, status); the session id is ``None`` before port 7
    :raises DeviceError: when the payload is too short
    """
    if port_version >= 7:
        if len(payload) < 5:
            raise DeviceError("short delete response")
        return struct.unpack_from("<IB", payload)
    if not payload:
        raise DeviceError("empty delete response")
    return None, payload[0]


def binary_serial(data: bytes) -> str:
    """Serial of a Note Pro advertisement, which carries it as raw bytes.

    Layout (as plaud_pc.advertisement): [2][project:u16][ver size][type]
    [version][serial size][serial]. The Note Pro (project 881) sends the
    serial as bytes whose hex digits are the serial number, e.g. 88 10 b3 ...
    for 8810B3...; the bridge parser expects ASCII and finds nothing.

    :param data: manufacturer data of the advertisement
    :returns: the serial, or an empty string for another layout
    """
    try:
        if data[0] != 2:
            return ""
        index = 3
        version_size = data[index]
        index += 1 + version_size
        size = data[index]
        raw = data[index + 1 : index + 1 + size]
    except IndexError:
        return ""
    serial = raw.hex().upper()
    return serial if len(raw) == size and serial.startswith("881") else ""


def advertised_serial(advertisement: Any) -> str:
    """Serial number in a Plaud advertisement, ASCII (Note) or binary (Note Pro).

    :param advertisement: bleak ``AdvertisementData``
    :returns: the serial, or an empty string when none is found
    """
    values = advertisement.manufacturer_data
    parsed = parse_manufacturer_data(values)
    if parsed:
        return parsed.serial_number
    return binary_serial(next(iter(values.values()))) if values else ""


def same_serial(left: str, right: str) -> bool:
    """Compare serial numbers, ignoring case; an empty serial never matches."""
    return bool(left) and left.upper() == right.upper()


def is_plaud(device: Any, advertisement: Any) -> bool:
    """Whether an advertisement comes from a Plaud recorder (service UUID or name)."""
    name = device.name or advertisement.local_name or ""
    uuids = {value.lower() for value in advertisement.service_uuids}
    return SERVICE_UUID in uuids or "plaud" in name.lower()


async def scan(seconds: float) -> list[tuple[str, str, str, int]]:
    """List Plaud recorders advertising nearby, without connecting.

    :param seconds: scan duration
    :returns: (address, name, serial, RSSI) rows, strongest signal first
    """
    from bleak import BleakScanner

    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    rows = []
    for address, (device, advertisement) in found.items():
        if is_plaud(device, advertisement):
            name = device.name or advertisement.local_name or "Plaud device"
            rows.append((address, name, advertised_serial(advertisement), advertisement.rssi))
    return sorted(rows, key=lambda row: row[3], reverse=True)


async def _signature_reply(material: dict[str, Any], *, force_clear: bool) -> int | None:
    """Send only the signature and return the device's first reply command.

    Never sends a client key: on 65041 (no key enrolled, send one) it just
    disconnects.

    :param material: device identity
    :param force_clear: send the signature as force-clear (65056)
    :returns: the reply command (65041 or 65042), or ``None`` without a reply
    :raises DeviceError: when the device is not advertising
    """
    from bleak import BleakClient, BleakScanner
    from plaud_pc.client import WRITE_UUID
    from plaud_pc.protocol import signature_chunks

    probe = PlaudSession(material)
    device = await BleakScanner.find_device_by_filter(probe._is_our_device, timeout=30.0)
    if device is None:
        raise DeviceError(f"no advertisement from serial ...{probe._serial[-4:]}")
    replies: asyncio.Queue[bytes] = asyncio.Queue()
    async with BleakClient(device, timeout=30.0) as ble:
        await ble.start_notify(NOTIFY_UUID, lambda _, data: replies.put_nowait(bytes(data)))
        for frame in signature_chunks(str(material["signature"]), force_clear=force_clear):
            await ble.write_gatt_char(WRITE_UUID, frame, response=True)
        try:
            packet = await asyncio.wait_for(replies.get(), 20.0)
        except TimeoutError:
            return None
    return struct.unpack_from("<H", packet)[0] if len(packet) >= 2 else None


async def release(material: dict[str, Any]) -> bool:
    """Remove this computer's client key from the device, enrolling none.

    A force-clear signature (65056) makes the device drop its enrolled key and
    ask for a new one (65041); we disconnect without sending any. A normal
    signature then confirms the result: 65041 means no key is enrolled, and the
    owner's phone app can pair again (this gave the device back to the iPhone
    app on 2026-10-06). The owner and the recordings are not touched.

    :param material: device identity
    :returns: whether the device now holds no client key
    """
    await _signature_reply(material, force_clear=True)
    await asyncio.sleep(5.0)
    return await _signature_reply(material, force_clear=False) == KEY_REQUESTED


class PlaudSession:
    """An authenticated BLE session, used as ``async with PlaudSession(material) as s:``.

    Entering scans for the provisioned device (by serial), connects, runs the
    handshake and binds; leaving disconnects.

    :param material: device identity (:func:`plaud_fetch.credentials.load_material`)
    :param scan_seconds: how long the first scan waits for an advertisement
    :param force_clear: send the signature as force-clear, replacing the
        device's enrolled client key; attempted once, never retried
    """

    def __init__(
        self, material: dict[str, Any], *, scan_seconds: float = 30.0, force_clear: bool = False
    ) -> None:
        self._client = PlaudBleClient(material)
        self._serial = str(material["sn"])
        self._scan_seconds = scan_seconds
        self._force_clear = force_clear
        self._ble: Any = None
        self.name = ""
        self.port_version = 0

    async def __aenter__(self) -> Self:
        """Connect, authenticate and bind.

        :raises DeviceError: when the device is out of reach, still paired with
            another client key, or owned by another account
        """
        from bleak import BleakClient, BleakScanner
        from bleak.exc import BleakError

        last_error: Exception | None = None
        # A force-clear changes the device's pairing: never repeat it blindly.
        for attempt in range(1 if self._force_clear else 3):
            device = await BleakScanner.find_device_by_filter(
                self._is_our_device,
                timeout=self._scan_seconds if attempt == 0 else 15.0,
            )
            if device is None:
                last_error = TimeoutError(f"no advertisement from serial ...{self._serial[-4:]}")
                continue
            self._client._session = None  # Judge each attempt on its own.
            ble = BleakClient(device, timeout=30.0)
            try:
                await ble.connect()
                await ble.start_notify(NOTIFY_UUID, self._on_notification)
                _, self.port_version = await self._authenticate(ble)
            except (InvalidTag, ValueError):
                # RSA "Decryption failed" or a failed challenge: the device did not
                # ask for our RSA key and encrypted the session to a key enrolled
                # earlier (another app's pairing).
                await self._disconnect(ble)
                raise DeviceError(
                    "the Plaud is still paired with another client key (the phone app?): "
                    "it accepted the signature but encrypted the session to that key. "
                    "Unbind it in that app while connected (not just disconnect), then retry."
                ) from None
            except (BleakError, TimeoutError, ProtocolError, OSError) as error:
                last_error = error
                await self._disconnect(ble)
                await asyncio.sleep(2.0)
                continue
            self._ble = ble
            self.name = device.name or "Plaud Note Pro"
            return self
        if self._client._session is not None:
            # Session keys worked but the bind (CMD 1) got no answer: the device
            # keeps another owner, as after a force-clear of a phone pairing.
            raise DeviceError(
                "the Plaud accepted this computer's key but not its bind identity: it is "
                "still owned by another account (the phone app). Let that app reconnect "
                "and unbind the device while connected, then run 'plaud-fetch pair'."
            )
        detail = str(last_error) or type(last_error).__name__
        raise DeviceError(
            f"cannot open an authenticated session with the Plaud: {detail}. "
            "Is Bluetooth on, the device awake and no phone connected to it?"
        )

    async def __aexit__(self, *_: object) -> None:
        """Disconnect."""
        await self._disconnect(self._ble)
        self._ble = None

    async def _authenticate(self, ble: Any) -> tuple[int, int]:
        """The bridge handshake, optionally with the signature sent as force-clear.

        Force-clear is what the Plaud SDK's recoveryConnectBleDevice sends
        (command 0xFE20 = 65056 instead of 65040, same chunks): the device drops
        its enrolled client RSA key and asks for ours (65041) before the session.

        :param ble: connected ``BleakClient`` with notifications started
        :returns: (handshake command, device protocol port version)
        """
        if not self._force_clear:
            return await self._client.authenticate(ble)
        from plaud_pc import client as bridge

        original = bridge.signature_chunks
        bridge.signature_chunks = lambda signature: original(signature, force_clear=True)
        try:
            return await self._client.authenticate(ble)
        finally:
            bridge.signature_chunks = original

    def _is_our_device(self, device: Any, advertisement: Any) -> bool:
        """Only the provisioned device: other Plaud recorders may be in range."""
        return is_plaud(device, advertisement) and same_serial(
            advertised_serial(advertisement), self._serial
        )

    def _on_notification(self, _: Any, data: bytearray) -> None:
        """Queue a GATT notification for the bridge client."""
        self._client._notifications.put_nowait(bytes(data))

    @staticmethod
    async def _disconnect(ble: Any) -> None:
        """Stop notifications and disconnect, tolerating a link already gone."""
        if ble is None:
            return
        try:
            if ble.is_connected:
                await ble.stop_notify(NOTIFY_UUID)
            await ble.disconnect()
        except Exception as error:  # noqa: BLE001  # Best effort: the link may be gone.
            print(f"Warning: disconnect: {error}", file=sys.stderr)

    async def snapshot(self) -> tuple[ReadOnlySnapshot, list[Recording]]:
        """Battery, storage and file list.

        :returns: the raw snapshot and its recordings, oldest first
        """
        snap = await self._client.read_snapshot(self._ble, self.name)
        recordings = sorted((Recording(item) for item in snap.files), key=lambda r: r.session_id)
        return snap, recordings

    async def download(self, recording: Recording, cache_dir: Path) -> Path:
        """Download a recording, resuming a previous partial download.

        :param recording: recording from :meth:`snapshot`
        :param cache_dir: directory for ``<session>.plaud`` (and ``.part``)
        :returns: path of the complete, still encrypted download
        """
        result = await self._client.download_file(
            self._ble, recording.file, cache_dir, port_version=self.port_version
        )
        return result.path

    async def delete(self, recording: Recording) -> None:
        """Delete one recording, then confirm it is gone from the file list.

        :param recording: recording from :meth:`snapshot`
        :raises DeviceError: when the device refuses or still lists it
        """
        session = self._client._session
        if session is None:
            raise DeviceError("session is not authenticated")
        payload = struct.pack(DELETE_PAYLOAD_FORMAT, recording.session_id)
        await self._client._write(self._ble, session.encrypt_command(CMD_DELETE, payload))
        _, response = await self._client._wait_command(CMD_DELETE_RESPONSE, timeout=15.0)
        session_id, status = parse_delete_response(response, self.port_version)
        if session_id not in (None, recording.session_id) or status != 0:
            raise DeviceError(
                f"device refused to delete {recording.name} (session {session_id}, status {status})"
            )
        _, remaining = await self.snapshot()
        if any(item.session_id == recording.session_id for item in remaining):
            raise DeviceError(f"{recording.name} is still listed after delete")
