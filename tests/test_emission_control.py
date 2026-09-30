"""The challenge server's MINER_EMISSION_PERCENTAGE is authoritative when it sends one;
otherwise the validator's own setting applies."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from bittensor.core.types import ExtrinsicResponse

from validator_utils.api_client import APIClient
from validator_utils.weight_manager import WeightManager, WeightTarget

HOTKEYS = ["hk_owner", "hk_validator", "hk_winner"]


def make_validator(local=10.0):
    from neurons.validator import ChipForgeValidator
    subtensor = MagicMock()
    subtensor.set_weights.return_value = ExtrinsicResponse(True, "ok")
    subtensor.blocks_since_last_update.return_value = 500
    subtensor.weights_rate_limit.return_value = 100
    wallet = MagicMock()
    wallet.hotkey.ss58_address = "hk_validator"
    metagraph = SimpleNamespace(hotkeys=list(HOTKEYS), coldkeys=["c0", "c1", "c2"])
    v = ChipForgeValidator.__new__(ChipForgeValidator)
    v.config = SimpleNamespace(miner_emission_percentage=local, netuid=108)
    v.weight_manager = WeightManager(wallet, subtensor, metagraph, v.config, miner_emission_percentage=local)
    v.emission_manager = SimpleNamespace(miner_emission_percentage=local)
    return v, subtensor


def test_server_value_is_authoritative_and_changes_weights():
    v, subtensor = make_validator(local=10.0)
    assert v.weight_manager.build(WeightTarget.winner("hk_winner"))[1] == [0.9, 0.1]
    assert v.apply_emission_percentage(True, 30) == 30.0
    assert v.weight_manager.build(WeightTarget.winner("hk_winner"))[1] == [0.7, 0.3]
    assert v.emission_manager.miner_emission_percentage == 30.0


def test_unset_or_missing_on_server_falls_back_to_local():
    v, _ = make_validator(local=10.0)
    v.apply_emission_percentage(True, 30)
    assert v.apply_emission_percentage(True, None) == 10.0          # server unset it again
    assert v.apply_emission_percentage(False, None) == 10.0         # older server: field absent


def test_invalid_or_out_of_range_server_values():
    v, _ = make_validator(local=10.0)
    assert v.apply_emission_percentage(True, "abc") == 10.0
    assert v.apply_emission_percentage(True, 250) == 100.0
    assert v.apply_emission_percentage(True, -5) == 0.0


async def test_change_is_put_on_chain_at_the_next_weight_set():
    v, subtensor = make_validator(local=10.0)
    await v.weight_manager.apply(WeightTarget.winner("hk_winner"))
    v.apply_emission_percentage(True, 50)
    await v.weight_manager.apply(WeightTarget.winner("hk_winner"))
    calls = [c.kwargs["weights"] for c in subtensor.set_weights.call_args_list]
    assert calls == [[0.9, 0.1], [0.5, 0.5]]


def test_api_client_reads_it_from_sync_or_challenge(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIPFORGE_DATA_DIR", str(tmp_path))
    client = APIClient(SimpleNamespace(challenge_api_url="https://cs", validator_secret_key="k"), MagicMock(), None)
    assert client.server_miner_emission_percentage() == (False, None)
    assert client.server_miner_emission_percentage({"challenge_id": "c1", "miner_emission_percentage": 15}) == (True, 15)
    client._sync_state = {"challenge": None, "miner_emission_percentage": 20}
    assert client.server_miner_emission_percentage() == (True, 20)       # also between challenges
