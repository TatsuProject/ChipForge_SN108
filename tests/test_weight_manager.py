"""WeightManager: one weight policy (build) and change/rate-limit aware submission (apply),
against a fake subtensor that returns bittensor's real ExtrinsicResponse."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from bittensor.core.types import ExtrinsicResponse

from validator_utils.weight_manager import WeightManager, WeightTarget, response_succeeded

HOTKEYS = ["hk_owner", "hk_validator", "hk_winner", "hk_banned"]
COLDKEYS = ["ck0", "ck1", "ck2", "ck_bad"]


def _metagraph():
    neurons = [SimpleNamespace(hotkey=h, coldkey=c) for h, c in zip(HOTKEYS, COLDKEYS)]
    return SimpleNamespace(neurons=neurons, hotkeys=list(HOTKEYS), coldkeys=list(COLDKEYS))


def _manager(percentage=10.0, response=None, rate_limited=False, refresh_seconds=1200):
    subtensor = MagicMock()
    subtensor.set_weights.return_value = response if response is not None else ExtrinsicResponse(True, "ok")
    subtensor.blocks_since_last_update.return_value = 5 if rate_limited else 500
    subtensor.weights_rate_limit.return_value = 100
    wallet = MagicMock()
    wallet.hotkey.ss58_address = "hk_validator"
    wm = WeightManager(wallet, subtensor, _metagraph(), SimpleNamespace(netuid=108),
                       miner_emission_percentage=percentage, refresh_seconds=refresh_seconds)
    return wm, subtensor


# --- the bug: ExtrinsicResponse is always truthy ------------------------------------------

def test_extrinsic_response_is_always_truthy_so_success_must_be_read():
    failed = ExtrinsicResponse(False, "rate limit exceeded")
    assert bool(failed) is True              # why `if success:` never saw failures
    assert response_succeeded(failed) is False
    assert response_succeeded(ExtrinsicResponse(True, "ok")) is True
    assert response_succeeded(None) is False


# --- policy ------------------------------------------------------------------------------

def test_winner_gets_configured_percentage_rest_burned():
    wm, _ = _manager(percentage=10.0)
    uids, weights, _ = wm.build(WeightTarget.winner("hk_winner", "t"))
    assert uids == [0, 2] and weights == [0.9, 0.1]


def test_hundred_percent_goes_only_to_winner():
    wm, _ = _manager(percentage=100.0)
    assert wm.build(WeightTarget.winner("hk_winner"))[:2] == ([2], [1.0])


@pytest.mark.parametrize("target, kwargs", [
    (WeightTarget.burn("no winner"), {}),
    (WeightTarget.winner("hk_winner"), {"ban_emissions": True}),
    (WeightTarget.winner("hk_banned"), {"banned_coldkeys": {"ck_bad"}}),
    (WeightTarget.winner("hk_not_registered"), {}),
    (WeightTarget.winner("hk_owner"), {}),
])
def test_burn_cases(target, kwargs):
    wm, _ = _manager()
    assert wm.build(target, **kwargs)[:2] == ([0], [1.0])


# --- submission --------------------------------------------------------------------------

async def test_apply_submits_lists_without_waiting_for_finalization():
    wm, subtensor = _manager()
    assert await wm.apply(WeightTarget.winner("hk_winner"))
    kwargs = subtensor.set_weights.call_args.kwargs
    assert kwargs["uids"] == [0, 2] and kwargs["weights"] == [0.9, 0.1]
    assert kwargs["netuid"] == 108 and kwargs["wait_for_finalization"] is False


async def test_rejected_extrinsic_is_a_failure_and_is_retried_after_backoff(monkeypatch):
    import validator_utils.weight_manager as wm_mod
    clock = [1000.0]
    monkeypatch.setattr(wm_mod.time, "monotonic", lambda: clock[0])
    wm, subtensor = _manager(response=ExtrinsicResponse(False, "rejected"))
    assert await wm.apply(WeightTarget.winner("hk_winner")) is False
    assert await wm.apply(WeightTarget.winner("hk_winner")) is False     # next loop: backing off
    assert subtensor.set_weights.call_count == 1
    clock[0] += 31
    subtensor.set_weights.return_value = ExtrinsicResponse(True, "ok")
    assert await wm.apply(WeightTarget.winner("hk_winner")) is True
    assert subtensor.set_weights.call_count == 2


async def test_backoff_grows_and_a_new_target_is_tried_at_once(monkeypatch):
    import validator_utils.weight_manager as wm_mod
    clock = [1000.0]
    monkeypatch.setattr(wm_mod.time, "monotonic", lambda: clock[0])
    wm, subtensor = _manager(response=ExtrinsicResponse(False, "not registered"))
    delays = []
    for _ in range(6):
        await wm.apply(WeightTarget.burn())
        delays.append(wm._retry_at - clock[0])
        clock[0] = wm._retry_at
    assert delays == [30, 60, 120, 240, 480, 600]                         # capped at 10 minutes
    assert subtensor.set_weights.call_count == 6
    await wm.apply(WeightTarget.winner("hk_winner"))                      # different weights
    assert subtensor.set_weights.call_count == 7


async def test_unchanged_weights_are_not_resubmitted():
    wm, subtensor = _manager()
    await wm.apply(WeightTarget.winner("hk_winner", "a"))
    await wm.apply(WeightTarget.winner("hk_winner", "different reason, same weights"))
    assert subtensor.set_weights.call_count == 1


async def test_periodic_refresh_resubmits_same_weights():
    wm, subtensor = _manager(refresh_seconds=0)
    await wm.apply(WeightTarget.burn())
    await wm.apply(WeightTarget.burn())
    assert subtensor.set_weights.call_count == 2


async def test_new_winner_waits_for_rate_limit_then_goes_out():
    wm, subtensor = _manager(rate_limited=True)
    assert await wm.apply(WeightTarget.winner("hk_winner")) is False
    assert subtensor.set_weights.call_count == 0
    subtensor.blocks_since_last_update.return_value = 100       # window reopens
    assert await wm.apply(WeightTarget.winner("hk_winner")) is True
    assert subtensor.set_weights.call_count == 1


async def test_exception_from_chain_is_contained():
    wm, subtensor = _manager()
    subtensor.set_weights.side_effect = RuntimeError("websocket closed")
    assert await wm.apply(WeightTarget.burn()) is False


def test_subtensor_set_weights_signature_has_expected_kwargs():
    import inspect

    import bittensor as bt

    params = inspect.signature(bt.Subtensor.set_weights).parameters
    for name in ("wallet", "netuid", "uids", "weights", "wait_for_inclusion", "wait_for_finalization"):
        assert name in params, f"subtensor.set_weights lost kwarg {name}"
    for name in ("blocks_since_last_update", "weights_rate_limit"):
        assert hasattr(bt.Subtensor, name)


def test_sdk_accepts_plain_lists_for_weights():
    """set_weights normalises through convert_and_normalize_weights_and_uids (lists -> numpy
    first), so torch is not needed. (The lower-level convert_weights_and_uids_for_emit alone
    would reject lists.)"""
    from bittensor.core.extrinsics import weights as weights_extrinsic
    from bittensor.utils.weight_utils import convert_and_normalize_weights_and_uids

    assert weights_extrinsic.convert_and_normalize_weights_and_uids is convert_and_normalize_weights_and_uids
    out_uids, out_weights = convert_and_normalize_weights_and_uids([0, 2], [0.9, 0.1])
    assert list(out_uids) == [0, 2]
    assert max(out_weights) == 65535 and out_weights[0] > out_weights[1]


def test_status_line_shows_on_chain_and_wanted(monkeypatch):
    wm, subtensor = _manager(rate_limited=True)
    line = wm.status_line(WeightTarget.winner("hk_winner"))
    assert "nothing set since start" in line and "winner UID 2" in line and "rate limit" in line


async def test_status_line_after_set():
    wm, subtensor = _manager()
    await wm.apply(WeightTarget.winner("hk_winner"))
    line = wm.status_line(WeightTarget.winner("hk_winner"))
    assert line.startswith("Weights on chain: winner UID 2") and "10%" in line and "wanted" not in line


def test_info_on_change_logs_repeats_at_debug(caplog):
    import logging
    from validator_utils.logutil import info_on_change
    log = logging.getLogger("t")
    with caplog.at_level(logging.DEBUG, logger="t"):
        for _ in range(3):
            info_on_change(log, "k", "same")
        info_on_change(log, "k", "different")
    levels = [(r.levelname, r.getMessage()) for r in caplog.records]
    assert levels == [("INFO", "same"), ("DEBUG", "same"), ("DEBUG", "same"), ("INFO", "different")]
