"""Does a recording contain speech like your meetings, or silence or garble?

This is the last gate before a recording may be deleted from the device.

Each recording is cut into one-minute chunks and every chunk gets a feature
vector (numpy only):

- speech: share of 20 ms frames that are clearly above the recording's noise
  floor, sit in the 300-3400 Hz speech band and have a peaky spectrum;
- active: share of frames above the noise floor;
- voiced: share of active frames that are periodic at pitch lags (60-400 Hz);
- flatness: median spectral flatness of active frames (noise is flat);
- modulation: share of loudness-envelope modulation at 2-8 Hz, the syllable
  rate of speech;
- clipped: share of samples at full scale;
- band: median speech-band energy share of active frames.

A reference built from previously recorded meetings (``plaud-fetch
calibrate``) holds the mean and covariance of these vectors over all chunks
that are not quiet. A chunk whose Mahalanobis distance exceeds the 99.5th
percentile of the reference chunks is unlike any meeting. Quiet chunks (pauses)
are not judged. Without a reference, fixed limits are used instead.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .audio import SAMPLE_RATE, Decoded

FRAME = 512  # 32 ms analysis window
HOP = 320  # 20 ms hop, 50 frames per second
FRAME_RATE = SAMPLE_RATE / HOP
BLOCK_FRAMES = 3000  # frames analysed per block, to bound memory on long files
CHUNK_FRAMES = 3000  # one minute per judged chunk
MIN_CHUNK_FRAMES = 1000  # a trailing chunk shorter than 20 s is merged/ignored
MOD_WINDOW = 512  # envelope samples per modulation window (~10 s)
VOICED_PEAK = 0.5  # autocorrelation peak above which a frame is voiced
QUIET_CHUNK = 0.10  # chunks with less activity are pauses, not judged

FEATURES = ("speech", "active", "voiced", "log_flatness", "modulation", "log_clipped", "band")
REFERENCE_FILE = "speech-reference.json"
REFERENCE_QUANTILE = 0.995

MIN_SPEECH_SECONDS = 10.0
MAX_OUTLIER_SHARE = 0.2  # of judged chunks, with a reference
# Fixed limits used only without a reference.
MIN_MODULATION = 0.30
MAX_FLATNESS = 0.45
MAX_CLIPPED = 0.01


class ReferenceError(RuntimeError):
    """The speech reference is missing data or was built by another version."""


@dataclass
class Reference:
    """Statistics of one-minute chunks of past meetings: what speech looks like here.

    Holds no audio, only the mean and inverse covariance of the chunk feature
    vectors and the distance that 99.5 % of the reference chunks stay within.
    """

    mean: np.ndarray
    inverse: np.ndarray
    threshold: float
    files: int
    chunks: int

    def distances(self, vectors: np.ndarray) -> np.ndarray:
        """Mahalanobis distance of each chunk vector from the reference meetings.

        :param vectors: one row per chunk, columns :data:`FEATURES`
        :returns: one distance per row
        """
        centred = vectors - self.mean
        return np.sqrt(np.einsum("ij,jk,ik->i", centred, self.inverse, centred))

    def save(self, path: Path) -> None:
        """Write the reference as JSON.

        :param path: target file; its directory is the private credentials one
        """
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "features": FEATURES,
                    "mean": self.mean.tolist(),
                    "inverse": self.inverse.tolist(),
                    "threshold": self.threshold,
                    "files": self.files,
                    "chunks": self.chunks,
                },
                indent=1,
            )
        )

    @classmethod
    def load(cls, path: Path) -> Reference:
        """Read a reference written by :meth:`save`.

        :param path: reference JSON
        :raises ReferenceError: when built by another version or feature set
        """
        raw = json.loads(path.read_text())
        if raw.get("version") != 1 or tuple(raw.get("features", ())) != FEATURES:
            raise ReferenceError(
                f"{path} was built by another version; run 'plaud-fetch calibrate'"
            )
        return cls(
            np.array(raw["mean"]),
            np.array(raw["inverse"]),
            float(raw["threshold"]),
            int(raw["files"]),
            int(raw["chunks"]),
        )

    @classmethod
    def build(cls, vectors: np.ndarray, files: int) -> Reference:
        """Fit the reference to chunk vectors of past meetings.

        :param vectors: non-quiet chunk vectors (:func:`chunk_vectors`) of all files
        :param files: number of recordings they come from, for the record
        :raises ReferenceError: with fewer than four chunks per feature
        """
        if len(vectors) < 4 * len(FEATURES):
            raise ReferenceError(
                f"only {len(vectors)} non-quiet minutes; give more meeting recordings"
            )
        mean = vectors.mean(axis=0)
        covariance = np.cov(vectors, rowvar=False)
        # Light ridge so a feature that barely varies cannot dominate.
        covariance += np.eye(len(FEATURES)) * 1e-3 * np.trace(covariance) / len(FEATURES)
        reference = cls(mean, np.linalg.inv(covariance), 0.0, files, len(vectors))
        reference.threshold = float(np.quantile(reference.distances(vectors), REFERENCE_QUANTILE))
        return reference


@dataclass
class SpeechReport:
    """Verdict on one recording: measures, and the reasons it was rejected."""

    duration: float
    speech_seconds: float
    chunks: int  # one-minute chunks judged (not quiet)
    outliers: int  # judged chunks unlike the reference meetings
    voiced: float
    flatness: float
    modulation: float
    clipped: float
    decode_errors: str
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether the recording passed: no reason to reject it."""
        return not self.reasons

    def summary(self) -> str:
        """One line with the measures, for the terminal and the ledger."""
        judged = f", unlike meetings {self.outliers}/{self.chunks} min" if self.chunks else ""
        return (
            f"{self.duration / 60:.1f} min, speech {self.speech_seconds / 60:.1f} min, "
            f"voiced {self.voiced:.0%}, flatness {self.flatness:.2f}, "
            f"modulation {self.modulation:.2f}, clipped {self.clipped:.2%}{judged}"
        )


def _frames(samples: np.ndarray) -> np.ndarray:
    """Overlapping analysis frames (:data:`FRAME` samples every :data:`HOP`)."""
    count = 1 + (len(samples) - FRAME) // HOP
    index = np.arange(FRAME)[None, :] + HOP * np.arange(count)[:, None]
    return samples[index]


def _frame_features(samples: np.ndarray) -> tuple[np.ndarray, ...]:
    """Per-frame level (dBFS), speech-band share, spectral flatness and voicing.

    Voicing is the normalised autocorrelation peak at pitch lags (60-400 Hz):
    voiced speech is periodic, decoder garble and noise are not.
    """
    window = np.hanning(FRAME).astype(np.float32)
    freqs = np.fft.rfftfreq(FRAME, 1.0 / SAMPLE_RATE)
    total_band = (freqs >= 80) & (freqs <= 7600)
    speech_band = (freqs >= 300) & (freqs <= 3400)
    low_lag, high_lag = SAMPLE_RATE // 400, SAMPLE_RATE // 60
    # Correct the triangular bias of the autocorrelation of a finite frame.
    lag_norm = FRAME / (FRAME - np.arange(low_lag, high_lag + 1))
    levels, shares, flatness, voicing = [], [], [], []
    frames = _frames(samples)
    for start in range(0, len(frames), BLOCK_FRAMES):
        block = frames[start : start + BLOCK_FRAMES]
        rms = np.sqrt(np.mean(block**2, axis=1)) + 1e-9
        levels.append(20 * np.log10(rms))
        power = np.abs(np.fft.rfft(block * window, axis=1)) ** 2 + 1e-12
        shares.append(power[:, speech_band].sum(1) / power[:, total_band].sum(1))
        band = power[:, speech_band]
        flatness.append(np.exp(np.mean(np.log(band), axis=1)) / np.mean(band, axis=1))
        centred = block - block.mean(axis=1, keepdims=True)
        spectrum = np.abs(np.fft.rfft(centred, n=2 * FRAME, axis=1)) ** 2
        autocorr = np.fft.irfft(spectrum, axis=1)[:, : high_lag + 1]
        peaks = (autocorr[:, low_lag:] * lag_norm).max(axis=1)
        voicing.append(peaks / (autocorr[:, 0] + 1e-12))
    return (
        np.concatenate(levels),
        np.concatenate(shares),
        np.concatenate(flatness),
        np.concatenate(voicing),
    )


def _modulation(levels: np.ndarray, active: np.ndarray) -> float:
    """Median share of 2-8 Hz in the envelope modulation spectrum of active windows."""
    freqs = np.fft.rfftfreq(MOD_WINDOW, 1.0 / FRAME_RATE)
    syllabic = (freqs >= 2) & (freqs <= 8)
    reference = (freqs >= 0.5) & (freqs <= 20)
    taper = np.hanning(MOD_WINDOW)
    shares = []
    for start in range(0, len(levels) - MOD_WINDOW + 1, MOD_WINDOW // 2):
        if active[start : start + MOD_WINDOW].mean() < 0.3:
            continue
        envelope = levels[start : start + MOD_WINDOW]
        power = np.abs(np.fft.rfft((envelope - envelope.mean()) * taper)) ** 2
        shares.append(power[syllabic].sum() / (power[reference].sum() + 1e-12))
    return float(np.median(shares)) if shares else 0.0


@dataclass
class _Analysis:
    """Measures of a whole recording and its per-minute feature vectors."""

    vectors: np.ndarray  # one row per non-quiet chunk, columns FEATURES
    speech_seconds: float
    voiced: float
    flatness: float
    modulation: float
    clipped: float


def _analyse(samples: np.ndarray) -> _Analysis:
    """Frame features, whole-recording measures and per-minute feature vectors.

    :param samples: 16 kHz mono samples in [-1, 1]
    """
    levels, shares, flatness, voicing = _frame_features(samples)
    floor = np.percentile(levels, 10)
    active = (levels > floor + 12) & (levels > -55)
    speechy = active & (shares > 0.6) & (flatness < MAX_FLATNESS)
    voiced = voicing > VOICED_PEAK
    sample_clipped = np.abs(samples) >= 0.999
    rows = []
    for start in range(0, len(levels), CHUNK_FRAMES):
        stop = min(start + CHUNK_FRAMES, len(levels))
        if stop - start < MIN_CHUNK_FRAMES:
            break
        part = slice(start, stop)
        act = active[part]
        if act.mean() < QUIET_CHUNK:
            continue
        clipped = sample_clipped[start * HOP : stop * HOP].mean()
        rows.append(
            (
                speechy[part].mean(),
                act.mean(),
                voiced[part][act].mean(),
                np.log10(np.median(flatness[part][act]) + 1e-4),
                _modulation(levels[part], act),
                np.log10(clipped + 1e-5),
                np.median(shares[part][act]),
            )
        )
    return _Analysis(
        vectors=np.array(rows, dtype=float).reshape(-1, len(FEATURES)),
        speech_seconds=float(speechy.sum() / FRAME_RATE),
        voiced=float(voiced[active].mean()) if active.any() else 0.0,
        flatness=float(np.median(flatness[active])) if active.any() else 1.0,
        modulation=_modulation(levels, active),
        clipped=float(sample_clipped.mean()),
    )


def chunk_vectors(decoded: Decoded) -> np.ndarray:
    """Feature vectors of the non-quiet minutes, for building a reference.

    :param decoded: a decoded recording
    :returns: one row per non-quiet minute; empty for very short files
    """
    if len(decoded.samples) < FRAME * MIN_CHUNK_FRAMES // 4:
        return np.empty((0, len(FEATURES)))
    return _analyse(decoded.samples).vectors


def reference_path() -> Path:
    """Where ``plaud-fetch calibrate`` keeps the reference."""
    from .credentials import default_dir

    return default_dir() / REFERENCE_FILE


def load_reference(path: Path | None = None) -> Reference | None:
    """The reference, or ``None`` before ``plaud-fetch calibrate`` has run.

    :param path: reference file (default :func:`reference_path`)
    :raises ReferenceError: when it was built by another version
    """
    path = path or reference_path()
    return Reference.load(path) if path.exists() else None


def analyse(
    decoded: Decoded,
    expected_duration: float | None = None,
    reference: Reference | None = None,
) -> SpeechReport:
    """Judge whether a recording holds speech like the reference meetings.

    :param decoded: the recording as decoded from the MP3
    :param expected_duration: duration of the source the MP3 was made from;
        a mismatch means a truncated or padded encode
    :param reference: meetings reference; without one, fixed limits apply
    :returns: the report; ``report.ok`` is the verdict
    """
    duration = decoded.duration
    if len(decoded.samples) < FRAME * 4:
        return SpeechReport(duration, 0, 0, 0, 0, 1, 0, 0, decoded.errors, ["too short to judge"])
    result = _analyse(decoded.samples)
    report = SpeechReport(
        duration=duration,
        speech_seconds=result.speech_seconds,
        chunks=len(result.vectors),
        outliers=0,
        voiced=result.voiced,
        flatness=result.flatness,
        modulation=result.modulation,
        clipped=result.clipped,
        decode_errors=decoded.errors,
    )
    if decoded.errors:
        report.reasons.append(f"decoder errors: {decoded.errors.splitlines()[0]}")
    if expected_duration is not None:
        tolerance = max(1.0, 0.005 * expected_duration)
        if abs(duration - expected_duration) > tolerance:
            report.reasons.append(
                f"duration {duration:.1f} s differs from source {expected_duration:.1f} s"
            )
    if result.speech_seconds < MIN_SPEECH_SECONDS:
        report.reasons.append(f"too little speech ({result.speech_seconds:.0f} s)")
    if reference is not None:
        if report.chunks:
            report.outliers = int((reference.distances(result.vectors) > reference.threshold).sum())
            if report.outliers > MAX_OUTLIER_SHARE * report.chunks:
                report.reasons.append(
                    f"{report.outliers} of {report.chunks} minutes unlike the reference meetings"
                )
        elif duration >= MIN_CHUNK_FRAMES / FRAME_RATE:
            report.reasons.append("no minute active enough to compare with the reference")
        return report
    if result.modulation < MIN_MODULATION:
        report.reasons.append(f"no syllabic rhythm (modulation {result.modulation:.2f})")
    if result.flatness > MAX_FLATNESS:
        report.reasons.append(f"noise-like spectrum (flatness {result.flatness:.2f})")
    if result.clipped > MAX_CLIPPED:
        report.reasons.append(f"clipped ({result.clipped:.1%} of samples)")
    return report
