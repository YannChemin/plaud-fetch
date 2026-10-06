from pathlib import Path

import numpy as np
import pytest

from plaud_fetch.audio import SAMPLE_RATE, Decoded, decode
from plaud_fetch.speech import FEATURES, Reference, ReferenceError, analyse, chunk_vectors

RNG = np.random.default_rng(7)
MEETINGS = Path.home() / "Documents" / "LaTex"


def babble(seconds: float) -> np.ndarray:
    """Speech-like test signal: voiced harmonics (100-180 Hz pitch) shaped by
    two formants (700 and 1800 Hz), in syllables at 4 Hz with short pauses."""
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    pitch = 140 + 40 * np.sin(2 * np.pi * 0.3 * t)
    phase = 2 * np.pi * np.cumsum(pitch) / SAMPLE_RATE
    voice = sum(np.sin(k * phase) / k for k in range(1, 40))
    freqs = np.fft.rfftfreq(len(t), 1 / SAMPLE_RATE)
    formants = sum(np.exp(-(((freqs - f) / w) ** 2)) for f, w in ((700, 250), (1800, 400)))
    voice = np.fft.irfft(np.fft.rfft(voice) * (formants + 0.02), n=len(t))
    syllables = 0.1 + 0.9 * np.abs(np.sin(2 * np.pi * 2 * t))
    pauses = (np.sin(2 * np.pi * 0.1 * t) > -0.6).astype(float)
    signal = voice * syllables * pauses
    return (0.3 * signal / np.abs(signal).max() + 0.002 * RNG.standard_normal(len(t))).astype(
        np.float32
    )


def decoded(samples, errors=""):
    return Decoded(np.asarray(samples, dtype=np.float32), errors)


def test_babble_passes_fixed_limits():
    report = analyse(decoded(babble(120)))
    assert report.ok, report.reasons


@pytest.mark.parametrize(
    "samples",
    [
        np.zeros(120 * SAMPLE_RATE),
        0.3 * RNG.standard_normal(120 * SAMPLE_RATE),
        0.3 * np.sin(2 * np.pi * 440 * np.arange(120 * SAMPLE_RATE) / SAMPLE_RATE),
    ],
    ids=["silence", "white-noise", "tone"],
)
def test_non_speech_is_rejected(samples):
    assert not analyse(decoded(samples)).ok


def test_decoder_errors_and_duration_mismatch_reject():
    assert not analyse(decoded(babble(60), errors="Error parsing the packet header")).ok
    report = analyse(decoded(babble(60)), expected_duration=75)
    assert any("duration" in reason for reason in report.reasons)


def test_clipping_rejects():
    assert not analyse(decoded(np.clip(babble(120) * 50, -1, 1))).ok


def test_reference_round_trip_and_outliers(tmp_path):
    vectors = np.vstack([chunk_vectors(decoded(babble(600 + 60 * k))) for k in range(5)])
    assert vectors.shape[1] == len(FEATURES)
    reference = Reference.build(vectors, 5)
    path = tmp_path / "reference.json"
    reference.save(path)
    loaded = Reference.load(path)
    assert loaded.threshold == pytest.approx(reference.threshold)
    assert analyse(decoded(babble(300)), reference=loaded).ok
    noise = decoded(0.3 * RNG.standard_normal(300 * SAMPLE_RATE))
    assert not analyse(noise, reference=loaded).ok
    # Half garbled: speech, then minutes of noise bursts.
    bursts = 0.3 * RNG.standard_normal(300 * SAMPLE_RATE) * (RNG.random(300 * SAMPLE_RATE) > 0.5)
    mixed = decoded(np.concatenate([babble(300), bursts]))
    assert not analyse(mixed, reference=loaded).ok


def test_reference_needs_enough_minutes():
    with pytest.raises(ReferenceError):
        Reference.build(np.zeros((3, len(FEATURES))), 1)


@pytest.mark.skipif(not MEETINGS.is_dir(), reason="no local meeting recordings")
def test_real_meetings_pass_reference_from_other_meetings():
    files = sorted(MEETINGS.glob("Report/*/mp3/*.mp3"))
    if len(files) < 6:
        pytest.skip("not enough meeting recordings")
    train, test = files[1::2][:8], files[0::2][:4]
    reference = Reference.build(np.vstack([chunk_vectors(decode(f)) for f in train]), len(train))
    for path in test:
        report = analyse(decode(path), reference=reference)
        if report.duration > 120:
            assert report.ok, (path.name, report.reasons)
