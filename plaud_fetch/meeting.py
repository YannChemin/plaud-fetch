"""Meeting folders, time windows and recording file names.

A meeting folder is named ``YYYYMMDD_<Name>`` (e.g. ``Report/20261006_CCM4_Day1``)
and keeps its audio in ``mp3/YYYY-MM-DD_HH_MM_SS.mp3``, the layout read by
``transcribe_remote.sh`` and ``journal.sh``. Times are local wall-clock time.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from pathlib import Path

FOLDER_DATE = re.compile(r"^(\d{4})(\d{2})(\d{2})(?:_|$)")
MP3_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2}_\d{2}_\d{2}_\d{2})\.mp3$")
NAME_FORMAT = "%Y-%m-%d_%H_%M_%S"
# Existing recordings exported by the Plaud phone app may be named a few
# seconds away from the device session start.
SAME_RECORDING_SECONDS = 5


class WindowError(ValueError):
    """The requested time window cannot be built or is empty."""


def local_start(session_id: int) -> datetime:
    """Recording start in local time; the device session id is Unix seconds.

    :param session_id: device session id
    :returns: timezone-aware local datetime
    """
    return datetime.fromtimestamp(session_id).astimezone()


def recording_name(start: datetime) -> str:
    """File name stem for a recording, ``YYYY-MM-DD_HH_MM_SS``."""
    return start.strftime(NAME_FORMAT)


def mp3_dir(folder: Path) -> Path:
    """Where a meeting folder keeps its audio: ``<folder>/mp3`` (or ``folder`` itself)."""
    return folder if folder.name == "mp3" else folder / "mp3"


def folder_date(folder: Path) -> date | None:
    """Date from a ``YYYYMMDD_`` folder name (or from the parent of ``mp3/``).

    :param folder: meeting folder or its ``mp3`` subfolder
    :returns: the date, or ``None`` for an undated or invalid name
    """
    name = folder.parent.name if folder.name == "mp3" else folder.name
    match = FOLDER_DATE.match(name)
    if not match:
        return None
    try:
        return date(int(match[1]), int(match[2]), int(match[3]))
    except ValueError:
        return None


def parse_moment(text: str, base: date | None) -> datetime:
    """Parse ``HH:MM[:SS]`` (on ``base``), ``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM[:SS]``.

    :param text: the user's time
    :param base: day for a bare time of day
    :returns: timezone-aware local datetime
    :raises WindowError: when the text is not a known format, or is a time
        of day without a ``base`` day
    """
    text = text.strip().replace("T", " ")
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            clock = datetime.strptime(text, fmt).time()  # noqa: DTZ007  # Time of day only.
        except ValueError:
            continue
        if base is None:
            raise WindowError(f"'{text}' has no date and the folder name gives none; use --date")
        return datetime.combine(base, clock).astimezone()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).astimezone()
        except ValueError:
            continue
    raise WindowError(f"cannot read time '{text}' (use HH:MM, YYYY-MM-DD or 'YYYY-MM-DD HH:MM')")


def resolve_window(
    folder: Path | None,
    *,
    day: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> tuple[datetime, datetime]:
    """Recording-start window [start, end) from the folder date and options.

    Without ``--from``/``--to`` the window is the whole day. A day comes from
    ``--date`` or else the folder name; an undated folder needs one of them. A
    bare ``YYYY-MM-DD`` end includes that whole day.

    :param folder: meeting folder, or ``None``
    :param day: ``--date`` value
    :param start: ``--from`` value
    :param end: ``--to`` value
    :returns: (start, end) as timezone-aware local datetimes
    :raises WindowError: without any date, or when the window is empty
    """
    base = date.fromisoformat(day) if day else (folder_date(folder) if folder else None)
    if start is None and end is None and base is None:
        raise WindowError(
            "no date: the folder is not named YYYYMMDD_..., give --date or --from/--to"
        )
    low = parse_moment(start, base) if start else None
    high = parse_moment(end, base) if end else None
    if low is None:
        low = datetime.combine(base or high.date(), time()).astimezone()
    if high is None:
        high = datetime.combine(base or low.date(), time()).astimezone() + timedelta(days=1)
        if high <= low:
            high = datetime.combine(low.date(), time()).astimezone() + timedelta(days=1)
    elif end and len(end.strip()) == 10:
        # A bare end date includes that whole day.
        high += timedelta(days=1)
    if high <= low:
        raise WindowError(f"empty window: {low:%Y-%m-%d %H:%M} to {high:%Y-%m-%d %H:%M}")
    return low, high


def existing_recording(directory: Path, start: datetime) -> Path | None:
    """An MP3 already in ``directory`` for the recording starting at ``start``.

    Names within :data:`SAME_RECORDING_SECONDS` of ``start`` match, since the
    phone app's exports may be a few seconds off the device session start.

    :param directory: the meeting's ``mp3`` folder (may not exist)
    :param start: recording start
    :returns: the matching file, or ``None``
    """
    if not directory.is_dir():
        return None
    for path in directory.iterdir():
        match = MP3_NAME.match(path.name)
        if not match:
            continue
        other = datetime.strptime(match[1], NAME_FORMAT).astimezone()
        if abs((other - start).total_seconds()) <= SAME_RECORDING_SECONDS:
            return path
    return None
