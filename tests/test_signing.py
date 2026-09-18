"""Wallet signing as used by APIClient.create_signature and miner_cli.

The challenge server (bittensor 11) verifies these sr25519 signatures with
its own primitives, so the signature *format* must not change across the
upgrade: raw-message sr25519 signature, 64 bytes, hex encoded.
"""
import json
import os
import shutil
import subprocess

import pytest
from bittensor_wallet import Keypair

MESSAGE = "chipforge-test-message:challenge-42:2026-09-18T00:00:00+00:00"


def test_hotkey_sign_str_produces_64_byte_sr25519_signature(temp_wallet):
    sig = temp_wallet.hotkey.sign(data=MESSAGE)
    assert isinstance(sig, (bytes, bytearray))
    assert len(sig) == 64
    hex_sig = sig.hex()
    assert len(hex_sig) == 128
    assert temp_wallet.hotkey.verify(MESSAGE, sig)


def test_signature_verifies_from_ss58_only(temp_wallet):
    """Server side only has the ss58 hotkey, never the keypair."""
    sig = temp_wallet.hotkey.sign(data=MESSAGE)
    pub_only = Keypair(ss58_address=temp_wallet.hotkey.ss58_address)
    assert pub_only.verify(MESSAGE, sig)
    assert not pub_only.verify(MESSAGE + "x", sig)


def test_signature_does_not_verify_with_other_key(temp_wallet, second_wallet):
    sig = temp_wallet.hotkey.sign(data=MESSAGE)
    assert not second_wallet.hotkey.verify(MESSAGE, sig)


def test_api_client_create_signature_matches_wallet(temp_wallet):
    """APIClient.create_signature is the exact call the validator makes."""
    from types import SimpleNamespace

    from validator_utils.api_client import APIClient

    config = SimpleNamespace(challenge_api_url="http://localhost:1", validator_secret_key="s")
    client = APIClient(config, temp_wallet, session=None)
    hex_sig = client.create_signature(MESSAGE)
    assert temp_wallet.hotkey.verify(MESSAGE, bytes.fromhex(hex_sig))


def test_miner_cli_create_signature_matches_wallet(temp_wallet, monkeypatch):
    from types import SimpleNamespace

    import bittensor as bt

    from python_scripts import miner_cli

    monkeypatch.setattr(bt, "Wallet", lambda config=None, **kw: temp_wallet)
    submitter = miner_cli.SolutionSubmitter(SimpleNamespace(api_url="http://localhost:1"))
    hex_sig = submitter.create_signature(MESSAGE)
    assert temp_wallet.hotkey.verify(MESSAGE, bytes.fromhex(hex_sig))


# ---------------------------------------------------------------------------
# Cross-environment check: sign here (bittensor 10 / bittensor-wallet 4.1),
# verify inside the challenge server's env (bittensor 11 + bittensor_core).
# ---------------------------------------------------------------------------
SERVER_ENV = os.environ.get("CHIPFORGE_SERVER_CONDA_ENV", "chipforge-server")

VERIFY_SNIPPET = r"""
import json, sys
from bittensor import sp_core
payload = json.loads(sys.stdin.read())
ok = sp_core.verify(payload["message"].encode(), bytes.fromhex(payload["signature"]), payload["ss58"], sp_core.CRYPTO_SR25519)
bad = sp_core.verify((payload["message"] + "!").encode(), bytes.fromhex(payload["signature"]), payload["ss58"], sp_core.CRYPTO_SR25519)
print(json.dumps({"ok": bool(ok), "tampered_ok": bool(bad), "version": __import__("bittensor").__version__}))
"""


def _server_env_available() -> bool:
    if not shutil.which("conda"):
        return False
    r = subprocess.run(["conda", "env", "list"], capture_output=True, text=True)
    return any(line.split() and line.split()[0] == SERVER_ENV for line in r.stdout.splitlines())


@pytest.mark.crossenv
@pytest.mark.skipif(not _server_env_available(), reason=f"conda env '{SERVER_ENV}' not found")
def test_signature_verifies_in_challenge_server_env(temp_wallet):
    sig_hex = temp_wallet.hotkey.sign(data=MESSAGE).hex()
    payload = json.dumps({"message": MESSAGE, "signature": sig_hex, "ss58": temp_wallet.hotkey.ss58_address})
    r = subprocess.run(
        ["conda", "run", "-n", SERVER_ENV, "--no-capture-output", "python", "-c", VERIFY_SNIPPET],
        input=payload,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0, r.stderr
    result = json.loads(r.stdout.strip().splitlines()[-1])
    assert result["version"].startswith("11."), result
    assert result["ok"] is True, result
    assert result["tampered_ok"] is False, result
