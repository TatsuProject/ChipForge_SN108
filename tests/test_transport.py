"""End-to-end axon/dendrite loopback using the real ChipForge handlers.

This is the piece bittensor 11 dropped, and the reason we stay on 10.x. The
test starts a miner-style axon on localhost with the handler/blacklist/
priority functions from neurons/miner.py, then drives it with a
validator-style dendrite through MinerCommunications.
"""
import asyncio

import bittensor as bt
import pytest

from chipforge.protocol import SimpleMessage
from conftest import free_port


class _StubMetagraph:
    def __init__(self, axons):
        self.axons = axons


@pytest.fixture
def miner(temp_wallet):
    from neurons.miner import ChipForgeMiner

    port = free_port()
    axon = bt.Axon(wallet=temp_wallet, ip="127.0.0.1", port=port, external_ip="127.0.0.1", external_port=port)

    # Build a ChipForgeMiner without touching the chain: bypass __init__ and
    # only wire the pieces setup_axon_handlers needs.
    m = ChipForgeMiner.__new__(ChipForgeMiner)
    m.wallet = temp_wallet
    m.axon = axon
    m.current_challenge_id = None
    m.current_github_url = None
    m.setup_axon_handlers()
    axon.start()
    yield m
    axon.stop()


async def _wait_until_listening(axon, timeout=10):
    import aiohttp

    url = f"http://127.0.0.1:{axon.port}/"
    deadline = asyncio.get_event_loop().time() + timeout
    async with aiohttp.ClientSession() as s:
        while asyncio.get_event_loop().time() < deadline:
            try:
                async with s.get(url):
                    return
            except aiohttp.ClientConnectorError:
                await asyncio.sleep(0.1)
    raise RuntimeError("axon never started listening")


async def test_challenge_notification_round_trip(miner, second_wallet):
    await _wait_until_listening(miner.axon)
    dendrite = bt.Dendrite(wallet=second_wallet)
    try:
        synapse = SimpleMessage(message="CHALLENGE_ACTIVE:chal-1:https://github.com/org/repo:2026-09-18T00:00:00+00:00")
        responses = await dendrite.forward(axons=[miner.axon.info()], synapse=synapse, timeout=15)
        assert len(responses) == 1
        resp = responses[0]
        assert resp.dendrite.status_code == 200, (resp.dendrite.status_code, resp.dendrite.status_message)
        assert resp.response == "OK"
        assert miner.current_challenge_id == "chal-1"
    finally:
        await dendrite.aclose_session()


async def test_validator_miner_comms_uses_dendrite(miner, second_wallet):
    """Drive the exact MinerCommunications code path the validator runs."""
    from validator_utils.miner_comms import MinerCommunications

    await _wait_until_listening(miner.axon)
    dendrite = bt.Dendrite(wallet=second_wallet)
    try:
        info = miner.axon.info()
        assert info.is_serving
        comms = MinerCommunications(dendrite, _StubMetagraph([info]))

        got = await comms.notify_miners_challenge_active("chal-2", "https://github.com/org/repo")
        assert got == {0: "OK"}

        got = await comms.notify_miners_batch_complete("batch-7")
        assert got == {0: "OK"}
    finally:
        await dendrite.aclose_session()


async def test_unknown_message_still_acknowledged(miner, second_wallet):
    await _wait_until_listening(miner.axon)
    dendrite = bt.Dendrite(wallet=second_wallet)
    try:
        resp = (await dendrite.forward(axons=[miner.axon.info()], synapse=SimpleMessage(message="PING"), timeout=15))[0]
        assert resp.response == "OK"
    finally:
        await dendrite.aclose_session()


async def test_tampered_signature_rejected(miner, second_wallet):
    """The axon's default verify must still reject a bad dendrite signature."""
    from types import SimpleNamespace

    await _wait_until_listening(miner.axon)
    dendrite = bt.Dendrite(wallet=second_wallet)
    try:
        # Dendrite only needs .ss58_address and .sign() from its keypair.
        dendrite.keypair = SimpleNamespace(ss58_address=second_wallet.hotkey.ss58_address, sign=lambda data: b"\x00" * 64)
        resp = (await dendrite.forward(axons=[miner.axon.info()], synapse=SimpleMessage(message="PING"), timeout=15))[0]
        assert resp.dendrite.status_code != 200
        assert resp.response == ""
    finally:
        await dendrite.aclose_session()
