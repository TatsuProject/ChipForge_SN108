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
    v._last_ladder_report = None
    v.weight_manager = _manager(10.0)
    v.banned_coldkeys = SimpleNamespace(get_active_ban_set=lambda challenge_id: set())
    v.state = SimpleNamespace(last_challenge_id="c", ban_emissions=False)
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


# --- logs: one ladder report when something changes, legacy reward lines silenced ----------------

def test_report_names_uids_states_and_unpayable_places():
    """The testnet run of 2026-10-15: UID 0 (the burn UID) holds place 1, an unregistered hotkey
    place 3, one record is in probation."""
    recs = [rec("r4", "hk_owner", 0.1, 100, probation=0, paid=1), rec("r3", "HD", 0.3, 50, probation=0, paid=1),
            rec("r2", "hk_unregistered", 0.4, 30, probation=0, paid=1), rec("r5", "HC", 0.05, 120, probation=1, paid=1),
            rec("old", "HB", 3, 10, probation=0, paid=1)]
    wm = _manager(10.0)
    sig, lines = rl.report(recs, NOW, 3, 0.4, lambda hk: wm.payability(hk), 10.0)
    text = "\n".join(lines)
    assert lines[0] == ("Reward ladder (3 places, ratio 0.4): 3 paying, 1 in probation; miners get 10% of the weight, "
                        "burn 90% (unpaid places' share goes to the paid ones)")
    assert "#1 UID 0 hk_owner... score 100: 64.1% of the miners' share -> 0.00% of all weight" in text
    assert "NOT PAID (is the burn UID)" in text
    assert "#2 UID 2 HD... score 50: 25.6% of the miners' share -> 10.00% of all weight" in text
    assert "#3 no UID hk_unreg... score 30: 10.3% of the miners' share -> 0.00% of all weight" in text
    assert "NOT PAID (not registered)" in text
    # all three payable: 64.1 / 25.6 / 10.3 % of the miners' 10%
    ok = rl.report(recs[1:2] + [rec("x", "HA", 0.2, 60, probation=0, paid=1)], NOW, 3, 0.4, lambda hk: wm.payability(hk), 10.0)[1]
    assert "#1 UID 4 HA... score 60: 71.4% of the miners' share -> 7.14% of all weight" in "\n".join(ok)
    assert "#2 UID 2 HD... score 50: 28.6% of the miners' share -> 2.86% of all weight" in "\n".join(ok)
    assert "probation UID 3 HC... score 120: paid from 12:57 UTC (in 57m)" in text
    assert "old" not in [s[0] for s in sig[:-2]]                               # finished records are left out
    assert rl.report(recs, NOW, 3, 0.4, lambda hk: wm.payability(hk), 10.0)[0] == sig    # same state: same signature
    assert rl.report(recs, NOW, 3, 0.4, lambda hk: wm.payability(hk), 20.0)[0] != sig    # miners' % changed
    assert rl.report([], NOW, 3, 0.4, lambda hk: wm.payability(hk))[1] == [
        "Reward ladder: no record in probation or being paid - the miners' share is burned"]


def test_ladder_report_is_logged_once_per_change_and_legacy_lines_go_to_debug(tmp_path, monkeypatch, caplog):
    import logging
    from validator_utils.logutil import RewardLog
    soon = datetime.now(timezone.utc)
    paying = {"submission_id": "p", "hotkey": "HD", "score": 1, "qualified_at": (soon - timedelta(hours=30)).isoformat(),
              "pays_from": (soon - timedelta(hours=6)).isoformat(), "pays_until": (soon + timedelta(hours=18)).isoformat()}
    v = _validator(tmp_path, monkeypatch, {"reward_ladder": {"size": 3, "decay_ratio": 0.6, "records": [paying]}})
    v.refresh_reward_ladder()
    assert RewardLog.ladder_active
    with caplog.at_level(logging.INFO):
        v.effective_target()
        v.effective_target()
        RewardLog(logging.getLogger("validator_utils.emission_manager")).info("Winner reward active: ...")
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert sum(m.startswith("Reward ladder (") for m in infos) == 1
    assert any("#1 UID 2 HD... score 1: 100.0% of the miners' share -> 10.00% of all weight" in m for m in infos)
    assert not any("Winner reward active" in m for m in infos)

    v.api_client._sync_state = {"challenge": None}          # older server: the own-winner logs are back
    v.refresh_reward_ladder()
    assert not RewardLog.ladder_active


# --- corner cases (audit 2026-10-15): bans, emissions ban, voids, restarts, boundaries -------------
# The server's side of each case is in chipforge_challenge_server tests/test_core_flows.py.

def _paying(sid, hotkey, decided_hours_ago, score=1.0):
    return rec(sid, hotkey, decided_hours_ago, score, probation=1, paid=24)


def test_banned_in_probation_is_not_paid_once_its_window_opens():
    """Banned while in probation: when the window opens the place is skipped and its share goes
    to the others; banned alone = burn."""
    recs = [rec("b", "H_banned", 0.5, 50, probation=1, paid=24), _paying("d", "HD", 5)]
    later = NOW + timedelta(hours=1)                                       # b's probation is over
    target = WeightTarget.ladder(rl.places(recs, later, 3, 0.6), "t")
    assert [hk for hk, _ in target.places] == ["H_banned", "HD"]           # the ban does not move places
    uids, weights, desc = _manager(10.0).build(target, banned_coldkeys={"ck_bad"})
    assert uids == [0, 2] and weights == [0.9, 0.1] and "banned" in desc   # HD gets the whole 10%
    alone = WeightTarget.ladder(rl.places(recs[:1], later, 3, 0.6), "t")
    assert _manager(10.0).build(alone, banned_coldkeys={"ck_bad"})[:2] == ([0], [1.0])
    assert _manager(10.0).build(alone)[:2] == ([0, 5], [0.9, 0.1])         # unbanned: paid again


def test_local_ban_list_covers_only_the_current_quest(tmp_path, monkeypatch):
    """The validator's own ban list applies a quest-A ban only while A is its current quest. A's
    records still paid after A ended are covered by the server's banned flag on each record
    (test_server_banned_record_is_not_paid_after_its_quest_ended)."""
    monkeypatch.setenv("CHIPFORGE_DATA_DIR", str(tmp_path))
    from validator_utils.banned_coldkeys import BannedColdkeysManager
    bans = BannedColdkeysManager()
    bans.update_from_response("quest_a", {"bans": [{"coldkey": "ck_bad", "scope": "challenge", "challenge_id": "quest_a"}]})
    assert bans.get_active_ban_set("quest_a") == {"ck_bad"}
    assert bans.get_active_ban_set(None) == set()                         # quest A over: no active quest
    assert bans.get_active_ban_set("quest_b") == set()                    # quest B running
    target = WeightTarget(None, "t", (("H_banned", 1.0),))
    assert _manager(10.0).build(target, banned_coldkeys=bans.get_active_ban_set(None))[:2] == ([0, 5], [0.9, 0.1])
    # a permanent ban survives (cached on disk)
    bans.update_from_response("quest_a", {"bans": [{"coldkey": "ck_bad", "scope": "permanent"}]})
    assert BannedColdkeysManager().get_active_ban_set(None) == {"ck_bad"}


def test_ban_emissions_burns_everything_and_the_report_says_so():
    recs = [_paying("d", "HD", 5), _paying("c", "HC", 4)]
    target = WeightTarget.ladder(rl.places(recs, NOW, 3, 0.6), "t")
    assert _manager(10.0).build(target, ban_emissions=True)[:2] == ([0], [1.0])
    _, lines = rl.report(recs, NOW, 3, 0.6, lambda hk: _manager(10.0).payability(hk), 0.0)
    assert "miners get 0% of the weight, burn 100%" in lines[0] and "-> 0.00% of all weight" in lines[1]
    sig, lines = rl.report(recs + [rec("p", "HA", 1, 9)], NOW, 3, 0.6, lambda hk: _manager(10.0).payability(hk), 0.0,
                           emissions_banned=True)
    assert "emissions banned by challenge server: burn 100%" in lines[0]
    assert all("NOT PAID (emissions banned by challenge server)" in l for l in lines if l.startswith("  #"))
    assert any(l.startswith("  probation") and "not paid while emissions are banned" in l for l in lines)
    assert rl.report(recs, NOW, 3, 0.6, lambda hk: _manager(10.0).payability(hk), 0.0)[0] != \
        rl.report(recs, NOW, 3, 0.6, lambda hk: _manager(10.0).payability(hk), 0.0, emissions_banned=True)[0]


def test_voided_record_stops_being_paid_on_the_next_sync_but_not_while_the_server_is_down(tmp_path, monkeypatch):
    soon = datetime.now(timezone.utc)
    a = {"submission_id": "a", "hotkey": "HD", "score": 1, "qualified_at": (soon - timedelta(hours=30)).isoformat(),
         "pays_from": (soon - timedelta(hours=6)).isoformat(), "pays_until": (soon + timedelta(hours=18)).isoformat()}
    b = {**a, "submission_id": "b", "hotkey": "HC", "qualified_at": (soon - timedelta(hours=29)).isoformat()}
    v = _validator(tmp_path, monkeypatch, {"reward_ladder": {"size": 3, "decay_ratio": 0.6, "records": [a, b]}})
    v.refresh_reward_ladder()
    assert [hk for hk, _ in v.effective_target().places] == ["HC", "HD"]
    v.api_client._sync_state = None                                       # server down: keeps paying b
    v.refresh_reward_ladder()
    assert [hk for hk, _ in v.effective_target().places] == ["HC", "HD"]
    v.api_client._sync_state = {"reward_ladder": {"size": 3, "decay_ratio": 0.6, "records": [a]}}   # b voided
    v.refresh_reward_ladder()
    assert v.effective_target().places == (("HD", 1.0),)
    assert rl.RewardLadder().schedule["records"] == [a]                   # a restart keeps the new schedule


def test_deregistered_then_re_registered_miner_is_paid_under_its_new_uid():
    target = WeightTarget(None, "t", (("HA", 0.6), ("HD", 0.4)))
    wm = _manager(100.0)
    assert wm.build(target)[:2] == ([4, 2], [0.6, 0.4])
    wm.metagraph.hotkeys[4] = "someone_else"                              # HA deregistered
    assert wm.build(target)[:2] == ([2], [1.0])
    wm.metagraph.hotkeys[3] = "HA"                                        # HA back under UID 3
    assert wm.build(target)[:2] == ([3, 2], [0.6, 0.4])


def test_unpayable_record_still_holds_a_place():
    """Places are chosen before payability: a banned (or unregistered) newer record takes a place,
    so an older payable record beyond `size` is not paid; the share goes to the paid places."""
    recs = [_paying("new", "H_banned", 3), _paying("mid", "HD", 4), _paying("old", "HC", 5)]
    target = WeightTarget.ladder(rl.places(recs, NOW, 2, 0.6), "t")
    assert [hk for hk, _ in target.places] == ["H_banned", "HD"]           # HC is third: no place
    uids, weights, _ = _manager(100.0).build(target, banned_coldkeys={"ck_bad"})
    assert uids == [2] and weights == [1.0]


def test_window_boundaries():
    r = rec("x", "HD", 10, 1, probation=2, paid=3)
    starts, ends = rl._time(r["pays_from"]), rl._time(r["pays_until"])
    assert rl.record_state(r, starts - timedelta(microseconds=1)) == "probation"
    assert rl.record_state(r, starts) == "paying"
    assert rl.record_state(r, ends - timedelta(microseconds=1)) == "paying"
    assert rl.record_state(r, ends) == "finished"
    zero = rec("z", "HD", 0, 1, probation=0, paid=1)                       # PROBATION_HOURS=0: paid at once
    assert rl.record_state(zero, NOW) == "paying"


def test_miner_emission_percentage_extremes():
    target = WeightTarget(None, "t", (("HD", 0.6), ("HC", 0.4)))
    assert _manager(0.0).build(target)[:2] == ([0], [1.0])
    assert _manager(100.0).build(target)[:2] == ([2, 3], [0.6, 0.4])       # no burn UID entry at all


def test_ban_emissions_from_sync_applies_with_no_active_quest(tmp_path, monkeypatch):
    soon = datetime.now(timezone.utc)
    paying = {"submission_id": "p", "hotkey": "HD", "score": 1, "qualified_at": (soon - timedelta(hours=30)).isoformat(),
              "pays_from": (soon - timedelta(hours=6)).isoformat(), "pays_until": (soon + timedelta(hours=18)).isoformat()}
    ladder = {"size": 3, "decay_ratio": 0.6, "records": [paying]}
    v = _validator(tmp_path, monkeypatch, {"challenge": None, "reward_ladder": ladder, "ban_emissions": True})
    v.state.save_state = lambda: None
    v.refresh_reward_ladder()
    assert v.state.ban_emissions is True
    assert v.weight_manager.build(v.effective_target(), ban_emissions=v.state.ban_emissions)[:2] == ([0], [1.0])
    v.api_client._sync_state = {"challenge": None, "reward_ladder": ladder, "ban_emissions": False}   # lifted
    v.refresh_reward_ladder()
    assert v.state.ban_emissions is False
    assert v.weight_manager.build(v.effective_target(), ban_emissions=False)[:2] == ([0, 2], [0.9, 0.1])
    v.api_client._sync_state = {"challenge": None, "reward_ladder": ladder}                 # older server: unchanged
    v.state.ban_emissions = True
    v.refresh_reward_ladder()
    assert v.state.ban_emissions is True


def test_server_banned_record_is_not_paid_after_its_quest_ended(tmp_path, monkeypatch):
    """Coldkey banned for quest A: the server sends A's records with banned=true, so they are not
    paid even when no quest (or quest B) is current and the local ban list has nothing for A."""
    soon = datetime.now(timezone.utc)

    def paying(sid, hotkey, hours_ago, banned=False):
        return {"submission_id": sid, "hotkey": hotkey, "score": 1, "banned": banned,
                "qualified_at": (soon - timedelta(hours=hours_ago)).isoformat(),
                "pays_from": (soon - timedelta(hours=6)).isoformat(), "pays_until": (soon + timedelta(hours=18)).isoformat()}
    a, b = paying("a", "HA", 30, banned=True), paying("b", "HD", 31)
    v = _validator(tmp_path, monkeypatch, {"challenge": None, "reward_ladder": {"size": 3, "decay_ratio": 0.6, "records": [a, b]}})
    v.state.last_challenge_id = None
    v.refresh_reward_ladder()
    target = v.effective_target()
    assert [hk for hk, _ in target.places] == ["HA", "HD"] and target.banned == ("HA",)    # keeps its place
    uids, weights, desc = v.weight_manager.build(target, banned_coldkeys=set())
    assert uids == [0, 2] and weights == [0.9, 0.1] and "coldkey banned for its quest" in desc
    _, lines = v.reward_ladder.report(lambda hk: v.weight_manager.payability(hk, set()), 10.0)
    assert any("#1 UID 4 HA..." in l and "NOT PAID (coldkey banned for its quest)" in l and "-> 0.00% of all weight" in l
               for l in lines)

    v.api_client._sync_state = {"challenge": None, "reward_ladder": {"size": 3, "decay_ratio": 0.6,
                                                                     "records": [{**a, "banned": False}, b]}}   # unbanned
    v.refresh_reward_ladder()
    target = v.effective_target()
    assert target.banned == () and v.weight_manager.build(target)[:2] == ([0, 4, 2], [0.9, 0.0625, 0.0375])
    only = WeightTarget.ladder([{"hotkey": "HA", "share": 1.0, "banned": True}], "t")
    assert v.weight_manager.build(only)[:2] == ([0], [1.0])                                 # banned alone: burn
