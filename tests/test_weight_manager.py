"""WeightManager against a fake subtensor, plus the SDK-side check that
bittensor 10 still accepts the torch tensors WeightManager passes to
subtensor.set_weights."""
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import torch

from validator_utils.weight_manager import WeightManager


def _fake_metagraph(hotkeys, coldkeys):
    neurons = [SimpleNamespace(hotkey=h, coldkey=c) for h, c in zip(hotkeys, coldkeys)]
    return SimpleNamespace(neurons=neurons, hotkeys=list(hotkeys), coldkeys=list(coldkeys))


def _manager(hotkeys, coldkeys):
    subtensor = MagicMock()
    subtensor.set_weights.return_value = True
    wm = WeightManager(wallet=MagicMock(), subtensor=subtensor, metagraph=_fake_metagraph(hotkeys, coldkeys), config=SimpleNamespace(netuid=84))
    return wm, subtensor


def test_set_winner_weights_splits_between_burn_and_winner():
    wm, subtensor = _manager(["hk0", "hk1", "hk2"], ["ck0", "ck1", "ck2"])
    assert wm.set_winner_weights("hk2", miner_emission_percentage=10.0)
    kwargs = subtensor.set_weights.call_args.kwargs
    assert kwargs["netuid"] == 84 and kwargs["wait_for_inclusion"] is True
    assert kwargs["uids"].tolist() == [0, 2]
    assert np.allclose(kwargs["weights"].tolist(), [0.9, 0.1])


def test_banned_winner_burns_everything():
    wm, subtensor = _manager(["hk0", "hk1"], ["ck0", "ck1"])
    assert wm.set_winner_weights("hk1", 50.0, banned_coldkeys={"ck1"}) is False
    kwargs = subtensor.set_weights.call_args.kwargs
    assert kwargs["uids"].tolist() == [0, 1]
    assert kwargs["weights"].tolist() == [1.0, 0.0]


def test_sdk_accepts_torch_tensors_for_weights():
    """WeightManager hands torch int64/float32 tensors to set_weights. Make sure
    the 10.x weight-conversion helpers still normalise them correctly."""
    from bittensor.utils.weight_utils import convert_weights_and_uids_for_emit

    uids = torch.tensor([0, 2], dtype=torch.int64)
    weights = torch.tensor([0.9, 0.1], dtype=torch.float32)
    out_uids, out_weights = convert_weights_and_uids_for_emit(uids, weights)
    assert list(out_uids) == [0, 2]
    assert len(out_weights) == 2
    # u16-normalised: largest weight maps to 65535
    assert max(out_weights) == 65535
    assert out_weights[0] > out_weights[1]


def test_subtensor_set_weights_signature_has_expected_kwargs():
    import inspect

    import bittensor as bt

    params = inspect.signature(bt.Subtensor.set_weights).parameters
    for name in ("wallet", "netuid", "uids", "weights", "wait_for_inclusion"):
        assert name in params, f"subtensor.set_weights lost kwarg {name}"
