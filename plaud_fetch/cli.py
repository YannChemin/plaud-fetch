"""plaud-fetch: Plaud Note Pro recordings over Bluetooth into meeting folders.

Typical use, from inside a meeting folder named YYYYMMDD_<Name>:

    plaud-fetch list                      # what the device holds for that day
    plaud-fetch fetch                     # copy that day's recordings to mp3/
    plaud-fetch fetch --from 14:00 --to 16:30 --delete
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__

if TYPE_CHECKING:
    import numpy as np

    from .device import Recording
    from .speech import SpeechReport

DEFAULT_ROOTS = ("Report", "Pitch", "Management")
LATEX_DIR = Path.home() / "Documents" / "LaTex"
OPUS_BYTES_PER_SECOND = 4000  # 32 kbit/s, for duration estimates before download


def info(message: str) -> None:
    """Print a progress message on stderr (stdout is kept for data)."""
    print(message, file=sys.stderr)


def fail(message: str) -> None:
    """Print an error on stderr and exit with status 1."""
    print(f"plaud-fetch: {message}", file=sys.stderr)
    raise SystemExit(1)


def cache_dir() -> Path:
    """Private (0700) directory for downloads in progress and rejected recordings."""
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    path = Path(base) / "plaud-fetch"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def fmt_size(size: int) -> str:
    """Size in MB, for listings."""
    return f"{size / 1e6:.1f} MB"


def fmt_minutes(size: int) -> str:
    """Rough duration from the download size, before anything is decoded."""
    return f"~{size / OPUS_BYTES_PER_SECOND / 60:.0f} min"


def load_material(args: argparse.Namespace) -> dict[str, Any]:
    """Load the device identity or exit with a clear message."""
    from .credentials import CredentialError, load_material

    try:
        return load_material(args.config_dir)
    except CredentialError as error:
        fail(str(error))


def window_for(args: argparse.Namespace) -> tuple[Path | None, datetime | None, datetime | None]:
    """Meeting folder and recording-start window from the command options.

    :returns: (folder, start, end); start and end are ``None`` with ``--all``
    """
    from .meeting import WindowError, resolve_window

    folder = args.folder.resolve() if args.folder else None
    if args.all:
        return folder, None, None
    try:
        low, high = resolve_window(folder, day=args.date, start=args.start, end=args.end)
    except WindowError as error:
        fail(f"{error} (or --all)")
    return folder, low, high


def in_window(recording: Recording, low: datetime | None, high: datetime | None) -> bool:
    """Whether a recording starts inside [low, high); always true without a window."""
    return low is None or low <= recording.start < high


# Commands.


def cmd_scan(args: argparse.Namespace) -> None:
    """``plaud-fetch scan``: list Plaud recorders advertising nearby."""
    from .device import scan

    rows = asyncio.run(scan(args.seconds))
    if not rows:
        fail("no Plaud advertisement seen; is Bluetooth on and the device awake?")
    for address, name, serial, rssi in rows:
        print(f"{name}\t{address}\tSN {serial or '?'}\tRSSI {rssi}")


def cmd_status(args: argparse.Namespace) -> None:
    """``plaud-fetch status``: battery, storage and recording count."""
    from .device import PlaudSession

    async def run() -> None:
        """Read the snapshot in one session."""
        async with PlaudSession(load_material(args), scan_seconds=args.scan_seconds) as session:
            snap, recordings = await session.snapshot()
        gib = 1024**3
        print(f"Device: {snap.name}")
        print(f"Battery: {snap.battery_percent}%{' (charging)' if snap.charging else ''}")
        print(f"Storage: {snap.free_bytes / gib:.2f} GiB free of {snap.total_bytes / gib:.2f} GiB")
        print(f"Recordings: {len(recordings)}")

    asyncio.run(run())


def cmd_pair(args: argparse.Namespace) -> None:
    """``plaud-fetch pair``: first connection; ``--force-clear`` replaces the client key."""
    from .device import PlaudSession

    material = load_material(args)
    if args.force_clear and not args.yes:
        info(
            "Force-clear makes the Plaud drop the client key it is paired with (the phone\n"
            "app's) and enrol this computer's, as the Plaud SDK's recovery does. Recordings\n"
            "stay on the device. If the device then refuses the bind, the phone app has to\n"
            "pair again too."
        )
        if input("Clear the pairing and pair with this computer? Type yes: ").strip() != "yes":
            fail("not paired")

    async def run() -> tuple[Any, list[Recording]]:
        """Pair (optionally force-clear) and read the snapshot."""
        async with PlaudSession(
            material, scan_seconds=args.scan_seconds, force_clear=args.force_clear
        ) as session:
            return await session.snapshot()

    snap, recordings = asyncio.run(run())
    print(f"Paired: {snap.name}, battery {snap.battery_percent}%, {len(recordings)} recordings.")


def cmd_release(args: argparse.Namespace) -> None:
    """``plaud-fetch release``: remove this computer's key so the phone app can pair."""
    from .device import release

    material = load_material(args)
    if not args.yes:
        info(
            "This removes this computer's key from the Plaud and enrols none, so the\n"
            "owner's phone app can pair again. Recordings and owner are not touched."
        )
        if input("Release the Plaud? Type yes: ").strip() != "yes":
            fail("not released")
    if not asyncio.run(release(material)):
        fail("the device still holds a client key; retry in a few seconds")
    print("Released: no client key enrolled. Pair it again from the phone app.")


def cmd_list(args: argparse.Namespace) -> None:
    """``plaud-fetch list``: the device's recordings in the folder's time window."""
    from .device import PlaudSession
    from .meeting import existing_recording, mp3_dir

    folder, low, high = window_for(args)

    async def run() -> tuple[Any, list[Recording]]:
        """Read the snapshot in one session."""
        async with PlaudSession(load_material(args), scan_seconds=args.scan_seconds) as session:
            return await session.snapshot()

    _, recordings = asyncio.run(run())
    shown = [r for r in recordings if in_window(r, low, high)]
    if low is not None:
        info(
            f"Window: {low:%Y-%m-%d %H:%M} to {high:%Y-%m-%d %H:%M} "
            f"({len(shown)} of {len(recordings)} recordings)"
        )
    for recording in shown:
        present = ""
        if folder is not None and existing_recording(mp3_dir(folder), recording.start):
            present = "\tin folder"
        print(
            f"{recording.name}\t{fmt_minutes(recording.file.size)}\t"
            f"{fmt_size(recording.file.size)}\tsession {recording.session_id}{present}"
        )


def cmd_fetch(args: argparse.Namespace) -> None:
    """``plaud-fetch fetch``: download the window's recordings into ``mp3/``.

    Each new recording is downloaded, decrypted, encoded and speech-checked,
    then logged in the ledger. With ``--delete``, a recording is deleted from
    the device only after its MP3 passed the check and is unchanged since.
    """
    from . import ledger
    from .audio import AudioError, decode, encode_mp3, to_ogg
    from .device import DeviceError, PlaudSession
    from .meeting import existing_recording, mp3_dir
    from .speech import ReferenceError, analyse, load_reference

    folder = (args.folder or Path.cwd()).resolve()
    args.folder = folder
    if not folder.is_dir():
        fail(f"{folder} is not a directory")
    _, low, high = window_for(args)
    target_dir = mp3_dir(folder)
    try:
        reference = load_reference()
    except ReferenceError as error:
        fail(str(error))
    if reference is None:
        info("Warning: no speech reference; using fixed limits. Run 'plaud-fetch calibrate'.")
    material = load_material(args)
    raw_dir = cache_dir()

    def convert(recording: Recording, raw: Path, target: Path) -> tuple[Path, SpeechReport]:
        """Decrypt, encode and speech-check one download (runs in a worker thread).

        :returns: the MP3 and its speech report
        """
        """Decrypt, encode and check one download (runs in a worker thread)."""
        with tempfile.TemporaryDirectory(dir=raw_dir) as work:
            ogg, source = to_ogg(raw, str(material["rsaPrivateKey"]), Path(work))
            encode_mp3(ogg, target, bitrate=args.bitrate, title=f"{folder.name} {recording.name}")
        report = analyse(decode(target), expected_duration=source.duration, reference=reference)
        return target, report

    def confirm(recording: Recording) -> bool:
        """Ask before deleting a recording, unless ``--yes``."""
        if args.yes:
            return True
        answer = input(f"Delete {recording.name} from the device? [y/N] ")
        return answer.strip().lower() in ("y", "yes")

    async def run() -> int:
        """Fetch (and delete) every selected recording in one session.

        :returns: number of recordings that need attention
        """
        problems = 0
        async with PlaudSession(material, scan_seconds=args.scan_seconds) as session:
            snap, recordings = await session.snapshot()
            selected = [r for r in recordings if in_window(r, low, high)]
            span = f"{low:%Y-%m-%d %H:%M} to {high:%Y-%m-%d %H:%M}" if low else "all dates"
            info(f"{snap.name}: {len(recordings)} recordings, {len(selected)} in {span}.")
            for recording in selected:
                existing = existing_recording(target_dir, recording.start)
                label = f"{recording.name} ({fmt_minutes(recording.file.size)})"
                if existing and not args.redo:
                    if not args.delete:
                        info(f"{label}: already in {existing.relative_to(folder)}, skipped.")
                        continue
                    if ledger.verified_fetch(recording.session_id, existing) is None:
                        info(
                            f"{label}: {existing.name} was not verified by plaud-fetch; "
                            "kept on device (use --redo to download and verify it)."
                        )
                        continue
                    info(f"{label}: already fetched and verified.")
                else:
                    if args.dry_run:
                        info(
                            f"{label}: would be fetched to {target_dir.relative_to(folder.parent)}/."
                        )
                        continue
                    info(f"{label}: downloading {fmt_size(recording.file.size)}...")
                    try:
                        raw = await session.download(recording, raw_dir)
                        # With --redo, replace the folder's copy rather than add a twin.
                        target = existing or target_dir / f"{recording.name}.mp3"
                        target, report = await asyncio.to_thread(convert, recording, raw, target)
                    except (AudioError, DeviceError, OSError, TimeoutError) as error:
                        info(f"{label}: failed: {error}")
                        problems += 1
                        continue
                    ledger.append(
                        ledger.Entry(
                            ledger.now(),
                            "fetched",
                            recording.session_id,
                            recording.start.isoformat(),
                            recording.file.size,
                            str(target.resolve()),
                            ledger.sha256(target),
                            "speech" if report.ok else "rejected",
                            report.summary(),
                        )
                    )
                    info(f"{label}: {target.relative_to(folder.parent)}: {report.summary()}")
                    if not report.ok:
                        info(
                            f"{label}: NOT speech-checked ({'; '.join(report.reasons)}); "
                            f"kept on device, raw copy kept in {raw_dir}."
                        )
                        problems += 1
                        continue
                    if not args.keep_raw:
                        raw.unlink(missing_ok=True)
                if args.delete and not args.dry_run and confirm(recording):
                    try:
                        await session.delete(recording)
                    except (DeviceError, TimeoutError) as error:
                        info(f"{label}: delete failed: {error}")
                        problems += 1
                        continue
                    ledger.append(
                        ledger.Entry(
                            ledger.now(),
                            "deleted",
                            recording.session_id,
                            recording.start.isoformat(),
                            recording.file.size,
                            str((target_dir / f"{recording.name}.mp3").resolve()),
                            "",
                            "speech",
                        )
                    )
                    info(f"{label}: deleted from the device.")
        return problems

    problems = asyncio.run(run())
    if problems:
        fail(f"{problems} recording(s) need attention (see above)")


def _vectors(path: str) -> np.ndarray:
    """Chunk feature vectors of one file (module level, for the process pool)."""
    from .audio import decode
    from .speech import chunk_vectors

    return chunk_vectors(decode(Path(path)))


def cmd_calibrate(args: argparse.Namespace) -> None:
    """``plaud-fetch calibrate``: build the speech reference from recorded meetings."""
    import numpy as np

    from .speech import Reference, reference_path

    roots = args.dirs or [LATEX_DIR / name for name in DEFAULT_ROOTS]
    files = sorted({str(p) for root in roots for p in Path(root).rglob("mp3/*.mp3")})
    if not files:
        fail("no mp3/*.mp3 recordings found under " + ", ".join(map(str, roots)))
    info(f"Measuring {len(files)} meeting recordings...")
    with ProcessPoolExecutor(max(1, (os.cpu_count() or 2) - 1)) as pool:
        vectors = np.vstack(list(pool.map(_vectors, files)))
    reference = Reference.build(vectors, len(files))
    path = reference_path()
    reference.save(path)
    print(
        f"Reference: {reference.files} recordings, {reference.chunks} speaking minutes, "
        f"threshold {reference.threshold:.2f} -> {path}"
    )


def cmd_check(args: argparse.Namespace) -> None:
    """``plaud-fetch check``: speech-check audio files; exit 1 if any is rejected."""
    from .audio import decode
    from .speech import analyse, load_reference

    reference = load_reference()
    bad = 0
    for path in args.files:
        report = analyse(decode(path), reference=reference)
        verdict = "speech" if report.ok else "REJECTED"
        reasons = f" ({'; '.join(report.reasons)})" if report.reasons else ""
        print(f"{path}\t{verdict}\t{report.summary()}{reasons}")
        bad += not report.ok
    raise SystemExit(1 if bad else 0)


def cmd_provision(args: argparse.Namespace) -> None:
    """``plaud-fetch provision``: one-time PLAUD developer-API provisioning."""
    from .credentials import provision_note_pro

    token = None
    if args.user_token_stdin:
        token = (
            getpass.getpass("User Access Token: ") if sys.stdin.isatty() else sys.stdin.readline()
        ).strip()
        if not token:
            fail("no User Access Token given")
    elif args.partner_config is None:
        fail("give --partner-config or --user-token-stdin")
    try:
        path = provision_note_pro(
            args.sn, user_token=token, config=args.partner_config, directory=args.config_dir
        )
    except (RuntimeError, ValueError, KeyError) as error:
        fail(f"provisioning stopped: {error}")
    print(f"Device credentials stored in {path}")


def cmd_bootstrap_init(args: argparse.Namespace) -> None:
    """``plaud-fetch bootstrap-init``: key for the Android bootstrap app."""
    from .credentials import init_bootstrap_key

    print(f"Public key for the Android bootstrap app: {init_bootstrap_key(args.config_dir)}")


def cmd_bootstrap_import(args: argparse.Namespace) -> None:
    """``plaud-fetch bootstrap-import``: store the Android bootstrap export."""
    from .credentials import CredentialError, import_bootstrap_package

    try:
        path = import_bootstrap_package(args.package, args.config_dir)
    except (CredentialError, ValueError, KeyError) as error:
        fail(f"import failed: {error}")
    print(f"Device credentials stored in {path}")


def build_parser() -> argparse.ArgumentParser:
    """The command-line parser with all subcommands."""
    parser = argparse.ArgumentParser(
        prog="plaud-fetch",
        description="Fetch Plaud Note Pro recordings over Bluetooth into meeting folders.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=None,
        help="credentials directory (default ~/.config/plaud-fetch)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def window_options(p: argparse.ArgumentParser) -> None:
        """Add the folder and time-window options to a subcommand."""
        p.add_argument(
            "folder",
            nargs="?",
            type=Path,
            help="meeting folder YYYYMMDD_<Name> (default: current directory)",
        )
        p.add_argument("--date", help="day YYYY-MM-DD (default: from the folder name)")
        p.add_argument(
            "--from",
            dest="start",
            metavar="TIME",
            help="earliest recording start: HH:MM, YYYY-MM-DD or 'YYYY-MM-DD HH:MM'",
        )
        p.add_argument("--to", dest="end", metavar="TIME", help="latest recording start (excluded)")
        p.add_argument("--all", action="store_true", help="every recording on the device")

    def ble_options(p: argparse.ArgumentParser) -> None:
        """Add the Bluetooth scan option to a subcommand."""
        p.add_argument("--scan-seconds", type=float, default=30.0)

    p = sub.add_parser("scan", help="show Plaud devices advertising nearby (no login)")
    p.add_argument("--seconds", type=float, default=15.0)
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("status", help="battery, storage and recording count")
    ble_options(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("pair", help="first connection; --force-clear replaces an old pairing")
    ble_options(p)
    p.add_argument(
        "--force-clear",
        action="store_true",
        help="make the device drop the client key it is paired with (e.g. the phone app's)",
    )
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_pair)

    p = sub.add_parser(
        "release", help="remove this computer's key from the device (give it back to the phone)"
    )
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("list", help="list device recordings in the folder's time window")
    window_options(p)
    ble_options(p)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("fetch", help="download recordings of the window as mp3/<start>.mp3")
    window_options(p)
    ble_options(p)
    p.add_argument(
        "--delete",
        action="store_true",
        help="delete each recording from the device once its MP3 is verified as speech",
    )
    p.add_argument("--yes", action="store_true", help="do not ask before each delete")
    p.add_argument("--dry-run", action="store_true", help="show what would be done")
    p.add_argument(
        "--redo",
        action="store_true",
        help="download again even if the folder already has the recording",
    )
    p.add_argument(
        "--keep-raw", action="store_true", help="keep the encrypted download in the cache"
    )
    p.add_argument("--bitrate", default="32k", help="MP3 bitrate (default 32k, 16 kHz mono)")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("calibrate", help="build the speech reference from recorded meetings")
    p.add_argument(
        "dirs",
        nargs="*",
        type=Path,
        help="folders searched for mp3/*.mp3 (default ~/Documents/LaTex/{Report,Pitch,Management})",
    )
    p.set_defaults(func=cmd_calibrate)

    p = sub.add_parser("check", help="speech-check audio files against the reference")
    p.add_argument("files", nargs="+", type=Path)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser(
        "provision", help="one-time PLAUD developer-API provisioning of the device identity"
    )
    p.add_argument("--sn", required=True, help="Note Pro serial number (starts with 881)")
    p.add_argument(
        "--partner-config",
        type=Path,
        help="JSON with clientId, clientSecret, userId (see plaud-direct-pc-research)",
    )
    p.add_argument(
        "--user-token-stdin",
        action="store_true",
        help="read an existing PLAUD User Access Token instead",
    )
    p.set_defaults(func=cmd_provision)

    p = sub.add_parser(
        "bootstrap-init", help="create the key the Android bootstrap app encrypts to"
    )
    p.set_defaults(func=cmd_bootstrap_init)

    p = sub.add_parser("bootstrap-import", help="import the Android bootstrap export")
    p.add_argument("package", type=Path, help="pc-bootstrap-v1.json pulled from the phone")
    p.set_defaults(func=cmd_bootstrap_import)
    return parser


def main(argv: list[str] | None = None) -> None:
    """Entry point of the ``plaud-fetch`` command.

    :param argv: arguments (default ``sys.argv[1:]``)
    """
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except KeyboardInterrupt:
        fail("interrupted")
    except ImportError as error:
        fail(f"{error}; install python3-bleak (apt) and the plaud-pc-bridge package")
    except RuntimeError as error:  # DeviceError, CredentialError, AudioError, ...
        fail(str(error))


if __name__ == "__main__":
    main()
