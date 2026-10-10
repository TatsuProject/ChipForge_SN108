"""The challenge server bumps baseline_epoch when it voids a record or an admin changes the score to
beat: the validator then resets its local challenge best and winner to the server's."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock


def make_validator(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIPFORGE_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from neurons.validator import ChipForgeValidator
    from validator_utils.emission_manager import EmissionManager
    from validator_utils.validator_state import ValidatorState
    v = ChipForgeValidator.__new__(ChipForgeValidator)
    v.state = ValidatorState()
    v.emission_manager = EmissionManager(miner_emission_percentage=10)
    v._target_current_challenge_reward = MagicMock()
    return v


def test_first_epoch_is_only_remembered(tmp_path, monkeypatch):
    v = make_validator(tmp_path, monkeypatch)
    v.state.current_challenge_best = ("hk_cheat", 2067.3)
    assert v.apply_baseline_epoch({"baseline_epoch": 0, "winner_baseline_score": 2067.3}) is False
    assert v.state.baseline_epoch == 0 and v.state.current_challenge_best == ("hk_cheat", 2067.3)
    assert v.apply_baseline_epoch({"baseline_epoch": None}) is False          # older server


def test_new_epoch_resets_to_server_winner(tmp_path, monkeypatch):
    v = make_validator(tmp_path, monkeypatch)
    v.state.baseline_epoch = 0
    v.state.current_challenge_best = ("hk_cheat", 2067.3)
    v.emission_manager.current_winner, v.emission_manager.current_winner_score = "hk_cheat", 2067.3
    won = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    assert v.apply_baseline_epoch({"baseline_epoch": 1, "winner_baseline_score": 1456.2,
                                   "current_winner": {"hotkey": "hk_prev", "score": 1456.2, "qualified_at": won}})
    assert v.state.current_challenge_best == ("hk_prev", 1456.2) and v.state.winner_baseline_score == 1456.2
    assert v.emission_manager.current_winner == "hk_prev"
    assert v.emission_manager.winner_reward_start_time.isoformat() == won       # original window, not a fresh one
    assert v.state.baseline_epoch == 1
    v._target_current_challenge_reward.assert_called_once()


def test_new_epoch_without_winner_burns(tmp_path, monkeypatch):
    v = make_validator(tmp_path, monkeypatch)
    v.state.baseline_epoch = 3
    v.state.current_challenge_best = ("hk_cheat", 120.0)
    assert v.apply_baseline_epoch({"baseline_epoch": 4, "winner_baseline_score": 50.0, "current_winner": None},
                                  retarget=False)
    assert v.state.current_challenge_best == (None, 0.0) and v.emission_manager.current_winner is None
    v._target_current_challenge_reward.assert_not_called()
