"""Sanity checks on the installed dependency set.

These pin down the *intent* of the upgrade: we are on the bittensor 10.x
classic SDK (networking layer present), not 9.x and not 11.x.
"""
import importlib
import importlib.metadata as md

import pytest


def _version(dist: str):
    from packaging.version import Version

    return Version(md.version(dist))


def test_bittensor_is_10x_classic_sdk():
    v = _version("bittensor")
    assert v.major == 10, f"expected bittensor 10.x, got {v}"


def test_bittensor_networking_layer_present():
    import bittensor as bt

    # These are exactly the names bittensor 11 removed.
    for name in ("Synapse", "Axon", "Dendrite", "Subtensor", "Wallet", "Config", "Metagraph", "AxonInfo", "logging"):
        assert hasattr(bt, name), f"bittensor.{name} missing - networking layer not available"


@pytest.mark.parametrize(
    "dist, minimum",
    [
        ("bittensor-wallet", "4.1.0"),
        ("bittensor-cli", "9.23.0"),
        ("bittensor-drand", "2.0.0"),
        ("async-substrate-interface", "2.2.0"),
        ("bt-decode", "0.8.0"),
    ],
)
def test_companion_packages_upgraded(dist, minimum):
    from packaging.version import Version

    assert _version(dist) >= Version(minimum)


def test_wallet_version_compatible_with_sdk_and_cli():
    """bittensor 10.5 wants wallet>=4.1.0, btcli 9.23 wants ==4.1.0."""
    from packaging.requirements import Requirement

    wallet_v = _version("bittensor-wallet")
    for dist in ("bittensor", "bittensor-cli"):
        for req_str in md.requires(dist) or []:
            req = Requirement(req_str)
            if req.name.lower().replace("_", "-") == "bittensor-wallet" and not req.marker:
                assert wallet_v in req.specifier, f"{dist} requires {req}, have {wallet_v}"


def test_scalecodec_comes_from_cyscale_not_legacy_pin():
    """async-substrate-interface 2.x uses cyscale, which provides the
    `scalecodec` module. A separate scalecodec==1.2.11 pin would overwrite it."""
    scalecodec = importlib.import_module("scalecodec")
    assert scalecodec is not None
    md.version("cyscale")  # raises PackageNotFoundError if missing
    try:
        legacy = md.version("scalecodec")
    except md.PackageNotFoundError:
        legacy = None
    assert legacy is None, f"legacy scalecodec {legacy} is installed alongside cyscale"


def test_app_imports():
    """Every module the neurons and CLI import must resolve in this env."""
    for mod in ("torch", "aiohttp", "aiofiles", "requests", "dotenv", "cryptography.hazmat.primitives.asymmetric.ed25519"):
        importlib.import_module(mod)


def test_repo_modules_import():
    import chipforge.protocol  # noqa: F401
    import validator_utils  # noqa: F401  (neurons/ is on sys.path via conftest)
