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
    assert v.state.current_challenge_best == (None, 50.0) and v.emission_manager.current_winner is None
    v._target_current_challenge_reward.assert_not_called()


def test_manual_lower_score_is_the_bar_here_too(tmp_path, monkeypatch):
    """Admin lowers the score to beat to 1000 while the current winner scored 1456: a new 1200 must
    win on the validator as it does on the server, so the local bar is 1000, not 1456."""
    from validator_utils.batch_processor import beats
    v = make_validator(tmp_path, monkeypatch)
    v.state.baseline_epoch = 1
    v.state.current_challenge_best = ("hk_prev", 1456.2)
    v.apply_baseline_epoch({"baseline_epoch": 2, "winner_baseline_score": 1000.0,
                            "current_winner": {"hotkey": "hk_prev", "score": 1456.2}})
    hotkey, bar = v.state.current_challenge_best
    assert hotkey == "hk_prev" and bar == 1000.0 and v.state.winner_baseline_score == 1000.0
    assert beats(1200.0, bar, 0.0) and beats(1200.0, v.state.winner_baseline_score, 0.0)


def test_restored_record_resumes_from_server_reward_end(tmp_path, monkeypatch):
    """The server sends the restored record's reward end (its unpaid hours from now)."""
    v = make_validator(tmp_path, monkeypatch)
    v.state.baseline_epoch = 0
    v.emission_manager.total_hours_for_winner_reward = 24.0
    ends = datetime.now(timezone.utc) + timedelta(hours=18)
    v.apply_baseline_epoch({"baseline_epoch": 1, "winner_baseline_score": 900.0, "current_winner": {
        "hotkey": "hk4", "score": 900.0, "qualified_at": "2026-10-01T00:00:00+00:00", "reward_expires_at": ends.isoformat()}})
    end = v.emission_manager.winner_reward_start_time + timedelta(hours=24)
    assert abs((end - ends).total_seconds()) < 1          # paid until the server's end, not 24h from now
    assert v.emission_manager.get_reward_hotkey("hk4", 900.0) == "hk4"
