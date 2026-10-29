"""Validator <-> EDA server and download handling (Phase B)."""
import asyncio
import hashlib
import json
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


def _commit(content, **over):
    obj = {"schema": "cf-chain-commit/1", "subtensor_genesis_hash": "ab" * 32, "netuid": 108, "hotkey": "5Miner",
           "block_number": 1, "block_hash": "cd" * 32, "block_timestamp_ms": 1793000000000,
           "package_sha256": hashlib.sha256(content).hexdigest()}
    obj.update(over)
    return json.dumps(obj, separators=(",", ":"))


@pytest.mark.parametrize("case", ["ok", "header_only", "none", "wrong_sha", "listing_vs_header", "bad_json"])
async def test_chain_commit_is_kept_verbatim_or_the_design_discarded(client_factory, case):
    client = client_factory(lambda kw: FakeResponse())
    content = b"PK-design"
    good = _commit(content)
    listed, header = {
        "ok": (good, good), "header_only": (None, good), "none": (None, None),
        "wrong_sha": (_commit(content, package_sha256="00" * 32),) * 2,
        "listing_vs_header": (good, _commit(content, block_number=2)), "bad_json": ("{", None),
    }[case]

    async def fake_download(challenge_id, submission_id):
        return {"content": content, "filename": "d.zip", "submission_id": submission_id, "chain_commit": header}
    client.download_submission = fake_download
    batch = {"batch_id": "b1", "submissions": [
        {"submission_id": "s1", "file_hash": hashlib.sha256(content).hexdigest(), "chain_commit": listed}]}
    downloaded = await client.download_batch_submissions("c1", batch)
    if case in ("ok", "header_only"):
        assert downloaded == {"s1": content} and client.chain_commits["s1"] == good      # text unchanged
    elif case == "none":                                                                    # older server
        assert downloaded == {"s1": content} and "s1" not in client.chain_commits
    else:
        assert downloaded == {} and "s1" not in client.chain_commits


async def test_v2_fields_are_sent_with_the_score(client_factory):
    client = client_factory(lambda kw: FakeResponse())
    sent = {}

    async def fake_signed(method, url, form=None, **kw):
        sent.update(form)
        return 200, "{}", {}
    client._signed = fake_signed
    ev = {"overall_score": 4.06, "functionality_score": 0, "area_score": 0, "delay_score": 0, "power_score": 0,
          "passed_testbench": True, "eval_status": "PASSED", "projection_hash": "7a" * 32}
    assert await client.submit_evaluation("c1", "s1", ev)
    assert sent["eval_status"] == "PASSED" and sent["projection_hash"] == "7a" * 32
    ev.pop("eval_status"), ev.pop("projection_hash")
    sent.clear()
    assert await client.submit_evaluation("c1", "s1", ev)
    assert "eval_status" not in sent and "projection_hash" not in sent


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


# --- the batch deadline travels to the EDA server (time_limit_s / fair_limit_s) ---------------

def _form_fields(call):
    """The plain (non-file) form fields of a recorded EDA request."""
    out = {}
    for type_options, _headers, value in call[1]["data"]._fields:
        if "filename" not in type_options:
            out[type_options["name"]] = value
    return out


async def test_eda_gets_the_time_left_and_the_full_window(client_factory):
    client = client_factory(lambda kw: FakeResponse(json_data=ACCEPTED))
    now = datetime.now(timezone.utc)
    deadline, window_start = now + timedelta(seconds=3600), now - timedelta(seconds=60)
    await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}, deadline=deadline, window_start=window_start)
    fields = _form_fields(client.session.calls[0])
    budget = client.session.calls[0][1]["timeout"].total
    # the EDA server gets the budget minus the margin for its answer; the validator waits the whole budget
    assert abs(float(fields["time_limit_s"]) - (budget - client.eda_answer_margin)) <= 1
    want_fair = 3660 - client.eda_deadline_buffer - client.eda_answer_margin
    assert abs(float(fields["fair_limit_s"]) - want_fair) <= 1


async def test_without_a_window_start_no_fair_limit_is_sent(client_factory):
    client = client_factory(lambda kw: FakeResponse(json_data=ACCEPTED))
    await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"},
                                                      deadline=datetime.now(timezone.utc) + timedelta(seconds=900))
    fields = _form_fields(client.session.calls[0])
    assert "time_limit_s" in fields and "fair_limit_s" not in fields


EDA_MINER_TIMEOUT = {"success": True, "result": "REJECTED", "functionality_score": 0.0,
                     "error": {"code": "EDA_TIMEOUT_BUILD", "category": "timeout", "fault": "miner", "retryable": False,
                               "stage": "build", "where": "eda_server/verilator",
                               "message": "Timeout in the EDA server (verilator) during building the simulator",
                               "timing": {"limit_s": 3500.0, "used_s": 3500.2, "queued_s": 0.0,
                                          "limited_by": "batch_deadline"}},
                     "final_score": {"overall": 0.0, "overall_gate": False, "functional_gate": False}}
EDA_SYSTEM_TIMEOUT = {"success": False, "result": "ERROR",
                      "error": {"code": "EDA_TIMEOUT_SIMULATION", "category": "timeout", "fault": "system",
                                "retryable": True, "stage": "simulation", "where": "eda_server/verilator",
                                "message": "Timeout in the EDA server (verilator) during the model simulations",
                                "timing": {"limit_s": 900.0, "queued_s": 2600.0}},
                      "final_score": {"overall": None, "overall_gate": False, "scored": False}}


async def test_eda_timeout_with_the_full_window_is_the_miners_zero_with_where_and_timing(client_factory):
    client = client_factory(lambda kw: FakeResponse(json_data=EDA_MINER_TIMEOUT))
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}))["s1"]
    assert ev["overall_score"] == 0.0 and not ev["timeout_occurred"] and "FAILED" not in ev["evaluation_notes"].upper()
    assert "EDA_TIMEOUT_BUILD (build)" in ev["evaluation_notes"]
    err = json.loads(ev["evaluation_details"])["error"]
    assert err["code"] == "EDA_TIMEOUT_BUILD" and err["where"] == "eda_server/verilator"
    assert err["timing"]["limit_s"] == 3500.0


async def test_eda_timeout_cut_short_is_a_retryable_failure_naming_where(client_factory):
    client = client_factory(lambda kw: FakeResponse(json_data=EDA_SYSTEM_TIMEOUT))
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}))["s1"]
    assert "FAILED" in ev["evaluation_notes"].upper() and "EDA_TIMEOUT_SIMULATION" in ev["evaluation_notes"]
    d = json.loads(ev["evaluation_details"])
    assert d["status"] == "eda_system_error" and d["where"] == "eda_server/verilator" and d["timing"]["queued_s"] == 2600.0


async def test_eda_not_answering_in_time_is_a_validator_timeout(client_factory):
    def hang(kw):
        raise asyncio.TimeoutError()
    client = client_factory(hang)
    ev = (await client.evaluate_submissions_with_eda_server(
        "c1", {"s1": b"PK"}, deadline=datetime.now(timezone.utc) + timedelta(seconds=900)))["s1"]
    d = json.loads(ev["evaluation_details"])
    assert ev["timeout_occurred"] is True and d["code"] == "VALIDATOR_EDA_NO_RESPONSE" and d["where"] == "validator"
    assert "VALIDATOR_EDA_NO_RESPONSE" in ev["evaluation_notes"] and "FAILED" in ev["evaluation_notes"].upper()


async def test_no_time_to_start_names_the_validator(client_factory):
    client = client_factory(lambda kw: FakeResponse(json_data=ACCEPTED))
    deadline = datetime.now(timezone.utc) + timedelta(seconds=client.eda_deadline_buffer + client.eda_answer_margin + 60)
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}, deadline=deadline))["s1"]
    d = json.loads(ev["evaluation_details"])
    assert client.session.calls == [] and ev["timeout_occurred"] is True
    assert d["code"] == "VALIDATOR_NO_TIME_TO_START" and d["retryable"] is True


def test_batch_timeout_evaluation_names_the_validator(client_factory):
    client = client_factory(lambda kw: FakeResponse(json_data=ACCEPTED))
    ev = client.build_timeout_evaluation("s1", 1234.5)
    d = json.loads(ev["evaluation_details"])
    assert ev["timeout_occurred"] is True and d["code"] == "VALIDATOR_BATCH_TIMEOUT" and "1234s" in d["message"]


@pytest.mark.parametrize("batch_id", ["/tmp/evil", "../../etc", "..", "a/b", "x" * 300, ""])
async def test_server_batch_id_never_escapes_the_data_dir(client_factory, batch_id, tmp_path):
    client = client_factory(lambda kw: FakeResponse())
    content = b"PK-design"

    async def fake_download(challenge_id, submission_id):
        return {"content": content, "filename": "../../evil.sh", "submission_id": submission_id}
    client.download_submission = fake_download
    batch = {"batch_id": batch_id, "submissions": [
        {"submission_id": "s1", "file_hash": hashlib.sha256(content).hexdigest()}]}
    assert await client.download_batch_submissions("c1", batch) == {"s1": content}
    written = [p for p in tmp_path.rglob("*") if p.is_file() and p.read_bytes() == content]
    assert written and all(client.submissions_dir in p.parents for p in written)


def test_challenge_id_never_escapes_the_testcases_dir(client_factory):
    client = client_factory(lambda kw: FakeResponse(), with_testcases=False)
    for cid in ("../../x", "/etc/passwd", "a/b"):
        assert client.get_testcase_files(cid).parent == client.base_dir / "testcases"
