# plaud-fetch

Fetch recordings from a **Plaud Note Pro** over Bluetooth, with no cloud and no
phone, into the meeting folders under `~/Documents/LaTex/{Report,Pitch,Management}`.
Each recording becomes `mp3/YYYY-MM-DD_HH_MM_SS.mp3` (local start time), the
layout `transcribe_remote.sh` and `journal.sh` already read. A recording can
then be deleted from the device, but only after its MP3 is verified to decode
fully and to contain speech like your other meetings.

```
cd ~/Documents/LaTex/Report/20261007_CCM4_Day2
plaud-fetch list                          # the device's recordings of 2026-10-07
plaud-fetch fetch                         # copy them to mp3/
plaud-fetch fetch --from 14:00 --to 16:30 --delete
```

Installation, provisioning and pairing: see [INSTALL.md](INSTALL.md).

## Commands

| Command | Does |
|---|---|
| `fetch [FOLDER]` | Download the window's recordings to `FOLDER/mp3/` and speech-check them. `--delete` then deletes each verified one from the device (asks first, `--yes` skips). `--dry-run`, `--redo`, `--keep-raw`, `--bitrate`. |
| `list [FOLDER]` | The device's recordings in the window, marking those already in the folder. |
| `status` | Battery, storage, number of recordings. |
| `check FILE...` | Speech-check audio files against the reference. |
| `calibrate [DIR...]` | Build the speech reference from recorded meetings. |
| `scan` | Plaud recorders advertising nearby, with serial numbers (no connection). |
| `pair` | First connection after provisioning. |
| `release` | Remove this computer's key so the phone app can pair again. |
| `provision`, `bootstrap-init`, `bootstrap-import` | One-time device identity (see INSTALL.md). |

**Window** (for `fetch` and `list`): a folder named `YYYYMMDD_<Name>` selects
that day (`FOLDER` defaults to the current directory). `--from`/`--to` take
`HH:MM`, `YYYY-MM-DD` or `'YYYY-MM-DD HH:MM'`: they narrow the window, or
extend it across days (`--from 2026-09-30` for a seminar folder dated
2026-10-01). `--date` dates an undated folder such as `Management/ORUS/Florian`;
`--all` takes every recording. A recording counts when it *starts* in the
window. One already in `mp3/` (within 5 s of its start) is skipped.

## How it works

- **Transport:** Bluetooth LE through
  [plaud-direct-pc-research](https://github.com/Shawn-TKD/plaud-direct-pc-research)
  (`pc-bridge`, Apache-2.0, cloned to `~/dev/plaud-direct-pc-research`). The
  bridge implements the device's RSA + ChaCha20-Poly1305 handshake, file list
  and transfer. plaud-fetch adds a Linux credential store, device matching by
  serial number (the Note Pro sends its serial as raw bytes, which the bridge
  does not parse), one session per run, delete, release, MP3 conversion and the
  speech check. The USB port shows up as a serial device (`/dev/ttyACM0`,
  "RealTek USB Audio Device") with no file access, and Wi-Fi Fast Transfer is
  undocumented, so Bluetooth is the only path.
- **Audio:** the device sends a `PLAUD.AI` container whose audio key is
  wrapped with the device RSA key. On the Note Pro (firmware V1.7.0) it holds an
  Ogg Opus stream, 48 kHz mono. Raw 80-byte Opus packets, as the NotePin S
  sends, are wrapped into Ogg as well. ffmpeg encodes a 16 kHz mono MP3
  (`--bitrate`, default 32k), the format of the existing meeting recordings.
- **Delete:** as in Plaud's official SDK v1.0.57 (`PlaudDeviceAgent.deleteFile`,
  decompiled). The request is CMD 30 with `[session_id:u32 LE]`. The device
  answers on CMD 31 with `[session_id:u32][status:u8]`; status 0 is success.
  plaud-fetch then checks that the recording is gone from the file list.

## Speech check (the delete gate)

`plaud-fetch calibrate` measures every `mp3/*.mp3` already in your meeting
folders, minute by minute. It measures speech-band energy, voicing (pitch
periodicity), spectral flatness, syllabic 2-8 Hz modulation and clipping, and
stores their mean and covariance in `~/.config/plaud-fetch/speech-reference.json`
(statistics only, no audio). A recording passes when all of these hold:

- ffmpeg decodes the MP3 with no errors;
- its duration equals the source's (within 0.5 %);
- it holds at least 10 s of speech;
- at most 20 % of its non-quiet minutes are outliers (beyond the 99.5th
  percentile Mahalanobis distance of your meetings). Pauses are not judged.

A deletion also needs the ledger entry of that fetch and an MP3 unchanged
since (same SHA-256).

Validation:

- Built from the 82 June recordings, the reference passed all 27 later
  recordings (August to October).
- It rejected every negative: silence, white, pink and brown noise, a tone,
  clipped speech, random Opus packets, and real speech decoded with the wrong
  frame size.
- On the Note Pro, a 90 s speech played through a laptop speaker about 1 m
  away passed like the original clip did (0 of 2 minutes unlike the meetings),
  despite an 11 dB level loss and 11 dB SNR.

A recording that fails the check is still converted to MP3 so you can listen
to it. It stays on the device, and its encrypted download stays in
`~/.cache/plaud-fetch/`. Recordings shorter than about 10 s always fail; delete
those by hand if needed.

## Files

| Path | Content |
|---|---|
| `~/.config/plaud-fetch/device-auth.json` | device identity (mode 0600; private like an SSH key) |
| `~/.config/plaud-fetch/speech-reference.json` | speech reference statistics |
| `~/.local/share/plaud-fetch/ledger.tsv` | every fetch and delete: session, MP3, SHA-256, verdict |
| `~/.cache/plaud-fetch/` | downloads in progress (resumable) and rejected recordings |

## Code

| Module | Role |
|---|---|
| `plaud_fetch/cli.py` | commands and the fetch pipeline |
| `plaud_fetch/device.py` | Bluetooth: scan, session, list, download, delete, release |
| `plaud_fetch/credentials.py` | device identity storage, provisioning, Android bootstrap import |
| `plaud_fetch/audio.py` | decrypt, Ogg, MP3, decoding checks |
| `plaud_fetch/speech.py` | speech features, reference, verdict |
| `plaud_fetch/meeting.py` | meeting folders, time windows, file names |
| `plaud_fetch/ledger.py` | fetch and delete log |

Tests: `make test` (pytest, `tests/*_test.py`). Lint and format: `ruff check`
and `ruff format`.

## Status

Verified on 2026-10-06 on a Note Pro V1.7.0:

- provisioning, pairing, listing and download;
- decryption and MP3 encoding, with MP3s faithful to what was said;
- the speech check, and delete with its verification;
- `release`, which gave the device back to the iPhone app.

Long recordings (hours) have not been transferred yet. Over Bluetooth, expect
several minutes per recorded hour.

## Licence

[Unlicense](LICENSE). The pc-bridge dependency is Apache-2.0 and is not
included here.
