"""Record of what was fetched, so a delete can be tied to a verified MP3.

A tab-separated file in ``$XDG_DATA_HOME/plaud-fetch/ledger.tsv``; one line
per fetch or delete event, newest last.
"""

from __future__ import annotations

import csv
import hashlib
import os
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

COLUMNS = ("time", "event", "session_id", "start", "size", "mp3", "sha256", "verdict", "note")


@dataclass
class Entry:
    """One ledger line: a fetch or a delete of one device recording."""

    time: str
    event: str  # fetched | deleted
    session_id: int
    start: str
    size: int
    mp3: str
    sha256: str
    verdict: str  # speech | rejected
    note: str = ""


def default_path() -> Path:
    """The ledger file, ``$XDG_DATA_HOME/plaud-fetch/ledger.tsv``."""
    base = os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    return Path(base) / "plaud-fetch" / "ledger.tsv"


def sha256(path: Path) -> str:
    """SHA-256 of a file, as hex; detects an MP3 changed after its fetch."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def append(entry: Entry, path: Path | None = None) -> None:
    """Append one entry, writing the header line first in a new ledger.

    :param entry: the event to record (tabs in values become spaces)
    :param path: ledger file (default :func:`default_path`)
    """
    path = path or default_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        if new:
            writer.writerow(COLUMNS)
        row = asdict(entry)
        writer.writerow([str(row[name]).replace("\t", " ") for name in COLUMNS])


def entries(path: Path | None = None) -> list[Entry]:
    """All entries, oldest first; empty when there is no ledger yet.

    :param path: ledger file (default :func:`default_path`)
    """
    path = path or default_path()
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        types = {field.name: field.type for field in fields(Entry)}
        return [
            Entry(**{k: int(v) if types[k] in ("int", int) else v for k, v in row.items()})
            for row in reader
        ]


def verified_fetch(session_id: int, mp3: Path, path: Path | None = None) -> Entry | None:
    """The fetch that makes deleting ``session_id`` from the device safe.

    :param session_id: device session id
    :param mp3: the MP3 in the meeting folder
    :param path: ledger file (default :func:`default_path`)
    :returns: the last fetch of that session into ``mp3`` if it passed the
        speech check and the file is unchanged since, else ``None``
    """
    for entry in reversed(entries(path)):
        if entry.event != "fetched" or entry.session_id != session_id:
            continue
        if Path(entry.mp3) != mp3.resolve() or entry.verdict != "speech":
            return None
        return entry if mp3.exists() and sha256(mp3) == entry.sha256 else None
    return None


def now() -> str:
    """Current local time, ISO 8601 with offset, to the second."""
    return datetime.now().astimezone().isoformat(timespec="seconds")
