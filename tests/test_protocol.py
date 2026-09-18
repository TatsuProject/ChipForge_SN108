"""SimpleMessage synapse behaviour under the upgraded SDK."""
import bittensor as bt

from chipforge.protocol import SimpleMessage


def test_simple_message_is_a_synapse():
    assert issubclass(SimpleMessage, bt.Synapse)
    s = SimpleMessage()
    assert s.name == "SimpleMessage"
    assert s.message == ""
    assert s.response == ""


def test_fields_round_trip_through_model_dump():
    s = SimpleMessage(message="CHALLENGE_ACTIVE:c1:https://github.com/x/y:2026-01-01T00:00:00+00:00")
    s.response = "OK"
    data = s.model_dump()
    assert data["message"].startswith("CHALLENGE_ACTIVE:")
    assert data["response"] == "OK"
    clone = SimpleMessage(**data)
    assert clone.message == s.message and clone.response == "OK"


def test_headers_round_trip():
    """Dendrite -> axon transport ships synapse metadata as HTTP headers."""
    s = SimpleMessage(message="BATCH_COMPLETE:b1:ts")
    headers = s.to_headers()
    assert headers["name"] == "SimpleMessage"
    rebuilt = SimpleMessage.from_headers(headers)
    assert rebuilt.name == "SimpleMessage"


def test_deserialize_returns_self():
    s = SimpleMessage(message="x")
    assert s.deserialize() is s


def test_dendrite_and_axon_fields_still_exist():
    s = SimpleMessage()
    # Built-in terminal info the validator/miner code relies on.
    assert hasattr(s, "dendrite") and hasattr(s, "axon")
    assert s.dendrite is None or hasattr(s.dendrite, "hotkey")
