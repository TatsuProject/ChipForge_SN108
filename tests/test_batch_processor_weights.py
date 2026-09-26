"""After a batch the processor only sets a weight target; the policy (percentage, bans)
is WeightManager's. This guards the old bug where the post-batch path gave 100%."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from validator_utils.batch_processor import BatchProcessor
from validator_utils.weight_manager import WeightTarget


@pytest.fixture()
def processor(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIPFORGE_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from validator_utils.emission_manager import EmissionManager
    from validator_utils.validator_state import ValidatorState

    api = MagicMock()
    api.get_challenge_info = AsyncMock(return_value={"winner_baseline_score": 10.0})
    api.download_batch_submissions = AsyncMock(return_value={"s1": b"zip"})
    api.submit_all_evaluations = AsyncMock(return_value={"s1": True})
    targets = []
    bp = BatchProcessor(api, ValidatorState(), EmissionManager(miner_emission_percentage=10), targets.append)
    # hotkey normally parsed from the downloaded filename
    bp.extract_hotkeys_from_filenames = lambda batch_id, subs: {"s1": "hk_winner"}
    return bp, api, targets


async def test_new_champion_sets_winner_target(processor):
    bp, api, targets = processor
    api.evaluate_submissions_with_eda_server = AsyncMock(return_value={
        "s1": {"overall_score": 50.0, "functional_gate": True, "overall_gate": True}})
    assert await bp.process_batch("c1", {"batch_id": "b1"})
    assert targets[-1].winner_hotkey == "hk_winner"
    assert bp.state.current_challenge_best == ("hk_winner", 50.0)
    assert "b1" in bp.state.evaluated_batches


async def test_gate_failed_score_does_not_crown(processor):
    bp, api, targets = processor
    api.evaluate_submissions_with_eda_server = AsyncMock(return_value={
        "s1": {"overall_score": 90.0, "functional_gate": False, "overall_gate": True}})
    await bp.process_batch("c1", {"batch_id": "b1"})
    assert all(t.winner_hotkey != "hk_winner" for t in targets)
    assert targets[-1] == WeightTarget.burn("no qualified winner")


async def test_hotkey_comes_from_batch_entry_not_filename(processor):
    bp, api, targets = processor
    bp.extract_hotkeys_from_filenames = lambda batch_id, subs: {"s1": "hk_from_filename"}
    api.evaluate_submissions_with_eda_server = AsyncMock(return_value={
        "s1": {"overall_score": 50.0, "functional_gate": True, "overall_gate": True}})
    await bp.process_batch("c1", {"batch_id": "b2", "submissions": [{"submission_id": "s1", "hotkey": "hk_server"}]})
    assert targets[-1].winner_hotkey == "hk_server"


async def test_deadline_passed_to_eda(processor):
    bp, api, targets = processor
    api.evaluate_submissions_with_eda_server = AsyncMock(return_value={})
    await bp.process_batch("c1", {"batch_id": "b3", "evaluation_ends_at": "2026-09-26T12:00:00+00:00"})
    deadline = api.evaluate_submissions_with_eda_server.call_args.kwargs["deadline"]
    assert deadline.isoformat() == "2026-09-26T12:00:00+00:00"


def test_submissions_dir_follows_data_dir(processor, tmp_path):
    """The filename fallback must look where the API client saves downloads (found by
    running the container with CHIPFORGE_DATA_DIR=/data and a read-only working dir)."""
    bp, _, _ = processor
    assert bp.submissions_dir == tmp_path / "validator_data" / "submissions"


async def test_min_improvement_margin(processor, monkeypatch):
    """With MIN_IMPROVEMENT_PERCENT the score must clear the baseline by that margin (same rule as the server)."""
    bp, api, targets = processor
    api.evaluate_submissions_with_eda_server = AsyncMock(return_value={
        "s1": {"overall_score": 10.05, "functional_gate": True, "overall_gate": True}})       # baseline is 10.0
    monkeypatch.setenv("MIN_IMPROVEMENT_PERCENT", "1")
    await bp.process_batch("c1", {"batch_id": "m1"})
    assert all(t.winner_hotkey != "hk_winner" for t in targets)
    monkeypatch.setenv("MIN_IMPROVEMENT_PERCENT", "0")
    await bp.process_batch("c1", {"batch_id": "m2"})
    assert targets[-1].winner_hotkey == "hk_winner"
