"""Turn a downloaded device recording into a verified MP3.

A download is a ``PLAUD.AI`` encrypted container (512-byte header whose
symmetric key is wrapped with the device RSA key) or bare audio. Inside is
either an Ogg Opus stream (what the Note Pro V1.7.0 sends: 48 kHz mono) or raw
fixed-size Opus packets (what the NotePin S sends: 80 bytes per 20 ms), which
are wrapped into Ogg. ffmpeg then encodes a 16 kHz mono MP3.

Every step is checked by a full ffmpeg decode, so a wrong key or frame size
fails here instead of producing a file of noise.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
# Raw Opus packet sizes tried, in order, when the payload is not Ogg: 80 bytes
# per 20 ms (32 kbit/s) is what the NotePin S sends.
FRAME_SIZES = (80, 40, 60, 100, 120, 160)


class AudioError(RuntimeError):
    """A recording could not be decrypted, decoded or encoded."""


@dataclass(frozen=True)
class Decoded:
    """A fully decoded recording and what the decoder complained about."""

    samples: np.ndarray  # float32 mono in [-1, 1] at SAMPLE_RATE
    errors: str  # ffmpeg error output; empty when the stream decoded cleanly

    @property
    def duration(self) -> float:
        """Length in seconds."""
        return len(self.samples) / SAMPLE_RATE


def decode(path: Path) -> Decoded:
    """Decode a whole audio file to 16 kHz mono and collect decoder errors.

    :param path: any audio file ffmpeg reads
    :returns: the samples and ffmpeg's error output (empty when clean)
    """
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-i",
            str(path),
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "s16le",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    samples = np.frombuffer(result.stdout, dtype="<i2").astype(np.float32) / 32768.0
    errors = result.stderr.decode("utf-8", errors="replace").strip()
    if result.returncode != 0 and not errors:
        errors = f"ffmpeg exited with status {result.returncode}"
    return Decoded(samples, errors)


def probe_duration(path: Path) -> float:
    """Duration of a media file as reported by ffprobe.

    :param path: audio file
    :returns: duration in seconds
    :raises AudioError: when ffprobe reports no duration
    """
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as error:
        raise AudioError(f"cannot read the duration of {path}") from error


def to_ogg(raw: Path, rsa_private_key: str, workdir: Path) -> tuple[Path, Decoded]:
    """Decrypt and, if needed, wrap a download into a cleanly decodable Ogg Opus file.

    :param raw: the file downloaded from the device
    :param rsa_private_key: PEM private key of the device identity, which
        unwraps the container's audio key
    :param workdir: directory for intermediate files
    :returns: the Ogg file and its full decode (the reference duration)
    :raises AudioError: when no interpretation decodes without errors
    """
    from plaud_pc.audio import MAGIC, decrypt_plaud_audio, wrap_raw_opus

    channels = 1
    payload = raw
    with raw.open("rb") as stream:
        head = stream.read(8)
    if head == MAGIC:
        payload = workdir / (raw.stem + ".dec")
        header = decrypt_plaud_audio(raw, payload, rsa_private_key)
        channels = max(1, header.channels)
        with payload.open("rb") as stream:
            head = stream.read(8)
    if head.startswith(b"OggS"):
        decoded = decode(payload)
        if decoded.errors or not len(decoded.samples):
            raise AudioError(f"{raw.name}: Ogg stream does not decode: {decoded.errors}")
        return payload, decoded

    size = payload.stat().st_size
    failures = []
    for frame_size in FRAME_SIZES:
        if size % frame_size:
            continue
        ogg = workdir / f"{raw.stem}.f{frame_size}.ogg"
        wrap_raw_opus(payload, ogg, channels=channels, frame_size=frame_size)
        decoded = decode(ogg)
        if not decoded.errors and len(decoded.samples):
            return ogg, decoded
        failures.append(
            f"{frame_size} B: {decoded.errors.splitlines()[0] if decoded.errors else 'empty'}"
        )
        ogg.unlink(missing_ok=True)
    raise AudioError(
        f"{raw.name}: not a PLAUD.AI container, Ogg or raw Opus with a known frame size"
        + (f" ({'; '.join(failures)})" if failures else f" (size {size} B)")
    )


def encode_mp3(source: Path, target: Path, *, bitrate: str, title: str) -> None:
    """Encode a 16 kHz mono MP3, the format of the existing meeting recordings.

    Written to ``<target>.part`` first and renamed, so ``target`` is never a
    partial file.

    :param source: audio to encode
    :param target: MP3 to create (its directory is created if needed)
    :param bitrate: libmp3lame bitrate, e.g. ``32k``
    :param title: ID3 title tag
    :raises AudioError: when ffmpeg fails
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-i",
            str(source),
            "-map_metadata",
            "-1",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            "libmp3lame",
            "-b:a",
            bitrate,
            "-metadata",
            f"title={title}",
            "-metadata",
            "comment=plaud-fetch",
            "-f",
            "mp3",
            str(temporary),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise AudioError(f"ffmpeg could not encode {target.name}: {result.stderr.strip()}")
    temporary.replace(target)
