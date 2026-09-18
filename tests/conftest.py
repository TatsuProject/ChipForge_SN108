"""Shared fixtures for the ChipForge subnet test-suite.

The suite exists mainly to prove that the Bittensor dependency upgrade
(9.x -> 10.x classic SDK) still supports everything the miner, validator
and miner CLI rely on: wallet signing, Synapse serialisation, axon/dendrite
transport, config parsing and weight setting.
"""
import os
import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# validator.py does `from validator_utils import ...` relative to neurons/.
for p in (REPO_ROOT, REPO_ROOT / "neurons"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# Never let a test accidentally pick up the operator's real wallets.
os.environ.setdefault("BT_WALLET_PATH", "/nonexistent")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def temp_wallet(tmp_path):
    """A throw-away, unencrypted wallet living under pytest's tmp dir."""
    import bittensor as bt

    wallet = bt.Wallet(name="cf_test", hotkey="cf_hot", path=str(tmp_path / "wallets"))
    wallet.create_new_coldkey(use_password=False, overwrite=True, suppress=True)
    wallet.create_new_hotkey(use_password=False, overwrite=True, suppress=True)
    return wallet


@pytest.fixture
def second_wallet(tmp_path):
    import bittensor as bt

    wallet = bt.Wallet(name="cf_test2", hotkey="cf_hot2", path=str(tmp_path / "wallets2"))
    wallet.create_new_coldkey(use_password=False, overwrite=True, suppress=True)
    wallet.create_new_hotkey(use_password=False, overwrite=True, suppress=True)
    return wallet
