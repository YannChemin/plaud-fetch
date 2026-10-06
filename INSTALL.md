# Installing and pairing plaud-fetch

Debian 13, Python 3.11 or newer, and a Bluetooth LE adapter. Pairing is done
once per device; after that every session is offline.

## 1. Install

```
sudo apt install python3-bleak python3-numpy ffmpeg curl
git clone https://github.com/Shawn-TKD/plaud-direct-pc-research ~/dev/plaud-direct-pc-research
cd ~/dev/plaud-fetch
make install        # venv in ~/.local/share/plaud-fetch, command in ~/.local/bin
make test           # pytest, about a minute
```

The venv uses Debian's `python3-bleak`, `python3-cryptography` and
`python3-numpy` (`--system-site-packages`). The bridge from
plaud-direct-pc-research is installed `--no-deps`, because it pins
`cryptography>=45` while Debian ships 43, which has every primitive it uses.
Override the locations with `make install BRIDGE=... VENV=... BINDIR=...`.

## 2. Speech reference

```
plaud-fetch calibrate
```

This measures the recordings already in `~/Documents/LaTex/{Report,Pitch,Management}/**/mp3/`
(or the folders you give) and writes `~/.config/plaud-fetch/speech-reference.json`.
Without it, the speech check falls back to fixed limits. Rebuild it when new
kinds of recordings appear.

## 3. Bluetooth

```
bluetoothctl power on
plaud-fetch scan
```

`scan` lists Plaud recorders nearby with their serial numbers, without
connecting. The Note Pro shows up as `Plaud Note Pro  ...  SN 8810...`. Other
people's recorders may appear too; plaud-fetch only ever connects to the
serial it was provisioned for.

## 4. Provision (one time, needs the network)

The Note Pro only accepts a client holding a PLAUD-issued signature over its
serial number, an RSA key pair and a bind identity. Only PLAUD's developer API
issues these. This step sends the serial and your developer credentials to
PLAUD; no audio or transcript leaves the computer.

1. On https://portal.plaud.ai/, create an app. You get a Client ID and a Client
   Secret. Choose a user id of 6-120 characters that you will never change.
2. Write them to a private file and provision:
   ```
   umask 077
   cat > ~/.config/plaud-fetch/partner.json <<EOF
   {"clientId": "client_...", "clientSecret": "sk_...", "userId": "yann-chemin-notepro"}
   EOF
   plaud-fetch provision --sn 8810... --partner-config ~/.config/plaud-fetch/partner.json
   ```
   With a User Access Token instead: `plaud-fetch provision --sn 8810... --user-token-stdin`.
3. The identity is now in `~/.config/plaud-fetch/device-auth.json` (mode 0600).
   Delete `partner.json` (`shred -u`); it is only needed to provision again.
   Rotate the Client Secret in the portal if it was ever exposed.

PLAUD's edge refuses Python's own HTTP client (HTTP 403, code 1010), so these
calls go through `curl`, the client PLAUD's documentation uses.

If the developer API stays closed to you, use the Android bootstrap app from
plaud-direct-pc-research instead. Run `plaud-fetch bootstrap-init`, copy
`~/.config/plaud-fetch/pc-bootstrap-public.pem` to the app, then run
`plaud-fetch bootstrap-import pc-bootstrap-v1.json`.

## 5. Pair (one time)

The Note Pro has one owner and one client. If it was set up with the Plaud
phone app, the app's account owns it. That account has to release it **over
Bluetooth**, so the order matters:

1. Open the Plaud app with the phone near the Note Pro, so that it is
   **connected** to it.
2. Unbind the device in the app. In the iOS app this is **Erase and Remove** in
   the device settings; it removed the device, and recordings already
   downloaded into the app stayed there. **Disconnect** does not unbind.
3. Switch the phone's Bluetooth off.
4. `plaud-fetch pair`, which should print `Paired: Plaud Note Pro, battery ..%, N recordings.`

Unbinding while the phone is offline never reaches the device.

## 6. Check the whole chain

1. Make a test recording of a minute or two of speech.
2. In a scratch folder named like a meeting (`20261006_test`), run
   `plaud-fetch fetch` and listen to `mp3/*.mp3`.
3. Run `plaud-fetch fetch --delete` there and confirm. The recording should
   disappear from `plaud-fetch list --all`.

## Giving the device back to the phone

```
plaud-fetch release
```

This removes this computer's key from the device without enrolling any. The
owner's phone app can then pair again; recordings and owner are not touched.
It is also the fix when the phone app no longer finds the device (see below).

## Troubleshooting

| Message | Meaning and fix |
|---|---|
| `PLAUD rejected this API client at its edge (HTTP 403, code 1010)` | Only for the bridge's own Python client; `plaud-fetch provision` uses curl. Check that `curl` is installed. |
| `still paired with another client key (the phone app?)` | The phone app's key is still enrolled. Unbind in the app **while connected** (section 5). |
| `accepted this computer's key but not its bind identity` | The key was force-cleared but the phone account still owns the device. Run `plaud-fetch release`, let the phone app pair again, then follow section 5. |
| `no advertisement from serial ...` | The device is asleep, out of range or connected to the phone. Wake it with its button and switch the phone's Bluetooth off. |
| `No powered Bluetooth adapters found` | `bluetoothctl power on`. |
| `ATT error: 0x0e (Unlikely Error)` | Often right after the adapter powers up; retry after about 10 s. |
| The phone app no longer finds the device | Run `plaud-fetch release`, then search again from the app with the phone online. |

`pair --force-clear` sends the signature as Plaud's SDK recovery does
(command 0xFE20): the device drops its enrolled client key. It does not change
the owner. On its own it leaves the bind refused and can hide the device from
the owner's app, so use section 5 instead.

## Update and uninstall

The package is installed editable, so code changes are live. Rerun
`make install` only after changing `pyproject.toml` or the bridge.

```
make uninstall      # venv and command
rm -r ~/.config/plaud-fetch ~/.local/share/plaud-fetch ~/.cache/plaud-fetch   # identity, reference, ledger, cache
```

Removing `~/.config/plaud-fetch` loses the device identity. Release the device
first (`plaud-fetch release`) if the phone app should take it back.
