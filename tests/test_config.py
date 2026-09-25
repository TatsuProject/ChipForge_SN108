"""Argument parsing for the miner, validator and miner CLI entry points.

bittensor 10 turned CLI parsing OFF by default (BT_NO_PARSE_CLI_ARGS="true"),
so bt.Config(parser) returns only defaults unless the process opts back in.
These tests call the real get_config() functions to prove the neurons do.
"""
import sys

import bittensor as bt
import pytest


@pytest.fixture(autouse=True)
def _clean_parse_flag(monkeypatch):
    # Prove the neurons work without the operator pre-setting the flag.
    monkeypatch.delenv("BT_NO_PARSE_CLI_ARGS", raising=False)


def test_validator_get_config_parses_cli(monkeypatch, tmp_path):
    from neurons.validator import get_config

    monkeypatch.setattr(sys, "argv", [
        "validator.py",
        "--netuid", "84",
        "--subtensor.network", "finney",
        "--wallet.name", "/some/path/chipforge_validator",
        "--wallet.hotkey", "vhot",
        "--wallet.path", str(tmp_path),
        "--challenge_api_url", "http://cs:8000",
        "--validator_secret_key", "sekrit",
        "--logging.debug",
    ])
    config = get_config()
    assert config.netuid == 84
    assert config.subtensor.network == "finney"
    assert config.wallet.name == "/some/path/chipforge_validator"
    assert config.wallet.hotkey == "vhot"
    assert config.challenge_api_url == "http://cs:8000"
    assert config.validator_secret_key == "sekrit"
    assert config.miner_emission_percentage == 10.0
    assert config.logging.debug is True
    assert config.is_set("netuid") and config.is_set("wallet.name")

    wallet = bt.Wallet(config=config)
    assert wallet.name == "/some/path/chipforge_validator"
    assert wallet.hotkey_str == "vhot"
    assert str(tmp_path) in str(wallet.path)


def test_miner_get_config_parses_cli(monkeypatch):
    from neurons.miner import get_config

    monkeypatch.setattr(sys, "argv", [
        "miner.py", "--netuid", "84", "--subtensor.network", "test",
        "--wallet.name", "m", "--wallet.hotkey", "mh", "--axon.port", "9001",
        "--challenge_api_url", "http://cs:8000",
    ])
    config = get_config()
    assert config.netuid == 84
    assert config.subtensor.network == "test"
    assert config.wallet.name == "m" and config.wallet.hotkey == "mh"
    assert config.axon.port == 9001
    assert config.challenge_api_url == "http://cs:8000"


def test_bare_bt_config_ignores_cli_by_default(monkeypatch):
    """Documents the 10.x behaviour the fix in get_config() works around.
    If this starts failing, bittensor changed the default and the env
    workaround can be dropped."""
    import argparse

    monkeypatch.setattr(sys, "argv", ["prog", "--netuid", "84"])
    parser = argparse.ArgumentParser()
    parser.add_argument("--netuid", type=int)
    assert bt.Config(parser).netuid is None
    monkeypatch.setenv("BT_NO_PARSE_CLI_ARGS", "false")
    assert bt.Config(parser).netuid == 84


def test_operator_override_of_parse_flag_is_respected(monkeypatch):
    from neurons.miner import get_config

    monkeypatch.setenv("BT_NO_PARSE_CLI_ARGS", "true")
    monkeypatch.setattr(sys, "argv", ["miner.py", "--netuid", "84"])
    assert get_config().netuid is None


def test_miner_cli_duck_typed_config_still_accepted_by_wallet():
    """miner_cli.main builds a plain object with .wallet.name/.wallet.hotkey
    (no .wallet.path) and hands it to bt.Wallet(config=...). bittensor-wallet
    4.1 must keep accepting that shape."""
    config = type("Config", (), {
        "wallet": type("Wallet", (), {"name": "cf_cli", "hotkey": "cf_cli_hot"})(),
        "api_url": "http://x:1",
    })()
    wallet = bt.Wallet(config=config)
    assert wallet.name == "cf_cli"
    assert wallet.hotkey_str == "cf_cli_hot"


def test_miner_cli_parser_accepts_documented_invocation(monkeypatch):
    """`miner_cli.py --wallet.name w --wallet.hotkey h --api_url u submit f.zip --check_status`
    is what submit_solution.sh runs."""
    from python_scripts import miner_cli

    captured = {}

    class FakeCLI:
        def __init__(self, config):
            captured["config"] = config

        def submit_solution(self, **kw):
            captured["submit"] = kw

    monkeypatch.setattr(miner_cli, "MinerCLI", FakeCLI)
    monkeypatch.setattr(sys, "argv", ["miner_cli.py", "--wallet.name", "w", "--wallet.hotkey", "h", "--api_url", "http://x:1", "submit", "sol.zip", "--check_status"])
    miner_cli.main()
    cfg = captured["config"]
    assert cfg.wallet.name == "w" and cfg.wallet.hotkey == "h" and cfg.api_url == "http://x:1"
    assert captured["submit"] == {"solution_file": "sol.zip", "challenge_id": None, "check_status": True, "dry_run": False}
