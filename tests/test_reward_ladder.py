"""Record ladder on the validator: same places as the server (app/services/ladder.py), weights per
place, unpayable places shared among the others, schedule kept on disk, fallback on old servers."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bittensor.core.types import ExtrinsicResponse

from validator_utils import reward_ladder as rl
from validator_utils.weight_manager import WeightManager, WeightTarget

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def rec(sid, hotkey, hours_ago, score, probation=24, paid=24):
    decided = NOW - timedelta(hours=hours_ago)
    return {"submission_id": sid, "hotkey": hotkey, "score": score, "qualified_at": decided.isoformat(),
            "pays_from": (decided + timedelta(hours=probation)).isoformat(),
            "pays_until": (decided + timedelta(hours=probation + paid)).isoformat()}


# Same vectors as the server's test_ladder_places_one_per_hotkey_newest_first_with_decaying_shares
RECORDS = [rec("a", "HA", 47, 10), rec("b", "HB", 40, 20), rec("c", "HA", 35, 30), rec("d", "HC", 30, 40),
           rec("e", "HD", 26, 50), rec("f", "HE", 5, 60), rec("g", "HF", 49, 5)]


def test_places_match_the_server():
    got = rl.places(RECORDS, NOW, 3, 0.6)
    assert [p["submission_id"] for p in got] == ["e", "d", "c"]
    assert [round(p["share"], 4) for p in got] == [0.5102, 0.3061, 0.1837]
    assert [p["submission_id"] for p in rl.places(RECORDS, NOW, 4, 0.6)] == ["e", "d", "c", "b"]
    assert [round(p["share"], 4) for p in rl.places(RECORDS, NOW, 2, 1.0)] == [0.5, 0.5]
    assert rl.places(RECORDS, NOW - timedelta(days=5), 3, 0.6) == []


@pytest.mark.parametrize("n, expected", [(1, [1.0]), (2, [0.625, 0.375]), (3, [0.5102, 0.3061, 0.1837])])
def test_shares(n, expected):
    assert [round(s, 4) for s in rl.shares(n, 0.6)] == expected


def test_probation_end_and_window_end_happen_on_the_validators_clock():
    records = [rec("x", "HX", 23, 10)]                     # probation ends in 1h
    assert rl.places(records, NOW, 3, 0.6) == []
    assert [p["submission_id"] for p in rl.places(records, NOW + timedelta(hours=1), 3, 0.6)] == ["x"]
    assert rl.places(records, NOW + timedelta(hours=25), 3, 0.6) == []


# --- schedule on disk ----------------------------------------------------------------------

def test_schedule_survives_restart_and_rejects_malformed(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIPFORGE_DATA_DIR", str(tmp_path))
    ladder = rl.RewardLadder()
    assert not ladder.active
    schedule = {"size": 3, "decay_ratio": 0.6, "probation_hours": 24, "reward_hours": 24, "records": RECORDS}
    assert ladder.update(schedule) and not ladder.update(dict(schedule))
    again = rl.RewardLadder()
    assert again.active and [p["submission_id"] for p in again.places(NOW)] == ["e", "d", "c"]
    assert not again.update({"size": 3, "records": [{"hotkey": "H"}]})      # missing times: ignored
    assert again.schedule == schedule
    again.clear()
    assert not again.active and not rl.RewardLadder().active


# --- weights -------------------------------------------------------------------------------

HOTKEYS = ["hk_owner", "hk_validator", "HD", "HC", "HA", "H_banned"]
COLDKEYS = ["ck0", "ck1", "ck2", "ck3", "ck4", "ck_bad"]


def _manager(percentage):
    metagraph = SimpleNamespace(hotkeys=list(HOTKEYS), coldkeys=list(COLDKEYS))
    subtensor = MagicMock()
    subtensor.set_weights.return_value = ExtrinsicResponse(True, "ok")
    return WeightManager(MagicMock(), subtensor, metagraph, SimpleNamespace(netuid=108),
                         miner_emission_percentage=percentage)


def test_ladder_weights_split_the_miner_emission_by_share():
    target = WeightTarget.ladder(rl.places(RECORDS, NOW, 3, 0.6), "t")
    uids, weights, _ = _manager(100.0).build(target)
    assert uids == [2, 3, 4] and [round(w, 4) for w in weights] == [0.5102, 0.3061, 0.1837]
    uids, weights, _ = _manager(50.0).build(target)
    assert uids == [0, 2, 3, 4] and [round(w, 4) for w in weights] == [0.5, 0.2551, 0.1531, 0.0918]


def test_unpayable_place_is_shared_among_the_others():
    target = WeightTarget(None, "t", (("HD", 0.5), ("hk_unregistered", 0.3), ("H_banned", 0.2)))
    uids, weights, desc = _manager(100.0).build(target, banned_coldkeys={"ck_bad"})
    assert uids == [2] and weights == [1.0] and "not registered" in desc and "banned" in desc
    target = WeightTarget(None, "t", (("HD", 0.6), ("HC", 0.3), ("hk_owner", 0.1)))
    uids, weights, _ = _manager(100.0).build(target)
    assert uids == [2, 3] and [round(w, 4) for w in weights] == [0.6667, 0.3333]


def test_ladder_burns_when_nothing_is_payable_or_emissions_banned():
    assert WeightTarget.ladder([], "none") == WeightTarget.burn("none")
    wm = _manager(100.0)
    assert wm.build(WeightTarget(None, "t", (("hk_unregistered", 1.0),)))[:2] == ([0], [1.0])
    assert wm.build(WeightTarget.ladder(rl.places(RECORDS, NOW, 3, 0.6)), ban_emissions=True)[:2] == ([0], [1.0])
    assert _manager(0.0).build(WeightTarget.ladder(rl.places(RECORDS, NOW, 3, 0.6)))[:2] == ([0], [1.0])


# --- validator: the ladder decides when the server sends one ----------------------------------

def _validator(tmp_path, monkeypatch, sync_state):
    monkeypatch.setenv("CHIPFORGE_DATA_DIR", str(tmp_path))
    from validator import ChipForgeValidator
    v = ChipForgeValidator.__new__(ChipForgeValidator)
    v.reward_ladder = rl.RewardLadder()
    v._last_ladder_target = None
    v.weight_target = WeightTarget.winner("HC", "own winner")
    v.api_client = SimpleNamespace(_sync_state=sync_state)
    return v


def test_validator_follows_the_ladder_and_falls_back_on_an_old_server(tmp_path, monkeypatch):
    soon = datetime.now(timezone.utc)
    paying = {"submission_id": "p", "hotkey": "HD", "score": 1, "qualified_at": (soon - timedelta(hours=30)).isoformat(),
              "pays_from": (soon - timedelta(hours=6)).isoformat(), "pays_until": (soon + timedelta(hours=18)).isoformat()}
    v = _validator(tmp_path, monkeypatch, {"reward_ladder": {"size": 3, "decay_ratio": 0.6, "records": [paying]}})
    v.refresh_reward_ladder()
    assert v.effective_target().places == (("HD", 1.0),)

    v.api_client._sync_state = None                         # server unreachable: keep following the ladder
    v.refresh_reward_ladder()
    assert v.effective_target().places == (("HD", 1.0),)

    v.api_client._sync_state = {"challenge": None}          # older server: no ladder
    v.refresh_reward_ladder()
    assert not v.reward_ladder.active and v.effective_target() == WeightTarget.winner("HC", "own winner")


def test_all_records_in_probation_means_burn(tmp_path, monkeypatch):
    soon = datetime.now(timezone.utc)
    waiting = {"submission_id": "w", "hotkey": "HD", "score": 1, "qualified_at": soon.isoformat(),
               "pays_from": (soon + timedelta(hours=24)).isoformat(), "pays_until": (soon + timedelta(hours=48)).isoformat()}
    v = _validator(tmp_path, monkeypatch, {"reward_ladder": {"size": 3, "decay_ratio": 0.6, "records": [waiting]}})
    v.refresh_reward_ladder()
    target = v.effective_target()
    assert target.winner_hotkey is None and not target.places and "1 record(s) in probation" in target.reason
