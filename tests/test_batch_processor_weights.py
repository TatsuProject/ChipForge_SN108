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
