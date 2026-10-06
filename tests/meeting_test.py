from datetime import date, datetime, timedelta

import pytest

from plaud_fetch.meeting import (
    WindowError,
    existing_recording,
    folder_date,
    mp3_dir,
    recording_name,
    resolve_window,
)


def local(*args):
    return datetime(*args).astimezone()


def test_folder_date_from_name_and_mp3_subdir(tmp_path):
    folder = tmp_path / "20261006_CCM4_Day1"
    assert folder_date(folder) == date(2026, 10, 6)
    assert folder_date(folder / "mp3") == date(2026, 10, 6)
    assert folder_date(tmp_path / "Florian") is None
    assert folder_date(tmp_path / "20261399_bad") is None


def test_mp3_dir(tmp_path):
    assert mp3_dir(tmp_path / "20261006_X") == tmp_path / "20261006_X" / "mp3"
    assert mp3_dir(tmp_path / "x" / "mp3") == tmp_path / "x" / "mp3"


def test_window_whole_day_from_folder(tmp_path):
    low, high = resolve_window(tmp_path / "20261006_CCM4")
    assert low == local(2026, 10, 6)
    assert high - low == timedelta(days=1)


def test_window_hours_on_folder_day(tmp_path):
    low, high = resolve_window(tmp_path / "20261006_CCM4", start="14:00", end="16:30")
    assert (low, high) == (local(2026, 10, 6, 14), local(2026, 10, 6, 16, 30))


def test_window_spanning_days(tmp_path):
    # From the day before through the end of the folder's own day.
    low, high = resolve_window(tmp_path / "20261001_ORUS_Seminaire", start="2026-09-30")
    assert low == local(2026, 9, 30)
    assert high == local(2026, 10, 2)
    low, high = resolve_window(None, start="2026-09-30", end="2026-10-01")
    assert high == local(2026, 10, 2)  # A bare end date includes that day.


def test_undated_folder_needs_date(tmp_path):
    with pytest.raises(WindowError):
        resolve_window(tmp_path / "Florian")
    with pytest.raises(WindowError):
        resolve_window(tmp_path / "Florian", start="10:00")
    low, _ = resolve_window(tmp_path / "Florian", day="2026-09-25", start="10:00")
    assert low == local(2026, 9, 25, 10)


def test_empty_window_fails(tmp_path):
    with pytest.raises(WindowError):
        resolve_window(tmp_path / "20261006_X", start="16:00", end="15:00")


def test_existing_recording_tolerates_a_few_seconds(tmp_path):
    start = local(2026, 9, 29, 15, 24, 42)
    assert recording_name(start) == "2026-09-29_15_24_42"
    (tmp_path / "2026-09-29_15_24_45.mp3").write_bytes(b"")
    (tmp_path / "notes.txt").write_bytes(b"")
    assert existing_recording(tmp_path, start).name == "2026-09-29_15_24_45.mp3"
    assert existing_recording(tmp_path, start + timedelta(seconds=30)) is None
    assert existing_recording(tmp_path / "missing", start) is None
