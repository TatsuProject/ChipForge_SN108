"""Validator <-> EDA server and download handling (Phase B)."""
import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from validator_utils.api_client import APIClient, safe_filename


class FakeResponse:
    def __init__(self, status=200, json_data=None, text=""):
        self.status, self._json, self._text = status, json_data, text

    async def json(self):
        return self._json

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    """Records EDA calls; `handler(kwargs)` returns a FakeResponse or raises."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.in_flight = 0
        self.max_in_flight = 0

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        session = self

        class _Ctx:
            async def __aenter__(self_inner):
                session.in_flight += 1
                session.max_in_flight = max(session.max_in_flight, session.in_flight)
                await asyncio.sleep(0.01)
                session.in_flight -= 1
                return session.handler(kwargs)

            async def __aexit__(self_inner, *a):
                return False
        return _Ctx()

    def get(self, url, **kwargs):
        return self.handler({"url": url, **kwargs})


@pytest.fixture()
def client_factory(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIPFORGE_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("USE_DUMMY_EVALUATION", raising=False)

    def make(handler, concurrency=4, with_testcases=True):
        monkeypatch.setenv("EDA_MAX_CONCURRENCY", str(concurrency))
        wallet = MagicMock()
        wallet.hotkey.ss58_address = "5Validator"
        client = APIClient(SimpleNamespace(challenge_api_url="http://cs", validator_secret_key="k"),
                           wallet, FakeSession(handler))
        if with_testcases:
            tc = client.get_testcase_files("c1")
            tc.parent.mkdir(parents=True, exist_ok=True)
            tc.write_bytes(b"PK-testcases")
        return client
    return make


ACCEPTED = {"result": "ACCEPTED", "final_score": {"overall": 42.5, "functional_gate": True, "overall_gate": True,
                                                  "func_score": 100}, "verilator_results": {"success": True,
                                                  "results": {"functionality_score": 100}}}
REJECTED = {"result": "REJECTED", "final_score": {"overall": 0.0, "overall_gate": False, "scored": True},
            "error": {"code": "COMPILE_ERROR", "stage": "simulation", "fault": "miner", "message": "syntax error"}}
SYSTEM_ERROR = {"result": "ERROR", "final_score": {"overall": None, "overall_gate": False, "scored": False},
                "error": {"code": "SERVICE_UNAVAILABLE", "fault": "system", "retryable": True, "message": "openlane down"}}


# --- filenames / downloads ---------------------------------------------------------------

@pytest.mark.parametrize("name, expected", [
    ("c1__5Hk__sid__1__processing.zip", "c1__5Hk__sid__1__processing.zip"),
    ('"quoted.zip"', "quoted.zip"),
    ("../../etc/passwd", "passwd"),
    ("..", "fallback.zip"),
    ("a b;rm.zip", "fallback.zip"),
    (None, "fallback.zip"),
])
def test_safe_filename(name, expected):
    assert safe_filename(name, "fallback.zip") == expected


async def test_download_with_wrong_hash_is_discarded(client_factory):
    client = client_factory(lambda kw: FakeResponse())
    good, bad = b"PK-good", b"PK-tampered"

    async def fake_download(challenge_id, submission_id):
        return {"content": good if submission_id == "s1" else bad, "filename": "../x.zip", "submission_id": submission_id}
    client.download_submission = fake_download
    batch = {"batch_id": "b1", "submissions": [
        {"submission_id": "s1", "file_hash": hashlib.sha256(good).hexdigest()},
        {"submission_id": "s2", "file_hash": hashlib.sha256(b"what the miner signed").hexdigest()},
    ]}
    downloaded = await client.download_batch_submissions("c1", batch)
    assert downloaded == {"s1": good}
    assert (client.submissions_dir / "b1" / "x.zip").exists()        # saved under the basename only


# --- EDA responses -----------------------------------------------------------------------

async def test_accepted_result_is_scored(client_factory):
    client = client_factory(lambda kw: FakeResponse(json_data=ACCEPTED))
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}))["s1"]
    assert ev["overall_score"] == 42.5 and ev["functional_gate"] and ev["overall_gate"]


async def test_system_error_is_a_retryable_failure_not_a_miner_zero(client_factory):
    client = client_factory(lambda kw: FakeResponse(json_data=SYSTEM_ERROR))
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}))["s1"]
    # the challenge server treats score 0 + not passed + "FAILED" in notes as a retryable failure
    assert ev["overall_score"] == 0.0 and not ev["passed_testbench"] and "FAILED" in ev["evaluation_notes"].upper()
    assert "eda_system_error" in ev["evaluation_details"] and "SERVICE_UNAVAILABLE" in ev["evaluation_details"]


async def test_miner_rejection_is_a_real_zero_with_reason(client_factory):
    client = client_factory(lambda kw: FakeResponse(json_data=REJECTED))
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}))["s1"]
    assert ev["overall_score"] == 0.0 and not ev["overall_gate"]
    assert "FAILED" not in ev["evaluation_notes"].upper()          # a real result, not a retry
    assert "COMPILE_ERROR" in ev["evaluation_notes"] and "syntax error" in ev["evaluation_details"]


async def test_missing_testcases_evaluates_nothing(client_factory):
    client = client_factory(lambda kw: FakeResponse(json_data=ACCEPTED), with_testcases=False)
    assert await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}) == {}
    assert client.session.calls == []


async def test_concurrency_is_capped(client_factory):
    client = client_factory(lambda kw: FakeResponse(json_data=ACCEPTED), concurrency=2)
    await client.evaluate_submissions_with_eda_server("c1", {f"s{i}": b"PK" for i in range(8)})
    assert len(client.session.calls) == 8 and client.session.max_in_flight == 2


async def test_timeout_is_the_time_left_before_the_server_deadline(client_factory):
    client = client_factory(lambda kw: FakeResponse(json_data=ACCEPTED))
    deadline = datetime.now(timezone.utc) + timedelta(seconds=600)
    await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}, deadline=deadline)
    budget = client.session.calls[0][1]["timeout"].total
    assert 600 - client.eda_deadline_buffer - 5 < budget <= 600 - client.eda_deadline_buffer


async def test_no_time_left_marks_timeout_without_calling_eda(client_factory):
    client = client_factory(lambda kw: FakeResponse(json_data=ACCEPTED))
    deadline = datetime.now(timezone.utc) + timedelta(seconds=30)
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}, deadline=deadline))["s1"]
    assert ev["timeout_occurred"] is True and client.session.calls == []


async def test_eda_preflight(client_factory):
    assert await client_factory(lambda kw: FakeResponse(status=404)).eda_server_ready()   # older gateway
    assert await client_factory(lambda kw: FakeResponse(status=200)).eda_server_ready()

    def refuse(kw):
        raise OSError("connection refused")
    assert not await client_factory(refuse).eda_server_ready()
