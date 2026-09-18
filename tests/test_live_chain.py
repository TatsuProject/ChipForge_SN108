"""Opt-in checks against a real Bittensor network.

Run with:  CHIPFORGE_LIVE_TESTS=1 CHIPFORGE_LIVE_NETWORK=test CHIPFORGE_LIVE_NETUID=440 pytest -m live
"""
import os

import pytest

pytestmark = pytest.mark.live

if not os.environ.get("CHIPFORGE_LIVE_TESTS"):
    pytest.skip("set CHIPFORGE_LIVE_TESTS=1 to run", allow_module_level=True)

NETWORK = os.environ.get("CHIPFORGE_LIVE_NETWORK", "test")
NETUID = int(os.environ.get("CHIPFORGE_LIVE_NETUID", "440"))


@pytest.fixture(scope="module")
def subtensor():
    import bittensor as bt

    return bt.Subtensor(network=NETWORK)


def test_can_connect_and_read_block(subtensor):
    assert subtensor.block > 0


def test_metagraph_sync_and_attributes(subtensor):
    """Exactly what the miner/validator loop does each cycle."""
    mg = subtensor.metagraph(NETUID)
    mg.sync(subtensor=subtensor)
    assert len(mg.neurons) == len(mg.hotkeys) == len(mg.coldkeys) == len(mg.axons)
    assert all(hasattr(a, "is_serving") for a in mg.axons)
    assert hasattr(mg.neurons[0], "hotkey") if mg.neurons else True
