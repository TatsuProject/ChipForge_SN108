"""Crash-safe state files: atomic writes, corrupt files moved aside, CHIPFORGE_DATA_DIR."""
import json
import os

import pytest

from validator_utils import storage


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIPFORGE_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


def test_atomic_write_leaves_old_file_when_serialisation_fails(data_dir):
    path = storage.data_path("state.json")
    storage.atomic_write_json(path, {"winner": "hk1"})
    with pytest.raises(TypeError):
        storage.atomic_write_json(path, {"bad": object()})     # not JSON-serialisable mid-write
    assert json.loads(path.read_text()) == {"winner": "hk1"}
    assert [p.name for p in data_dir.iterdir()] == ["state.json"]  # no temp files left


def test_corrupt_file_is_moved_aside_not_overwritten(data_dir):
    path = storage.data_path("state.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"winner": "hk1", "trunc')
    assert storage.load_json(path) is None
    assert not path.exists()
    assert any(p.name.startswith("state.json.corrupt-") for p in data_dir.iterdir())


def test_state_files_live_in_data_dir(data_dir):
    from validator_utils.emission_manager import EmissionManager
    from validator_utils.validator_state import ValidatorState

    state = ValidatorState()
    state.last_challenge_id = "ch1"
    state.save_state()
    em = EmissionManager(miner_emission_percentage=10)
    em.update_winner("hk_w", 42.0, 10.0)

    assert (data_dir / "validator_state.json").exists()
    assert (data_dir / "emission_state.json").exists()
    assert ValidatorState().last_challenge_id == "ch1"
    assert EmissionManager().current_winner == "hk_w"


def test_evaluated_batches_keep_the_most_recent(data_dir):
    from validator_utils.validator_state import ValidatorState

    state = ValidatorState()
    for i in range(10):
        state.mark_batch_evaluated(f"b{i}", keep=3)
    assert list(state.evaluated_batches) == ["b7", "b8", "b9"]
    state.save_state()
    assert list(ValidatorState().evaluated_batches) == ["b7", "b8", "b9"]
