import subprocess
from pathlib import Path

import numpy as np
import pytest

from plaud_fetch.audio import AudioError, decode, encode_mp3, probe_duration, to_ogg


def ogg_packets(data: bytes) -> list[bytes]:
    packets, position, current = [], 0, b""
    while position < len(data):
        count = data[position + 26]
        sizes = data[position + 27 : position + 27 + count]
        body = position + 27 + count
        for size in sizes:
            current += data[body : body + size]
            body += size
            if size < 255:
                packets.append(current)
                current = b""
        position = body
    return packets[2:]  # Skip OpusHead and OpusTags.


@pytest.fixture
def raw_opus(tmp_path) -> Path:
    """Raw 80-byte Opus packets, as the device stores them (32 kbit/s CBR, 20 ms)."""
    ogg = tmp_path / "cbr.ogg"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=f=220:r=16000",
            "-t",
            "3",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            "-vbr",
            "off",
            "-frame_duration",
            "20",
            str(ogg),
        ],
        check=True,
    )
    packets = ogg_packets(ogg.read_bytes())
    assert {len(p) for p in packets} == {80}
    raw = tmp_path / "1759152282.plaud"
    raw.write_bytes(b"".join(packets))
    return raw


def test_raw_opus_to_verified_mp3(tmp_path, raw_opus):
    ogg, source = to_ogg(raw_opus, "", tmp_path)
    assert ogg.name.endswith(".f80.ogg")
    assert source.errors == "" and source.duration == pytest.approx(3.0, abs=0.05)
    mp3 = tmp_path / "out" / "2026-09-29_15_24_42.mp3"
    encode_mp3(ogg, mp3, bitrate="32k", title="test")
    assert probe_duration(mp3) == pytest.approx(3.0, abs=0.1)
    decoded = decode(mp3)
    assert decoded.errors == ""
    assert np.abs(decoded.samples).max() > 0.1


def test_random_bytes_are_rejected(tmp_path):
    raw = tmp_path / "garbage.plaud"
    raw.write_bytes(np.random.default_rng(1).bytes(80 * 500))
    with pytest.raises(AudioError):
        to_ogg(raw, "", tmp_path)


def test_unaligned_data_is_rejected(tmp_path):
    raw = tmp_path / "odd.plaud"
    raw.write_bytes(b"\x01" * 4801)
    with pytest.raises(AudioError):
        to_ogg(raw, "", tmp_path)
