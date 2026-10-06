"""Device credentials for the Plaud BLE session, kept for the current Linux user.

The material (PLAUD-issued serial-number signature, RSA key pair and bind
token) is what lets the Note Pro accept this computer as its client. It is
stored like an SSH private key: plain JSON in a mode 0600 file inside a mode
0700 directory. Loading refuses files that other users can read.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MATERIAL_FILE = "device-auth.json"
BOOTSTRAP_PRIVATE_FILE = "pc-bootstrap-private.pem"
BOOTSTRAP_PUBLIC_FILE = "pc-bootstrap-public.pem"
# Same envelope as plaud_pc.bootstrap, produced by the Android bootstrap app.
BOOTSTRAP_AAD = b"plaud-pc-bootstrap-v1"
REQUIRED = ("sn", "signature", "bindToken", "rsaPublicKey", "rsaPrivateKey")


class CredentialError(RuntimeError):
    """Credentials are missing, incomplete or readable by other users."""


def default_dir() -> Path:
    """The credentials directory, ``$XDG_CONFIG_HOME/plaud-fetch``.

    :returns: usually ``~/.config/plaud-fetch``
    """
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "plaud-fetch"


def _ensure_private_dir(directory: Path) -> None:
    """Create ``directory`` if needed and restrict it to its owner (0700)."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)


def _write_private(path: Path, data: bytes) -> None:
    """Write a file atomically, with mode 0600 from the start.

    :param path: file to create or replace
    :param data: its content
    """
    temporary = path.with_name(path.name + ".part")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    temporary.replace(path)


def _check_private(path: Path) -> None:
    """Refuse a secret file that group or others can access.

    :raises CredentialError: when the mode allows group or other access
    """
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CredentialError(
            f"{path} is accessible to other users (mode {stat.S_IMODE(mode):o}); "
            f"run: chmod 600 {path}"
        )


def validate_material(material: dict[str, Any]) -> None:
    """Check the device identity has every field the BLE handshake needs.

    :param material: identity with ``version`` 1 and the fields in ``REQUIRED``
    :raises CredentialError: when a field is missing or empty
    """
    if material.get("version") != 1 or any(not material.get(key) for key in REQUIRED):
        raise CredentialError("incomplete Plaud authentication material")


def save_material(material: dict[str, Any], directory: Path | None = None) -> Path:
    """Validate and store the device identity (mode 0600).

    :param material: identity, as from provisioning or the bootstrap app
    :param directory: credentials directory (default :func:`default_dir`)
    :returns: path of the stored ``device-auth.json``
    :raises CredentialError: when the identity is incomplete
    """
    directory = directory or default_dir()
    validate_material(material)
    _ensure_private_dir(directory)
    path = directory / MATERIAL_FILE
    _write_private(path, json.dumps(material, separators=(",", ":")).encode("utf-8"))
    return path


def load_material(directory: Path | None = None) -> dict[str, Any]:
    """Load the device identity for a BLE session.

    :param directory: credentials directory (default :func:`default_dir`)
    :returns: the identity; never log it, it holds the RSA private key
    :raises CredentialError: when absent, incomplete or not private
    """
    directory = directory or default_dir()
    path = directory / MATERIAL_FILE
    if not path.exists():
        raise CredentialError(
            f"no device credentials in {directory}; run 'plaud-fetch provision' "
            "or 'plaud-fetch bootstrap-import' first"
        )
    _check_private(path)
    material = json.loads(path.read_text(encoding="utf-8"))
    validate_material(material)
    return material


def init_bootstrap_key(directory: Path | None = None) -> Path:
    """Create (once) the RSA key the Android bootstrap app encrypts its export to.

    :param directory: credentials directory (default :func:`default_dir`)
    :returns: the public key file to copy to the phone
    """
    directory = directory or default_dir()
    _ensure_private_dir(directory)
    private_path = directory / BOOTSTRAP_PRIVATE_FILE
    public_path = directory / BOOTSTRAP_PUBLIC_FILE
    if private_path.exists():
        _check_private(private_path)
        key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        _write_private(
            private_path,
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
    public_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    return public_path


def import_bootstrap_package(package: Path, directory: Path | None = None) -> Path:
    """Decrypt the Android bootstrap export (RSA-OAEP + AES-GCM) and store it.

    :param package: ``pc-bootstrap-v1.json`` pulled from the phone
    :param directory: credentials directory (default :func:`default_dir`)
    :returns: path of the stored ``device-auth.json``
    :raises CredentialError: without a bootstrap key or with an unknown version
    """
    directory = directory or default_dir()
    private_path = directory / BOOTSTRAP_PRIVATE_FILE
    if not private_path.exists():
        raise CredentialError("no bootstrap key; run 'plaud-fetch bootstrap-init' first")
    _check_private(private_path)
    envelope = json.loads(package.read_text(encoding="utf-8"))
    if envelope.get("version") != 1:
        raise CredentialError("unsupported bootstrap package version")
    key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
    aes_key = key.decrypt(
        base64.b64decode(envelope["wrappedKey"]),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    plaintext = AESGCM(aes_key).decrypt(
        base64.b64decode(envelope["nonce"]),
        base64.b64decode(envelope["ciphertext"]),
        BOOTSTRAP_AAD,
    )
    return save_material(json.loads(plaintext.decode("utf-8")), directory)


def _curl_config_value(value: str) -> str:
    """Quote a value for a curl ``--config`` file."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def curl_post(url: str, *, headers: dict[str, str], body: bytes = b"") -> dict[str, Any]:
    """POST with curl, the client PLAUD's API documentation uses.

    PLAUD's edge refuses Python's default HTTP client (HTTP 403, code 1010) but
    accepts curl as is. Headers and body go to curl on stdin, so the client
    secret and tokens never appear on a command line. Same contract as
    ``plaud_pc.cloud_bootstrap._post``, which it replaces.

    :param url: HTTPS endpoint
    :param headers: request headers (may hold secrets)
    :param body: request body
    :returns: the decoded JSON response
    :raises RuntimeError: on a network error or any status other than 200
    """
    lines = [f"url = {_curl_config_value(url)}", 'request = "POST"']
    lines += [f"header = {_curl_config_value(f'{k}: {v}')}" for k, v in headers.items()]
    lines.append(f"data-binary = {_curl_config_value(body.decode('utf-8'))}")
    result = subprocess.run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--max-time",
            "30",
            "--config",
            "-",
            "--write-out",
            "\n%{http_code}",
        ],
        input="\n".join(lines).encode("utf-8"),
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Cannot reach PLAUD API: {result.stderr.decode().strip()}")
    text, _, code = result.stdout.decode("utf-8", errors="replace").rpartition("\n")
    if code != "200":
        if code == "403" and "1010" in text:
            raise RuntimeError("PLAUD rejected this API client at its edge (HTTP 403, code 1010)")
        raise RuntimeError(f"PLAUD API returned HTTP {code}")
    return json.loads(text)


def provision_note_pro(
    serial: str,
    *,
    user_token: str | None = None,
    config: Path | None = None,
    directory: Path | None = None,
) -> Path:
    """One-time PLAUD developer-API provisioning (gen-key + sn-sign).

    Reuses ``plaud_pc.cloud_bootstrap``, swapping its Windows DPAPI store for
    ours and its HTTP client for curl. Only the device serial and the developer
    credentials or user token are sent; no recording or transcript leaves this
    computer.

    :param serial: Note Pro serial number (starts with 881)
    :param user_token: an existing PLAUD User Access Token, instead of ``config``
    :param config: JSON with ``clientId``, ``clientSecret`` and ``userId``
    :param directory: credentials directory (default :func:`default_dir`)
    :returns: path of the stored ``device-auth.json``
    :raises RuntimeError: when PLAUD refuses a call
    :raises ValueError: on an invalid serial, token or configuration
    """
    from plaud_pc import cloud_bootstrap

    directory = directory or default_dir()
    saved: dict[str, Path] = {}

    def _save(_state_dir: Path, material: dict[str, Any]) -> dict[str, Any]:
        """Stand-in for the bridge's DPAPI store."""
        saved["path"] = save_material(material, directory)
        return {"version": 1, "serialSuffix": str(material["sn"])[-4:]}

    originals = (cloud_bootstrap.save_device_material, cloud_bootstrap._post)
    cloud_bootstrap.save_device_material = _save
    cloud_bootstrap._post = curl_post
    try:
        cloud_bootstrap.provision_note_pro(config, directory, serial, user_token)
    finally:
        cloud_bootstrap.save_device_material, cloud_bootstrap._post = originals
    return saved["path"]
