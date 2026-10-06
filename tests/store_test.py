import base64
import json
import os

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from plaud_fetch import credentials, ledger

MATERIAL = {
    "version": 1,
    "sn": "8810AABBCCDDEEFF",
    "signature": "c2ln",
    "bindToken": "0" * 32,
    "rsaPublicKey": "pub",
    "rsaPrivateKey": "priv",
}


def test_material_is_private_and_round_trips(tmp_path):
    path = credentials.save_material(MATERIAL, tmp_path / "cfg")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    assert credentials.load_material(tmp_path / "cfg") == MATERIAL


def test_readable_material_is_refused(tmp_path):
    path = credentials.save_material(MATERIAL, tmp_path)
    os.chmod(path, 0o644)
    with pytest.raises(credentials.CredentialError):
        credentials.load_material(tmp_path)


def test_incomplete_material_is_refused(tmp_path):
    with pytest.raises(credentials.CredentialError):
        credentials.save_material({**MATERIAL, "signature": ""}, tmp_path)
    with pytest.raises(credentials.CredentialError):
        credentials.load_material(tmp_path / "none")


def test_android_bootstrap_envelope(tmp_path):
    public_path = credentials.init_bootstrap_key(tmp_path)
    public = serialization.load_pem_public_key(public_path.read_bytes())
    # Encrypt as the Android bootstrap app does (PcBootstrapExporter).
    key = AESGCM.generate_key(bit_length=256)
    nonce = os.urandom(12)
    envelope = {
        "version": 1,
        "wrappedKey": base64.b64encode(
            public.encrypt(
                key,
                padding.OAEP(
                    mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None
                ),
            )
        ).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(
            AESGCM(key).encrypt(nonce, json.dumps(MATERIAL).encode(), credentials.BOOTSTRAP_AAD)
        ).decode(),
    }
    package = tmp_path / "pc-bootstrap-v1.json"
    package.write_text(json.dumps(envelope))
    credentials.import_bootstrap_package(package, tmp_path)
    assert credentials.load_material(tmp_path) == MATERIAL


def test_ledger_verifies_unchanged_mp3(tmp_path):
    book = tmp_path / "ledger.tsv"
    mp3 = tmp_path / "2026-09-29_15_24_42.mp3"
    mp3.write_bytes(b"audio")
    entry = ledger.Entry(
        ledger.now(),
        "fetched",
        42,
        "2026-09-29T15:24:42",
        5,
        str(mp3.resolve()),
        ledger.sha256(mp3),
        "speech",
        "a\tb",
    )
    ledger.append(entry, book)
    assert ledger.verified_fetch(42, mp3, book).session_id == 42
    assert ledger.verified_fetch(43, mp3, book) is None
    mp3.write_bytes(b"edited")
    assert ledger.verified_fetch(42, mp3, book) is None


def test_ledger_rejected_fetch_is_not_verified(tmp_path):
    book = tmp_path / "ledger.tsv"
    mp3 = tmp_path / "x.mp3"
    mp3.write_bytes(b"noise")
    ledger.append(
        ledger.Entry(
            ledger.now(), "fetched", 7, "", 5, str(mp3.resolve()), ledger.sha256(mp3), "rejected"
        ),
        book,
    )
    assert ledger.verified_fetch(7, mp3, book) is None
