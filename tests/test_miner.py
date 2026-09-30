"""Miner neuron and miner CLI (Phase F): validator-only axon, challenge download that
works for server-hosted URLs and retries after failures, CLI calls the server accepts."""
import asyncio
import io
import sys
import zipfile
from types import SimpleNamespace

import numpy as np
import pytest
from bittensor_wallet import Keypair

from neurons import miner as miner_mod
from neurons.miner import ChipForgeMiner, resolve_download_url

API = "https://api.example"


def make_zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


@pytest.fixture()
def miner(tmp_path, monkeypatch):
    """A ChipForgeMiner without chain objects: only the attributes the logic uses."""
    monkeypatch.setenv("MINER_CHALLENGE_DIR", str(tmp_path / "challenges"))
    m = ChipForgeMiner.__new__(ChipForgeMiner)
    m.config = SimpleNamespace(challenge_api_url=API)
    m.wallet = SimpleNamespace(hotkey=SimpleNamespace(ss58_address="5Miner"))
    m.metagraph = SimpleNamespace(hotkeys=["5Val", "5Miner2"], validator_permit=np.array([True, False]),
                                  S=np.array([1000.0, 1.0]))
    m.challenge_dir = tmp_path / "challenges"
    m.challenge_dir.mkdir()
    m.poll_seconds, m.metagraph_sync_seconds = 300, 600
    m.max_download_bytes, m.max_extracted_bytes, m.max_zip_members = 10 << 20, 10 << 20, 100
    m.require_validator_permit = True
    m.current_challenge_id = m.current_github_url = None
    m.downloaded_challenges = set()
    m._poll_now, m._stop = asyncio.Event(), asyncio.Event()
    return m


def serve(monkeypatch, miner, payloads):
    """Replace the HTTP fetch: payloads is a list of bytes/Exception consumed per download."""
    urls = []

    def fetch(url, dest):
        urls.append(url)
        item = payloads.pop(0)
        if isinstance(item, Exception):
            raise item
        if len(item) > miner.max_download_bytes:
            raise miner_mod.ChallengeDownloadError("too big")
        dest.write_bytes(item)
    monkeypatch.setattr(miner, "_fetch_to_file", fetch)
    return urls


# --- download URL ------------------------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    (f"{API}/api/v1/challenges/c1/download", f"{API}/api/v1/challenges/c1/download"),   # server-hosted
    # stored link names another deployment (testnet challenge activated with the production URL)
    ("https://api.chipforge.io/api/v1/challenges/c1/download", f"{API}/api/v1/challenges/c1/download"),
    ("https://bucket.s3.amazonaws.com/pending/c1.zip?X-Amz=1", "https://bucket.s3.amazonaws.com/pending/c1.zip?X-Amz=1"),
    ("https://github.com/org/repo", "https://github.com/org/repo/archive/main.zip"),
    ("https://github.com/org/repo.git", "https://github.com/org/repo/archive/main.zip"),
    ("https://github.com/org/repo/tree/dev/sub", "https://github.com/org/repo/archive/dev.zip"),
    ("", f"{API}/api/v1/challenges/c1/download"),
])
def test_resolve_download_url(url, expected):
    assert resolve_download_url("c1", url, API) == expected


# --- download / extract ------------------------------------------------------------------

async def test_download_extracts_and_writes_metadata(miner, monkeypatch):
    urls = serve(monkeypatch, miner, [make_zip({"spec.md": "x", "rtl/top.v": "module"})])
    assert await miner.download_challenge("c1", f"{API}/api/v1/challenges/c1/download")
    d = miner.challenge_dir / "c1"
    assert (d / "rtl/top.v").exists() and (d / "challenge_metadata.json").exists()
    assert urls == [f"{API}/api/v1/challenges/c1/download"]                # not .../archive/main.zip
    assert [p.name for p in miner.challenge_dir.iterdir()] == ["c1"]      # no temp dirs left


async def test_failed_download_leaves_nothing_and_is_retried(miner, monkeypatch):
    serve(monkeypatch, miner, [OSError("404"), make_zip({"a": "b"})])
    assert not await miner.download_challenge("c1", "")
    assert list(miner.challenge_dir.iterdir()) == []                      # old code left an empty dir forever
    assert await miner.download_challenge("c1", "")


async def test_legacy_empty_dir_is_replaced(miner, monkeypatch):
    (miner.challenge_dir / "c1").mkdir()                                   # left by the old miner after a 404
    serve(monkeypatch, miner, [make_zip({"a": "b"})])
    assert await miner.download_challenge("c1", "")
    assert (miner.challenge_dir / "c1" / "a").exists()


async def test_zip_limits(miner, monkeypatch):
    miner.max_zip_members = 3
    serve(monkeypatch, miner, [make_zip({f"f{i}": "x" for i in range(5)})])
    assert not await miner.download_challenge("c1", "")
    miner.max_zip_members, miner.max_extracted_bytes = 100, 1000
    serve(monkeypatch, miner, [make_zip({"big": "0" * 5000})])             # compresses well, expands past limit
    assert not await miner.download_challenge("c1", "")
    assert list(miner.challenge_dir.iterdir()) == []


async def test_unsafe_challenge_id_refused(miner, monkeypatch):
    serve(monkeypatch, miner, [])
    assert not await miner.download_challenge("../x", "")


async def test_poll_downloads_once_and_retries_failures(miner, monkeypatch):
    monkeypatch.setattr(miner, "_get_active_challenge", lambda: {"challenge_id": "c1", "github_url": ""})
    urls = serve(monkeypatch, miner, [OSError("down"), make_zip({"a": "b"})])
    await miner.poll_once()
    assert "c1" not in miner.downloaded_challenges
    await miner.poll_once()
    await miner.poll_once()
    assert "c1" in miner.downloaded_challenges and len(urls) == 2


# --- axon --------------------------------------------------------------------------------

def synapse(hotkey, message="CHALLENGE_ACTIVE:c1:https://x/y:2026-09-26T00:00:00"):
    return SimpleNamespace(dendrite=SimpleNamespace(hotkey=hotkey), message=message, response=None)


async def test_blacklist_only_allows_validators(miner):
    assert await miner.blacklist_simple_message(synapse("5Val")) == (False, "")
    assert (await miner.blacklist_simple_message(synapse("5Miner2")))[0]         # registered, no permit
    assert (await miner.blacklist_simple_message(synapse("5Stranger")))[0]
    miner.require_validator_permit = False
    assert not (await miner.blacklist_simple_message(synapse("5Miner2")))[0]


async def test_challenge_notice_triggers_immediate_poll(miner):
    s = await miner.handle_simple_message(synapse("5Val"))
    assert s.response == "OK" and miner._poll_now.is_set()


# --- config ------------------------------------------------------------------------------

def test_miner_api_url_default_is_https(monkeypatch):
    from neurons.miner import get_config

    monkeypatch.delenv("CHALLENGE_API_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["miner.py", "--netuid", "108"])
    assert get_config().challenge_api_url == "https://api.chipforge.io"


# --- miner CLI ---------------------------------------------------------------------------

@pytest.fixture()
def cli_parse(monkeypatch):
    from python_scripts import miner_cli
    captured = {}

    class FakeCLI:
        def __init__(self, config):
            captured["config"] = config

        def show_status(self):
            captured["cmd"] = "status"

    monkeypatch.setattr(miner_cli, "MinerCLI", FakeCLI)

    def run(argv):
        monkeypatch.setattr(sys, "argv", ["miner_cli.py", *argv])
        miner_cli.main()
        return captured["config"]
    return run


def test_cli_flags_work_after_the_subcommand(cli_parse):
    cfg = cli_parse(["status", "--wallet.name", "w", "--api_url", "http://x:1/"])
    assert cfg.wallet.name == "w" and cfg.api_url == "http://x:1"


def test_cli_flags_before_subcommand_are_not_overwritten(cli_parse):
    cfg = cli_parse(["--wallet.name", "w", "--wallet.hotkey", "h", "status"])
    assert cfg.wallet.name == "w" and cfg.wallet.hotkey == "h"


def test_cli_history_request_is_signed_the_way_the_server_verifies(monkeypatch):
    from python_scripts import miner_cli

    kp = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    sub = miner_cli.SolutionSubmitter.__new__(miner_cli.SolutionSubmitter)
    sub.api_url, sub.miner_hotkey = API, kp.ss58_address
    sub.wallet = SimpleNamespace(hotkey=SimpleNamespace(sign=lambda data: kp.sign(data)))
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen.update(url=url, params=params)
        return SimpleNamespace(status_code=200, json=lambda: {"submissions": [{"submission_id": "s1"}]})
    monkeypatch.setattr(miner_cli.requests, "get", fake_get)
    assert sub.get_miner_submissions("c1") == [{"submission_id": "s1"}]
    p = seen["params"]
    # server: verify_hotkey_signature(hotkey, signature, timestamp) over f"{hotkey}{timestamp}"
    assert kp.verify(f"{kp.ss58_address}{p['timestamp']}".encode(), bytes.fromhex(p["signature"]))


def test_cli_size_limit_matches_server():
    from python_scripts import miner_cli
    assert miner_cli.MAX_FILE_SIZE == 50 * 1024 * 1024


def test_cli_logs_request_is_signed_and_names_the_hotkey(monkeypatch):
    from python_scripts import miner_cli

    kp = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    sub = miner_cli.SolutionSubmitter.__new__(miner_cli.SolutionSubmitter)
    sub.api_url, sub.miner_hotkey = API, kp.ss58_address
    sub.wallet = SimpleNamespace(hotkey=SimpleNamespace(sign=lambda data: kp.sign(data)))
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen.update(url=url, params=params)
        return SimpleNamespace(status_code=200, json=lambda: {"validations": []})
    monkeypatch.setattr(miner_cli.requests, "get", fake_get)
    assert sub.get_evaluation_logs("sub1") == {"validations": []}
    assert seen["url"] == f"{API}/api/v1/submissions/sub1/evaluation_logs"
    p = seen["params"]
    assert p["hotkey"] == kp.ss58_address
    assert kp.verify(f"{kp.ss58_address}{p['timestamp']}".encode(), bytes.fromhex(p["signature"]))


def _signed_submitter(monkeypatch):
    from python_scripts import miner_cli
    kp = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
    sub = miner_cli.SolutionSubmitter.__new__(miner_cli.SolutionSubmitter)
    sub.api_url, sub.miner_hotkey = API, kp.ss58_address
    sub.wallet = SimpleNamespace(hotkey=SimpleNamespace(sign=lambda data: kp.sign(data)))
    return miner_cli, sub, kp


def test_reveal_download_is_signed_and_hash_checked(monkeypatch):
    import hashlib
    miner_cli, sub, kp = _signed_submitter(monkeypatch)
    design = b"PK-revealed"
    seen = {}

    def ok(url, params=None, timeout=None):
        seen.update(url=url, params=params)
        return SimpleNamespace(status_code=200, content=design, headers={"X-File-SHA256": hashlib.sha256(design).hexdigest()})
    monkeypatch.setattr(miner_cli.requests, "get", ok)
    got = sub.download_revealed_design("s1")
    assert got["content"] == design and seen["url"] == f"{API}/api/v1/reveals/s1/download"
    assert kp.verify(f"{kp.ss58_address}{seen['params']['timestamp']}".encode(), bytes.fromhex(seen["params"]["signature"]))

    monkeypatch.setattr(miner_cli.requests, "get",
                        lambda *a, **k: SimpleNamespace(status_code=200, content=b"tampered", headers={"X-File-SHA256": hashlib.sha256(design).hexdigest()}))
    assert sub.download_revealed_design("s1") is None                      # mismatching bytes are discarded

    monkeypatch.setattr(miner_cli.requests, "get",
                        lambda *a, **k: SimpleNamespace(status_code=403, json=lambda: {"detail": {"reason": "scheduled"}}, text=""))
    assert sub.download_revealed_design("s1") is None


def test_cli_wallet_defaults_come_from_miner_settings(cli_parse, monkeypatch, tmp_path):
    monkeypatch.setenv("MINER_WALLET_NAME", "m_wallet")
    monkeypatch.setenv("MINER_HOTKEY", "m_hot")
    monkeypatch.setenv("MINER_WALLET_DIR", str(tmp_path))
    monkeypatch.setenv("WALLET_NAME", "old_style")            # the pre-split name only fills in when MINER_ is unset
    cfg = cli_parse(["status"])
    assert (cfg.wallet.name, cfg.wallet.hotkey, cfg.wallet.path) == ("m_wallet", "m_hot", str(tmp_path))
    monkeypatch.delenv("MINER_WALLET_NAME")
    assert cli_parse(["status"]).wallet.name == "old_style"
