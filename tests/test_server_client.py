"""Validator <-> challenge server (Phase C): v1+v2 signatures, /validator/sync with legacy
fallback, test-case versioning, secrets from the environment."""
import hashlib
import json
import sys
import time
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from bittensor_wallet import Keypair

from validator_utils.api_client import APIClient


class Resp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.headers = headers or {}

    async def read(self):
        return self._body

    async def text(self):
        return self._body.decode()

    async def json(self):
        return json.loads(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class Session:
    def __init__(self, routes):
        self.routes = routes           # path -> callable(method, kwargs) -> Resp
        self.calls = []

    def request(self, method, url, **kw):
        path = urlparse(url).path
        self.calls.append((method, path, kw))
        return self.routes[path](method, kw)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)


@pytest.fixture()
def keypair():
    return Keypair.create_from_mnemonic(Keypair.generate_mnemonic())   # sr25519, like wallet hotkeys


@pytest.fixture()
def make_client(tmp_path, monkeypatch, keypair):
    monkeypatch.setenv("CHIPFORGE_DATA_DIR", str(tmp_path))

    def make(routes, mode="both"):
        monkeypatch.setenv("SIGNATURE_MODE", mode)
        wallet = SimpleNamespace(hotkey=SimpleNamespace(
            ss58_address=keypair.ss58_address, sign=lambda data: keypair.sign(data)))
        return APIClient(SimpleNamespace(challenge_api_url="https://cs.example", validator_secret_key="val_x"),
                         wallet, Session(routes))
    return make


SYNC = {
    "challenge": {"challenge_id": "c1", "status": "active", "expires_at": "2099-01-01T00:00:00+00:00",
                  "github_url": "https://x", "winner_baseline_score": 12.5, "winner_reward_hours": 24,
                  "ban_emissions": False, "relaunch_count": 0},
    "batch_windows": {"download_seconds": 480, "evaluation_seconds": 1320},
    "testcases": {"version": "etag-1", "download_new_testcases": False},
    "bans": {"version": "b1", "count": 1, "bans": [{"coldkey": "5Cold", "scope": "permanent", "reason": "r"}]},
    "batch": {"batch_id": "b-1", "status": "exposed", "submissions": [{"submission_id": "s1", "hotkey": "5M"}]},
    "server_time": "2026-09-26T00:00:00+00:00", "next_poll_seconds": 15,
}


# --- signatures --------------------------------------------------------------------------

def test_both_mode_sends_v1_and_v2_and_both_verify(make_client, keypair):
    client = make_client({})
    form = {"overall_score": "5.0", "passed_testbench": "true"}
    url = "https://cs.example/api/v1/challenges/c1/submissions/s1/submit_score"
    params, headers = client._auth("POST", url, form)

    # v1: signature over f"{hotkey}{timestamp}" in the query
    assert keypair.verify(f"{keypair.ss58_address}{params['timestamp']}".encode(), bytes.fromhex(params["signature"]))
    # v2: exactly the challenge server's canonical message
    canonical = "&".join(f"{k}={v}" for k, v in sorted(form.items()))
    message = (f"chipforge-sn108:v2:POST:/api/v1/challenges/c1/submissions/s1/submit_score:"
               f"{keypair.ss58_address}:{headers['X-Timestamp']}:{headers['X-Nonce']}:"
               f"{hashlib.sha256(canonical.encode()).hexdigest()}")
    assert headers["X-Signature-Version"] == "2" and headers["X-Validator-Secret"] == "val_x"
    assert keypair.verify(message.encode(), bytes.fromhex(headers["X-Signature"]))
    assert params["validator_hotkey"] == keypair.ss58_address


def test_v2_only_mode_keeps_signatures_out_of_the_url(make_client):
    params, headers = make_client({}, mode="v2")._auth("GET", "https://cs.example/api/v1/validator/sync")
    assert set(params) == {"validator_hotkey"} and "X-Signature" in headers


def test_every_request_gets_a_fresh_nonce(make_client):
    client = make_client({})
    nonces = {client._auth("GET", "https://cs.example/x")[1]["X-Nonce"] for _ in range(20)}
    assert len(nonces) == 20


# --- /validator/sync ---------------------------------------------------------------------

async def test_sync_answers_all_state_calls_with_one_request(make_client):
    client = make_client({"/api/v1/validator/sync": lambda m, kw: Resp(200, SYNC, {"ETag": '"e1"'})})
    assert (await client.get_active_challenge())["challenge_id"] == "c1"
    info = await client.get_challenge_info("c1")
    assert info["winner_baseline_score"] == 12.5 and info["batch_evaluation_window_seconds"] == 1320
    assert (await client.get_current_batch("c1"))["batch_id"] == "b-1"
    assert (await client.get_banned_coldkeys("c1"))["bans"][0]["coldkey"] == "5Cold"
    assert len(client.session.calls) == 1


async def test_sync_revalidates_with_etag_and_reuses_state_on_304(make_client):
    responses = [Resp(200, SYNC, {"ETag": '"e1"'}), Resp(304)]
    client = make_client({"/api/v1/validator/sync": lambda m, kw: responses.pop(0)})
    await client.get_active_challenge()
    info = await client.get_challenge_info("c1", fresh=True)          # bypass cache -> 304
    assert info["winner_baseline_score"] == 12.5
    assert client.session.calls[1][2]["headers"]["If-None-Match"] == '"e1"'


async def test_old_server_without_sync_uses_individual_endpoints(make_client):
    routes = {
        "/api/v1/validator/sync": lambda m, kw: Resp(404, {"detail": "Not Found"}),
        "/api/v1/challenges/active": lambda m, kw: Resp(200, {"challenge_id": "c1", "winner_reward_hours": 24}),
        "/api/v1/challenges/c1/batch/current": lambda m, kw: Resp(200, {"batch_id": "b9", "submissions": []}),
    }
    client = make_client(routes)
    assert (await client.get_active_challenge())["challenge_id"] == "c1"
    assert (await client.get_current_batch("c1"))["batch_id"] == "b9"
    await client.get_active_challenge()
    sync_calls = [c for c in client.session.calls if c[1] == "/api/v1/validator/sync"]
    assert len(sync_calls) == 1                                          # 404 remembered


async def test_unreachable_server_raises_connection_error(make_client):
    import aiohttp

    def down(m, kw):
        raise aiohttp.ClientConnectionError("refused")
    client = make_client({"/api/v1/validator/sync": down, "/api/v1/challenges/active": down})
    with pytest.raises(ConnectionError):
        await client.get_active_challenge()


# --- test cases --------------------------------------------------------------------------

async def test_testcases_downloaded_only_when_version_changes(make_client):
    state = json.loads(json.dumps(SYNC))
    routes = {
        "/api/v1/validator/sync": lambda m, kw: Resp(200, state, {"ETag": '"e"'}),
        "/api/v1/challenges/c1/test_cases/download": lambda m, kw: Resp(200, b"PK-tc"),
    }
    client = make_client(routes)
    assert (await client.get_challenge_info("c1", fresh=True))["download_new_testcases"] is True
    assert await client.download_test_cases("c1")
    assert "download_new_testcases" not in await client.get_challenge_info("c1", fresh=True)
    state["testcases"]["version"] = "etag-2"                            # admin uploaded new test cases
    assert (await client.get_challenge_info("c1", fresh=True))["download_new_testcases"] is True


async def test_legacy_flag_is_throttled(make_client):
    routes = {
        "/api/v1/validator/sync": lambda m, kw: Resp(404),
        "/api/v1/challenges/c1/info": lambda m, kw: Resp(200, {"winner_baseline_score": 1, "download_new_testcases": True}),
    }
    client = make_client(routes)
    tc = client.get_testcase_files("c1")
    tc.parent.mkdir(parents=True, exist_ok=True)
    tc.write_bytes(b"PK")                                               # just downloaded
    assert "download_new_testcases" not in await client.get_challenge_info("c1")
    old = time.time() - 3600
    import os
    os.utime(tc, (old, old))
    assert (await client.get_challenge_info("c1"))["download_new_testcases"] is True


# --- config / clamps ---------------------------------------------------------------------

def test_secret_and_url_come_from_env(monkeypatch, tmp_path):
    from neurons.validator import get_config

    monkeypatch.setenv("VALIDATOR_SECRET_KEY", "from-env")
    monkeypatch.delenv("CHALLENGE_API_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["validator.py", "--netuid", "108", "--wallet.path", str(tmp_path)])
    config = get_config()
    assert config.validator_secret_key == "from-env"
    assert config.challenge_api_url == "https://api.chipforge.io"


def test_absurd_winner_reward_hours_from_server_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIPFORGE_DATA_DIR", str(tmp_path))
    from validator_utils.emission_manager import EmissionManager

    em = EmissionManager()
    em.update_winner_reward_hours_from_server(48)
    assert em.total_hours_for_winner_reward == 48
    em.update_winner_reward_hours_from_server(10 ** 6)
    assert em.total_hours_for_winner_reward == em.local_winner_reward_hours
