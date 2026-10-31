"""Evaluator distribution and validator status: the validator installs the evaluator the challenge server
names (download, sha256 check, POST /v2/evaluator, follow the phases), reports what it does, claims work
only with that evaluator ready, evaluates with the batch's evaluator and sends its sha256 with the result."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from validator_utils.batch_processor import BatchProcessor, result_line
from validator_utils.evaluator_manager import EvaluatorManager
from validator_utils.status_reporter import StatusReporter
from test_server_client import keypair, make_client  # noqa: F401 (fixtures)

SHA = "a" * 64
TARGET = {"version": 2, "sha256": SHA, "label": "fixed gate B", "size_bytes": 10,
          "zip_url": "/api/v1/challenges/c1/evaluator/" + SHA + "/zip",
          "sshsig_url": "/api/v1/challenges/c1/evaluator/" + SHA + "/sshsig"}


class FakeEDA:
    """The EDA server as the validator sees it: health, install state per sha, install."""

    def __init__(self, phases=("verify", "fetch_assets", "post_install", "preflight", "ready"), up=True,
                 active=None, refuse=None):
        self.up, self.active, self.refuse = up, active, refuse
        self.phases = list(phases)
        self.installed = {}
        self.posts = []

    async def eda_health(self):
        if not self.up:
            return None
        return {"ready": self.active is not None, "active_evaluator": self.active, "mock": False}

    async def eda_evaluator_state(self, sha):
        if sha not in self.installed:
            return {}
        if self.installed[sha]:
            phase = self.installed[sha].pop(0)
            if phase == "ready":
                self.active = sha
            self.last = phase
            return {"phase": phase, "error": "EVALUATOR_PREFLIGHT_FAILED" if phase == "failed" else None}
        return {"phase": self.last}

    async def eda_install_evaluator(self, path, sig, sha):
        self.posts.append((path.read_bytes(), sig, sha))
        if self.refuse:
            return False, self.refuse
        self.installed[sha] = list(self.phases)
        return True, None


def manager(tmp_path, monkeypatch, eda, content=b"zipbytes"):
    monkeypatch.setenv("CHIPFORGE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EVALUATOR_POLL_SECONDS", "0")
    api = SimpleNamespace(eda_health=eda.eda_health, eda_evaluator_state=eda.eda_evaluator_state,
                          eda_install_evaluator=eda.eda_install_evaluator,
                          download_evaluator_signature=AsyncMock(return_value=b"-----BEGIN SSH SIGNATURE-----"))

    async def download(url, dest, sha, size=None):
        if hashlib.sha256(content).hexdigest() != sha:
            return False
        dest.write_bytes(content)
        return True
    api.download_evaluator = AsyncMock(side_effect=download)
    status = StatusReporter(SimpleNamespace())
    return EvaluatorManager(api, status), api, status


async def settle(m):
    if m._task is not None:
        await m._task


async def test_installs_the_evaluator_the_server_names_and_reports_each_step(tmp_path, monkeypatch):
    content = b"evaluator zip"
    target = dict(TARGET, sha256=hashlib.sha256(content).hexdigest())
    eda = FakeEDA()
    m, api, status = manager(tmp_path, monkeypatch, eda, content)
    seen = []
    original = status.set
    status.set = lambda *a, **k: (seen.append(a[0]), original(*a, **k))

    assert await m.ready_sha(target) == (True, None)            # not there: install starts, no work yet
    await settle(m)
    assert eda.posts[0][0] == content and eda.posts[0][2] == target["sha256"]
    assert await m.ready_sha(target) == (True, target["sha256"])
    steps = [x for i, x in enumerate(seen) if i == 0 or seen[i - 1] != x]
    assert steps[:2] == ["downloading_evaluator", "installing_evaluator"]
    assert status.evaluator_ready and status.target_sha256 == target["sha256"]


async def test_a_wrong_download_is_never_installed(tmp_path, monkeypatch):
    eda = FakeEDA()
    m, api, status = manager(tmp_path, monkeypatch, eda, content=b"not what the server announced")
    await m.ready_sha(TARGET)
    await settle(m)
    assert eda.posts == [] and status.state == "install_failed" and status.detail == "DOWNLOAD_FAILED"


async def test_failed_install_is_reported_and_retried_later_not_in_a_loop(tmp_path, monkeypatch):
    content = b"zz"
    target = dict(TARGET, sha256=hashlib.sha256(content).hexdigest())
    eda = FakeEDA(phases=("verify", "preflight", "failed"))
    m, api, status = manager(tmp_path, monkeypatch, eda, content)
    await m.ready_sha(target)
    await settle(m)
    assert status.state == "install_failed" and status.detail == "EVALUATOR_PREFLIGHT_FAILED"
    eda.installed.clear()
    assert await m.ready_sha(target) == (True, None)
    assert len(eda.posts) == 1                                   # within EVALUATOR_RETRY_MINUTES: no new attempt
    m.retry_seconds = 0
    await m.ready_sha(target)
    await settle(m)
    assert len(eda.posts) == 2


async def test_refused_by_the_eda_server_names_its_code(tmp_path, monkeypatch):
    content = b"zz"
    eda = FakeEDA(refuse="EVALUATOR_SIGNATURE_INVALID")
    m, _, status = manager(tmp_path, monkeypatch, eda, content)
    await m.ready_sha(dict(TARGET, sha256=hashlib.sha256(content).hexdigest()))
    await settle(m)
    assert status.detail == "EVALUATOR_SIGNATURE_INVALID"


async def test_already_installed_evaluator_is_used_without_downloading(tmp_path, monkeypatch):
    eda = FakeEDA(active=SHA)
    eda.installed[SHA] = ["ready"]
    m, api, _ = manager(tmp_path, monkeypatch, eda)
    assert await m.ready_sha(TARGET) == (True, SHA)
    api.download_evaluator.assert_not_called()


async def test_eda_offline_and_no_server_evaluator(tmp_path, monkeypatch):
    m, _, status = manager(tmp_path, monkeypatch, FakeEDA(up=False))
    assert await m.ready_sha(TARGET) == (False, None)
    assert not status.eda_ready
    m2, _, _ = manager(tmp_path, monkeypatch, FakeEDA(active=SHA))
    assert await m2.ready_sha(None) == (True, SHA)               # Quest 1 / older server: the EDA server's own


# --- status reporter -------------------------------------------------------------------------

async def test_status_is_sent_on_change_and_periodically_never_twice_unchanged():
    sent = []
    api = SimpleNamespace(report_status=AsyncMock(side_effect=lambda p: sent.append(p) or 200))
    r = StatusReporter(api, every_seconds=3600)
    r.set("idle")
    assert await r.flush() and sent[-1]["state"] == "idle"
    assert not await r.flush()                                   # unchanged and not due
    r.set("evaluating", submission_id="s1")
    await r.flush()
    assert sent[-1]["state"] == "evaluating" and sent[-1]["submission_id"] == "s1" and sent[-1]["started_at"]
    r.every = 0
    assert await r.flush()                                       # due again


async def test_status_reports_stop_on_an_old_server():
    api = SimpleNamespace(report_status=AsyncMock(return_value=404))
    r = StatusReporter(api)
    await r.flush()
    await r.flush(force=True)
    assert api.report_status.await_count == 1


# --- batch processor ---------------------------------------------------------------------------

@pytest.fixture()
def processor(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIPFORGE_DATA_DIR", str(tmp_path))
    import logging
    # importing bittensor (in the test process, after this module) mutes loggers that already exist
    logging.getLogger("validator_utils.batch_processor").setLevel(logging.NOTSET)
    from validator_utils.validator_state import ValidatorState
    api = MagicMock()
    api.already_evaluated = set()
    api.download_batch_submissions = AsyncMock(return_value={"s1": b"zip"})
    api.evaluate_submissions_with_eda_server = AsyncMock(return_value={"s1": {
        "overall_score": 6.0, "functional_gate": True, "overall_gate": True, "eval_status": "PASSED",
        "evaluator_sha256": SHA}})
    api.submit_all_evaluations = AsyncMock(return_value={"s1": True})
    status = StatusReporter(SimpleNamespace())
    return BatchProcessor(api, ValidatorState(), status), api, status


async def test_batch_is_evaluated_with_the_batchs_evaluator(processor, caplog):
    import logging
    bp, api, status = processor
    batch = {"batch_id": "split-1", "evaluation_ends_at": "2099-01-01T00:00:00+00:00",
             "submissions": [{"submission_id": "s1", "miner_uid": 7}]}
    with caplog.at_level(logging.INFO):
        assert await bp.process_batch("c1", batch, SHA)
    assert api.evaluate_submissions_with_eda_server.await_args.kwargs["evaluator_sha256"] == SHA
    assert "split-1" in bp.state.evaluated_batches
    assert any("Evaluated s1 (UID 7): PASSED S=6.0000" in r.getMessage() for r in caplog.records)
    assert status.state == "submitting_result"


async def test_batch_already_evaluated_by_this_validator_is_done_not_failed(processor):
    bp, api, _ = processor
    api.download_batch_submissions = AsyncMock(return_value={})
    api.already_evaluated = {"s1"}
    assert await bp.process_batch("c1", {"batch_id": "b2", "submissions": [{"submission_id": "s1"}]})


def test_result_lines():
    assert result_line({"eval_status": "REJECTED", "evaluation_details": json.dumps(
        {"problems": [{"code": "NPU_PORT_MISMATCH"}]})}) == "REJECTED (NPU_PORT_MISMATCH)"
    assert result_line({"eval_status": "INCOMPLETE", "evaluation_details": json.dumps({"code": "EDA_INCOMPLETE"})}) \
        == "not scored (EDA_INCOMPLETE)"
    assert result_line({"timeout_occurred": True}) == "timeout"


# --- api client: claims, results, status are signed --------------------------------------------

async def test_claim_sends_the_ready_evaluator_and_results_carry_its_sha(make_client):
    from test_server_client import Resp, SYNC
    calls = {}

    def batch(m, kw):
        calls["claim"] = kw["params"]
        return Resp(200, {"batch_id": None})

    def score(m, kw):
        calls["score"] = kw["data"]
        return Resp(200, {"status": "ok"})
    client = make_client({"/api/v1/validator/sync": lambda m, kw: Resp(200, dict(SYNC, batch=None)),
                          "/api/v1/challenges/c1/batch/current": batch,
                          "/api/v1/challenges/c1/submissions/s1/submit_score": score})
    await client.get_current_batch("c1", claim=True, evaluator_sha256=SHA)
    assert calls["claim"]["evaluator_sha256"] == SHA
    ev = {"overall_score": 1, "functionality_score": 0, "area_score": 0, "delay_score": 0, "power_score": 0,
          "passed_testbench": True, "evaluator_sha256": SHA}
    assert await client.submit_evaluation("c1", "s1", ev)
    assert calls["score"]["evaluator_sha256"] == SHA


async def test_status_report_is_json_signed_over_its_bytes(make_client, keypair):
    from test_server_client import Resp
    got = {}

    def status(m, kw):
        got.update(kw)
        return Resp(200, {"status": "ok"})
    client = make_client({"/api/v1/validator/status": status}, mode="v2")
    assert await client.report_status({"state": "idle"}) == 200
    body = got["data"]
    assert json.loads(body) == {"state": "idle"} and got["headers"]["Content-Type"] == "application/json"
    h = got["headers"]
    message = (f"chipforge-sn108:v2:POST:/api/v1/validator/status:{keypair.ss58_address}:{h['X-Timestamp']}:"
               f"{h['X-Nonce']}:{hashlib.sha256(body).hexdigest()}")
    assert keypair.verify(message, bytes.fromhex(h["X-Signature"]))


async def test_evaluator_download_checks_the_hash_and_streams(make_client, tmp_path):
    from test_server_client import Resp
    content = b"E" * 3000

    class Stream(Resp):
        def __init__(self, data):
            super().__init__(200, data)
            self.content = SimpleNamespace(iter_chunked=self._chunks)

        async def _chunks(self, n):
            for i in range(0, len(self._body), 1000):
                yield self._body[i:i + 1000]

    sha = hashlib.sha256(content).hexdigest()
    path = f"/api/v1/challenges/c1/evaluator/{sha}/zip"
    client = make_client({path: lambda m, kw: Stream(content)})
    dest = tmp_path / "e.zip"
    assert await client.download_evaluator(path, dest, sha)
    assert dest.read_bytes() == content
    bad = tmp_path / "f.zip"
    client.session.routes[f"/api/v1/challenges/c1/evaluator/{'0' * 64}/zip"] = lambda m, kw: Stream(content)
    assert not await client.download_evaluator(f"/api/v1/challenges/c1/evaluator/{'0' * 64}/zip", bad, "0" * 64)
    assert not bad.exists()


# --- pentest fixes (2026-10-30) -----------------------------------------------------------------

@pytest.mark.parametrize("path", ["@evil.example/steal", ":9999@evil.example/x", ".evil.example/x",
                                  "/api/v1/challenges/c1/evaluator/" + "a" * 64 + "/zip?x=@evil",
                                  "https://evil.example/api/v1/challenges/c1/evaluator/" + "a" * 64 + "/zip"])
async def test_evaluator_paths_other_than_the_servers_own_are_refused(make_client, tmp_path, path):
    from test_server_client import Resp
    client = make_client({})                                  # any request would fail the test (no route)
    assert not await client.download_evaluator(path, tmp_path / "e.zip", "a" * 64)
    assert await client.download_evaluator_signature(path.replace("/zip", "/sshsig")) is None
    assert client.session.calls == []


async def test_a_result_from_another_evaluator_than_requested_is_a_system_fault(make_client, monkeypatch):
    from test_server_client import Resp

    class Session:
        def __init__(self):
            self.calls = []

        def post(self, url, **kw):
            return Resp(202, {"request_id": "r"})

        def get(self, url, **kw):
            return Resp(200, {"status": "PASSED", "public_report": {"score": {"S": {"value": 5.0}}}},
                        {"X-CF-Evaluator-Sha256": "b" * 64})
    client = make_client({})
    client.session = Session()
    client.chain_commits["s1"] = json.dumps({"hotkey": "5M"})
    ev = await client._evaluate_v2_one("s1", b"zip", None, evaluator_sha256="a" * 64)
    assert "VALIDATOR_EDA_WRONG_EVALUATOR" in ev["evaluation_details"] and ev["overall_score"] == 0
    Session.get = lambda self, url, **kw: Resp(200, {"status": "PASSED", "public_report": {"score": {"S": {"value": 5.0}}}},
                                               {"X-CF-Evaluator-Sha256": "a" * 64})
    ev = await client._evaluate_v2_one("s1", b"zip", None, evaluator_sha256="a" * 64)
    assert ev["overall_score"] == 5.0 and ev["evaluator_sha256"] == "a" * 64


def test_status_details_are_cut_to_what_the_server_accepts():
    r = StatusReporter(SimpleNamespace())
    r.set("install_failed", "EDA code: weird/stuff <x>\n" + "z" * 200)
    assert len(r.detail) <= 80 and all(c.isalnum() or c in " _.()'-" for c in r.detail)


async def test_results_the_validator_makes_itself_name_the_batchs_evaluator(processor):
    """No time left / EDA unreachable: the validator builds the result; it must still carry the evaluator."""
    bp, api, _ = processor
    api.evaluate_submissions_with_eda_server = AsyncMock(return_value={"s1": {
        "overall_score": 0.0, "timeout_occurred": True, "evaluation_details": json.dumps({"code": "VALIDATOR_NO_TIME_TO_START"})}})
    await bp.process_batch("c1", {"batch_id": "split-9", "submissions": [{"submission_id": "s1"}]}, SHA)
    sent = api.submit_all_evaluations.await_args.args[1]
    assert sent["s1"]["evaluator_sha256"] == SHA


async def test_a_batch_whose_results_are_refused_is_not_retried_every_cycle(processor):
    bp, api, _ = processor
    api.submit_all_evaluations = AsyncMock(return_value={"s1": False})
    api.last_submit_status = {"s1": 409}
    assert not await bp.process_batch("c1", {"batch_id": "split-r", "submissions": [{"submission_id": "s1"}]}, SHA)
    assert "split-r" in bp.state.evaluated_batches
    api.last_submit_status = {"s1": None}                                 # server unreachable: retried next cycle
    assert not await bp.process_batch("c1", {"batch_id": "split-n", "submissions": [{"submission_id": "s1"}]}, SHA)
    assert "split-n" not in bp.state.evaluated_batches
