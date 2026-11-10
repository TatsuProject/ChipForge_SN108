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
    def __init__(self, status=200, json_data=None, text="", headers=None):
        self.status, self._json, self._text = status, json_data, text
        self.headers = headers or {}

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
        kwargs = {"url": url, **kwargs}
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
    monkeypatch.setenv("EDA_API", "v1")          # these tests drive the npu-v1 /evaluate route

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


# --- npu-v2 evaluator --------------------------------------------------------------------------------

V2_SHA = "ab" * 32


def _v2_result(status="PASSED", s=4.06, projection="7a" * 32, problems=()):
    return {"schema": "cf-response/1", "status": status, "tier_reached": "T2" if status == "PASSED" else "T0",
            "problems": list(problems), "projection_sha256": projection, "evaluator_sha256": V2_SHA,
            "public_report": {"schema": "cf-public-report/1", "verdict": {"status": status, "score_S": s},
                              "score": {"S": {"value": s}}}}


class V2Server:
    """Fake v2 EDA server: health, evaluate (202), status (202 `polls` times, then the result). EDA server 1.0.2+
    (jobs=True): GET /v2/jobs lists `unfinished` and POST /v2/cancel/<id> is recorded; older ones answer 404."""
    def __init__(self, result, polls=1, health=None, evaluate_status=202, running=(), jobs=True, phase="running",
                 unfinished=()):
        self.result, self.polls, self.forms = result, polls, []
        self.health = health or {"ready": True, "active_evaluator": V2_SHA}
        self.evaluate_status = evaluate_status
        self.known = set(running)                           # request IDs it holds (e.g. started before an update)
        self.jobs, self.phase, self.unfinished = jobs, phase, [dict(j) for j in unfinished]
        self.cancelled, self.headers = [], []

    def __call__(self, kw):
        url = kw.get("url", "")
        if url.endswith("/v2/health"):
            return FakeResponse(json_data=self.health)
        if url.endswith("/v2/jobs"):
            return FakeResponse(json_data={"jobs": self.unfinished}) if self.jobs else FakeResponse(status=404, json_data={})
        if url.endswith("/v2/cancel-all"):
            if not self.jobs:
                return FakeResponse(status=404, json_data={})
            self.cancelled.append("ALL")
            return FakeResponse(json_data={"cancelled": [j.get("request_id") for j in self.unfinished]})
        if "/v2/cancel/" in url:
            if not self.jobs:
                return FakeResponse(status=404, json_data={})
            self.cancelled.append(url.rsplit("/", 1)[-1])
            return FakeResponse(json_data={"cancelled": True})
        if "data" in kw:                                    # POST /v2/evaluate
            fields = {f[0]["name"]: f[2] for f in kw["data"]._fields}
            self.forms.append(fields)
            self.headers.append(kw.get("headers") or {})
            self.known.add(fields["request_id"])
            return FakeResponse(status=self.evaluate_status, json_data={"request_id": fields["request_id"], "phase": "running"}
                                if self.evaluate_status == 202 else {"error": {"code": "INVALID_CHAIN_COMMIT"}})
        if "/v2/status/" in url and url.rsplit("/", 1)[-1] not in self.known:
            return FakeResponse(status=404, json_data={"error": {"code": "NOT_FOUND"}})
        if self.polls > 0:
            self.polls -= 1
            return FakeResponse(status=202, json_data={"phase": self.phase}, headers={"X-CF-Phase": self.phase})
        # the loader names the evaluator it ran
        return FakeResponse(json_data=self.result, headers={"X-CF-Evaluator-Sha256": V2_SHA})


@pytest.fixture()
def v2_client(client_factory, monkeypatch):
    def make(server):
        monkeypatch.setenv("EDA_API", "v2")
        monkeypatch.setenv("EDA_POLL_SECONDS", "1")
        client = client_factory(server, with_testcases=False)
        client.eda_poll_seconds = 0.01
        return client
    return make


async def test_v2_passed_sends_chain_commit_and_maps_s(v2_client):
    server = V2Server(_v2_result())
    client = v2_client(server)
    content = b"PK-design"
    client.chain_commits["s1"] = _commit(content)
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": content}))["s1"]
    assert ev["overall_score"] == 4.06 and ev["functional_gate"] and ev["overall_gate"] and ev["passed_testbench"]
    assert ev["eval_status"] == "PASSED" and ev["projection_hash"] == "7a" * 32
    import gzip
    assert json.loads(gzip.decompress(ev["public_report_gz"]))["score"]["S"]["value"] == 4.06
    form = server.forms[0]
    assert form["mode"] == "T2" and form["evaluator_sha256"] == V2_SHA and form["chain_commit"] == _commit(content)
    assert form["submitter"] == "5Miner"


async def test_v2_rejected_is_a_real_zero(v2_client):
    client = v2_client(V2Server(_v2_result("REJECTED", None, problems=[{"code": "NPU_PORT_MISMATCH", "fault": "miner"}])))
    client.chain_commits["s1"] = _commit(b"PK")
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}))["s1"]
    assert ev["overall_score"] == 0 and not ev["functional_gate"] and ev["eval_status"] == "REJECTED"
    assert "NPU_PORT_MISMATCH" in ev["evaluation_notes"] and "FAILED" not in ev["evaluation_notes"].upper()


@pytest.mark.parametrize("status", ["INCOMPLETE", "ERROR"])
async def test_v2_system_fault_is_never_the_miners_score(v2_client, status):
    client = v2_client(V2Server(_v2_result(status, None, problems=[{"code": "EVALUATION_RANDOMNESS_UNAVAILABLE"}])))
    client.chain_commits["s1"] = _commit(b"PK")
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}))["s1"]
    # challenge server: score 0 + not passed + FAILED in notes = a failed (retryable) evaluation; status = system
    assert ev["overall_score"] == 0 and "FAILED" in ev["evaluation_notes"].upper() and ev["eval_status"] == status


async def test_v2_without_chain_commit_is_not_sent(v2_client):
    server = V2Server(_v2_result())
    client = v2_client(server)
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}))["s1"]
    assert server.forms == [] and "VALIDATOR_NO_CHAIN_COMMIT" in ev["evaluation_details"]


async def test_v2_refused_request_is_a_system_fault(v2_client):
    client = v2_client(V2Server(_v2_result(), evaluate_status=400))
    client.chain_commits["s1"] = _commit(b"PK")
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}))["s1"]
    assert "INVALID_CHAIN_COMMIT" in ev["evaluation_details"] and "FAILED" in ev["evaluation_notes"].upper()


async def test_v2_not_ready_server(v2_client):
    client = v2_client(V2Server(_v2_result(), health={"ready": False}))
    assert not await client.eda_server_ready()
    assert not client.needs_testcases


async def test_v2_report_is_sent_with_its_signed_sha(client_factory, monkeypatch):
    monkeypatch.setenv("EDA_API", "v2")
    client = client_factory(lambda kw: FakeResponse())
    sent = {}

    async def fake_signed(method, url, form=None, files=None, **kw):
        sent.update(form=form, files=files)
        return 200, "{}", {}
    client._signed = fake_signed
    gz = b"\x1f\x8bfake"
    ev = {"overall_score": 4.06, "functionality_score": 0, "area_score": 0, "delay_score": 0, "power_score": 0,
          "passed_testbench": True, "public_report_gz": gz}
    assert await client.submit_evaluation("c1", "s1", ev)
    assert sent["files"]["public_report"][1] == gz
    assert sent["form"]["public_report_sha256"] == hashlib.sha256(gz).hexdigest()


async def test_each_assignment_gets_its_own_eda_request_id(v2_client):
    """The EDA server keeps every request ID: a design evaluated again (a later assignment, a reset, a new
    evaluator) must not reuse the old ID (REQUEST_ID_CONFLICT, or the old stored result); the same assignment
    keeps its ID (a restarted validator finds its running evaluation)."""
    import re
    server = V2Server(_v2_result())
    client = v2_client(server)
    client.chain_commits["s1"] = _commit(b"PK")
    for batch in ("split-aaaa1111bbbb2222cccc3333dddd4444", "split-eeee5555ffff6666aaaa7777bbbb8888"):
        await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}, batch_id=batch)
    first, second = (f["request_id"] for f in server.forms)
    assert first != second and first.startswith("s1-") and "aaaa1111" in first and "eeee5555" in second
    assert client.eda_request_id("s1", "split-aaaa1111bbbb2222cccc3333dddd4444", V2_SHA) == first   # stable
    assert client.eda_request_id("s1", "split-aaaa1111bbbb2222cccc3333dddd4444", "f" * 64) != first  # evaluator
    rid = client.eda_request_id("d" * 32, "b" * 40, "a" * 64)
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", rid)          # what the EDA server accepts
    assert client.eda_request_id("s1", None, V2_SHA).startswith("s1-")


async def test_updated_mid_evaluation_waits_for_the_running_one(v2_client):
    """Restarted on 1.0.4+ while the EDA server still runs the design under the old request ID: no second
    evaluation is started; the running one's result is reported."""
    legacy = f"s1-{v2_client(V2Server(_v2_result())).validator_hotkey[:10]}"
    server = V2Server(_v2_result(), running=[legacy])
    client = v2_client(server)
    client.chain_commits["s1"] = _commit(b"PK")
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}, batch_id="split-abc"))["s1"]
    assert server.forms == [] and ev["overall_score"] == 4.06


# --- an evaluation nobody waits for must not hold the EDA server (EDA server 1.0.2+) -------------------------

def _short(client, seconds):
    """A deadline `seconds` away with no margins, so a test gives up quickly."""
    client.eda_deadline_buffer = client.eda_answer_margin = client.eda_min_limit = 0
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


async def test_the_deadline_goes_with_the_evaluation(v2_client):
    server = V2Server(_v2_result())
    client = v2_client(server)
    client.chain_commits["s1"] = _commit(b"PK")
    deadline = _short(client, 3600)
    await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}, deadline=deadline, batch_id="split-abc")
    h = server.headers[0]
    assert h["X-CF-Request-Id"] == server.forms[0]["request_id"]
    assert abs(float(h["X-CF-Deadline"]) - deadline.timestamp()) < 5


async def test_out_of_time_after_running_is_a_timeout_and_the_run_is_cancelled(v2_client):
    server = V2Server(_v2_result(), polls=10 ** 6, phase="running")
    client = v2_client(server)
    client.chain_commits["s1"] = _commit(b"PK")
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}, deadline=_short(client, 0.4),
                                                            batch_id="split-abc"))["s1"]
    assert ev["timeout_occurred"] is True
    details = json.loads(ev["evaluation_details"])
    assert details["code"] == "VALIDATOR_EDA_NO_RESULT" and details["timing"]["waited_s"] < 1
    await asyncio.gather(*client._bg_cancels)
    assert server.cancelled == [server.forms[0]["request_id"]]          # the EDA server is free for the next design


async def test_a_design_that_never_got_the_machine_is_not_blamed(v2_client):
    server = V2Server(_v2_result(), polls=10 ** 6, phase="queued")
    client = v2_client(server)
    client.chain_commits["s1"] = _commit(b"PK")
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}, deadline=_short(client, 0.4),
                                                            batch_id="split-abc"))["s1"]
    details = json.loads(ev["evaluation_details"])
    assert not ev.get("timeout_occurred") and details["code"] == "VALIDATOR_EDA_BUSY"
    assert "FAILED" in ev["evaluation_notes"].upper()                   # the server: a validator fault, requeued
    await asyncio.gather(*client._bg_cancels)
    assert server.cancelled == [server.forms[0]["request_id"]]


async def test_a_design_started_late_is_not_blamed(v2_client):
    import time as _t
    server = V2Server(_v2_result(), polls=10 ** 6, phase="running")
    client = v2_client(server)
    client.chain_commits["s1"] = _commit(b"PK")
    rid = client.eda_request_id("s1", "split-abc", V2_SHA)
    # the EDA server's record (it survives a validator restart): sent 2 h ago, started 30 min ago
    server.unfinished = [{"request_id": rid, "phase": "running", "sent_unix": _t.time() - 7200, "started_unix": _t.time() - 1800}]
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}, deadline=_short(client, 0.4),
                                                            batch_id="split-abc"))["s1"]
    details = json.loads(ev["evaluation_details"])
    assert details["code"] == "VALIDATOR_EDA_BUSY" and details["timing"]["waited_s"] > 5000


async def test_abandoned_evaluations_are_cancelled_before_a_new_design(v2_client):
    me = v2_client(V2Server(_v2_result())).validator_hotkey[:10]
    server = V2Server(_v2_result(), unfinished=[
        {"request_id": f"old1-{me}-aaaa-abababab"}, {"request_id": f"old2-{me}-bbbb-abababab"},
        {"request_id": "other-5SomeoneEl-cccc-abababab"}])                  # not this validator's: left to the EDA server
    client = v2_client(server)
    client.chain_commits["s1"] = _commit(b"PK")
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}, batch_id="split-abc"))["s1"]
    assert ev["overall_score"] == 4.06
    await asyncio.gather(*client._bg_cancels)
    assert sorted(server.cancelled) == [f"old1-{me}-aaaa-abababab", f"old2-{me}-bbbb-abababab"]


async def test_a_batch_keeps_all_its_designs(v2_client):
    server = V2Server(_v2_result())
    client = v2_client(server)
    for s in ("s1", "s2"):
        client.chain_commits[s] = _commit(s.encode())
    rid2 = client.eda_request_id("s2", "batch-1", V2_SHA)
    server.unfinished = [{"request_id": rid2}]                          # sent earlier in this same batch
    await client.evaluate_submissions_with_eda_server("c1", {"s1": b"s1", "s2": b"s2"}, batch_id="batch-1")
    assert server.cancelled == []


async def test_an_older_eda_server_still_works(v2_client):
    server = V2Server(_v2_result(), polls=10 ** 6, jobs=False)
    client = v2_client(server)
    client.chain_commits["s1"] = _commit(b"PK")
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}, deadline=_short(client, 0.4),
                                                            batch_id="split-abc"))["s1"]
    assert ev["timeout_occurred"] is True and server.cancelled == []   # it cannot cancel: the old behaviour


async def test_the_eda_server_enforced_the_deadline_first(v2_client):
    cancelled = _v2_result("ERROR", None, problems=[{"code": "EVALUATION_CANCELLED", "fault": "system"}])
    server = V2Server(cancelled, polls=40, phase="running")
    client = v2_client(server)
    client.chain_commits["s1"] = _commit(b"PK")
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}, deadline=_short(client, 0.3),
                                                            batch_id="split-abc"))["s1"]
    assert ev["timeout_occurred"] is True                              # it ran its time: a timeout, as if we gave up


async def test_a_slow_cancel_does_not_delay_the_report(v2_client):
    """Pen test: stopping a running stage can take minutes; the result must not wait for it."""
    import time as _t
    server = V2Server(_v2_result(), polls=10 ** 6, phase="running")
    client = v2_client(server)
    client.chain_commits["s1"] = _commit(b"PK")
    started = []

    async def slow_cancel(rid, reason):
        started.append(rid)
        await asyncio.sleep(3)
        return True
    client._v2_cancel = slow_cancel
    t0 = _t.monotonic()
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}, deadline=_short(client, 0.5),
                                                            batch_id="split-abc"))["s1"]
    assert _t.monotonic() - t0 < 1.5 and ev["timeout_occurred"] is True
    assert started == [server.forms[0]["request_id"]]                  # the cancel was started, not awaited
    for t in list(client._bg_cancels):
        t.cancel()


async def test_the_admin_token_goes_with_jobs_and_cancel(v2_client, monkeypatch):
    monkeypatch.setenv("EDA_ADMIN_TOKEN", "s3cret")
    seen = []
    server = V2Server(_v2_result())
    client = v2_client(lambda kw: (seen.append(kw), server(kw))[1])
    assert client._eda_admin_headers() == {"Authorization": "Bearer s3cret"}
    await client._v2_jobs()
    await client._v2_cancel("r1", "test")
    assert all(k.get("headers", {}).get("Authorization") == "Bearer s3cret" for k in seen
               if k.get("url", "").endswith(("/v2/jobs", "/v2/cancel/r1")))



# --- kill switch -------------------------------------------------------------------------------------------

async def test_a_kill_cancels_everything_once(v2_client):
    server = V2Server(_v2_result())
    client = v2_client(server)
    assert await client.on_kill({"epoch": 1, "active": True, "reason": "evaluator bug"})
    assert server.cancelled == ["ALL"]
    assert not await client.on_kill({"epoch": 1, "active": True})              # the same kill: once
    assert server.cancelled == ["ALL"]
    # remembered across a restart (a new client on the same data directory)
    again = v2_client(server)
    assert again.kill_epoch == 1 and not await again.on_kill({"epoch": 1, "active": True})


async def test_a_kill_that_already_ended_is_only_remembered(v2_client):
    server = V2Server(_v2_result())
    client = v2_client(server)
    assert not await client.on_kill({"epoch": 4, "active": False})
    assert server.cancelled == [] and client.kill_epoch == 4


async def test_a_kill_stops_the_evaluation_in_progress(v2_client):
    server = V2Server(_v2_result(), polls=10 ** 6, phase="running")
    client = v2_client(server)
    client.chain_commits["s1"] = _commit(b"PK")

    async def kill_soon():
        await asyncio.sleep(0.2)
        await client.on_kill({"epoch": 1, "active": True})
    killer = asyncio.create_task(kill_soon())
    ev = (await client.evaluate_submissions_with_eda_server("c1", {"s1": b"PK"}, deadline=_short(client, 60),
                                                            batch_id="split-abc"))["s1"]
    await killer
    assert json.loads(ev["evaluation_details"])["code"] == "VALIDATOR_KILLED" and not ev.get("timeout_occurred")
    assert "ALL" in server.cancelled


async def test_the_kill_watch_reads_the_sync(v2_client):
    server = V2Server(_v2_result())
    client = v2_client(server)
    states = iter([{"kill": {"epoch": 0}}, {"kill": {"epoch": 2, "active": True, "reason": "x"}}])

    async def fake_sync(fresh=False):
        return next(states, {"kill": {"epoch": 2, "active": True}})
    client._sync = fake_sync
    stop = asyncio.Event()
    task = asyncio.create_task(client.kill_watch(stop, every_seconds=0.05))
    await asyncio.sleep(0.3)
    stop.set(); await task
    assert server.cancelled == ["ALL"] and client.kill_epoch == 2


async def test_killed_designs_are_not_reported(tmp_path, monkeypatch):
    from validator_utils.batch_processor import BatchProcessor
    monkeypatch.setenv("CHIPFORGE_DATA_DIR", str(tmp_path))
    submitted = []
    killed = {"overall_score": 0.0, "evaluation_details": json.dumps({"code": "VALIDATOR_KILLED"})}

    class Api:
        already_evaluated = set()
        async def download_batch_submissions(self, cid, batch): return {"s1": b"PK"}
        async def evaluate_submissions_with_eda_server(self, *a, **k): return {"s1": killed}
        async def submit_all_evaluations(self, cid, evs):
            submitted.append(evs); return {s: True for s in evs}
    state = MagicMock()
    ok = await BatchProcessor(Api(), state).process_batch("c1", {"batch_id": "split-x", "submissions": [{"submission_id": "s1"}]})
    assert ok is False and submitted == [] and state.mark_batch_evaluated.called


def test_the_validator_keeps_at_least_five_minutes(client_factory, monkeypatch):
    for env, expected in (("30", 300), ("", 300), ("600", 600)):
        monkeypatch.setenv("EDA_DEADLINE_BUFFER_SECONDS", env)
        assert client_factory(lambda kw: FakeResponse()).eda_deadline_buffer == expected
