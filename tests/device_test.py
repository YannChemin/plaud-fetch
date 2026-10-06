import struct
from types import SimpleNamespace

import pytest

from plaud_fetch import device
from plaud_fetch.device import Recording, advertised_serial, binary_serial, same_serial

# Layout of a real Note Pro V1.7.0 advertisement; the serial bytes are made up.
NOTE_PRO_ADV = bytes.fromhex("027103045600070108" + "8810aabbccddeeff" + "441400040101")


def test_note_pro_binary_serial():
    assert binary_serial(NOTE_PRO_ADV) == "8810AABBCCDDEEFF"
    advertisement = SimpleNamespace(manufacturer_data={0x005D: NOTE_PRO_ADV})
    assert advertised_serial(advertisement) == "8810AABBCCDDEEFF"


def test_binary_serial_rejects_other_layouts():
    assert binary_serial(b"") == ""
    assert binary_serial(bytes.fromhex("0271030456")) == ""
    assert binary_serial(bytes.fromhex("027103045600070108" + "1234")) == ""


def test_serial_match_ignores_case_but_not_empty():
    assert same_serial("8810aabb", "8810AABB")
    assert not same_serial("", "")
    assert not same_serial("8810AABB", "8810AABC")


def test_recording_name_from_session_id():
    from plaud_pc.client import DeviceFile

    recording = Recording(DeviceFile(1759152282, 100, 1, False))
    assert recording.start.timestamp() == 1759152282
    assert len(recording.name) == len("2025-09-29_15_24_42")


def test_parse_delete_response():
    assert device.parse_delete_response(bytes.fromhex("2a00000000"), 20) == (42, 0)
    assert device.parse_delete_response(b"\x03", 6) == (None, 3)
    with pytest.raises(device.DeviceError):
        device.parse_delete_response(b"\x2a\x00", 20)


class FakeNotePro:
    """Plays the device side of an authenticated session over a shared key."""

    def __init__(self, files, status=0):
        from plaud_pc.protocol import SessionCrypto

        key, nonce, aad = bytes(range(32)), bytes(12), bytes(12)
        self.host = SessionCrypto(key, nonce, aad)
        self.device = SessionCrypto(key, nonce, aad)
        self.files, self.status, self.sent = list(files), status, []

    def attach(self, session):
        session._client._session = self.host
        session._ble = self
        session.port_version = 20

        async def write(_ble, packet):
            _, body = self.device.decrypt_packet(packet)
            self.sent.append(body)
            command = struct.unpack_from("<H", body, 1)[0]
            if command == 30:
                (sid,) = struct.unpack_from("<I", body, 3)
                if self.status == 0:
                    self.files = [f for f in self.files if f.session_id != sid]
                self.reply(31, struct.pack("<IB", sid, self.status))

        session._client._write = write

    def reply(self, command, payload):
        packet = self.device.encrypt_command(command, payload)
        self.queue.put_nowait(packet)


def run_delete(status, files_after_delete=None):
    import asyncio

    from plaud_pc.client import DeviceFile, ReadOnlySnapshot

    target = DeviceFile(1759152282, 1000, 1, False)
    other = DeviceFile(1759160000, 2000, 1, False)
    fake = FakeNotePro([target, other], status)
    session = device.PlaudSession({"sn": "8810AABB"})

    async def scenario():
        fake.queue = session._client._notifications
        fake.attach(session)

        async def snapshot():
            files = fake.files if files_after_delete is None else files_after_delete
            return ReadOnlySnapshot("", 0, False, 0, 0, 0, tuple(files)), [
                Recording(f) for f in files
            ]

        session.snapshot = snapshot
        await session.delete(Recording(target))

    try:
        asyncio.run(scenario())
        return fake, None
    except device.DeviceError as error:
        return fake, error


def test_delete_sends_cmd30_with_session_and_reads_cmd31():
    fake, error = run_delete(status=0)
    assert error is None
    assert fake.sent == [bytes([1]) + struct.pack("<HI", 30, 1759152282)]
    assert [f.session_id for f in fake.files] == [1759160000]


def test_delete_refused_by_device_raises():
    fake, error = run_delete(status=1)
    assert error is not None and "status 1" in str(error)
    assert len(fake.files) == 2


def test_delete_still_listed_raises():
    from plaud_pc.client import DeviceFile

    _, error = run_delete(status=0, files_after_delete=[DeviceFile(1759152282, 1000, 1, False)])
    assert error is not None and "still listed" in str(error)


class _Stop(Exception):
    pass


def _first_signature_frame(force_clear):
    import asyncio
    import base64

    material = {"sn": "8810AABB", "signature": base64.b64encode(bytes(256)).decode()}
    session = device.PlaudSession(material, force_clear=force_clear)
    frames = []

    async def write(_ble, packet):
        frames.append(packet)
        raise _Stop

    session._client._write = write
    with pytest.raises(_Stop):
        asyncio.run(session._authenticate(None))
    return frames[0]


def test_force_clear_sends_signature_as_65056():
    from plaud_pc import client as bridge
    from plaud_pc.protocol import signature_chunks

    frame = _first_signature_frame(force_clear=True)
    assert struct.unpack_from("<HBB", frame) == (65056, 3, 0)
    assert bridge.signature_chunks is signature_chunks  # Restored after use.


def test_normal_pairing_sends_signature_as_65040():
    frame = _first_signature_frame(force_clear=False)
    assert struct.unpack_from("<HBB", frame) == (65040, 3, 0)


@pytest.mark.parametrize(("check", "released"), [(65041, True), (65042, False), (None, False)])
def test_release_force_clears_then_checks(monkeypatch, check, released):
    import asyncio

    calls = []

    async def fake_reply(_material, *, force_clear):
        calls.append(force_clear)
        return 65041 if force_clear else check

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(device, "_signature_reply", fake_reply)
    monkeypatch.setattr(device.asyncio, "sleep", no_sleep)
    assert asyncio.run(device.release({"sn": "8810AABB"})) is released
    assert calls == [True, False]
