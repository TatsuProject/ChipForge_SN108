"""Docker healthcheck: the heartbeat file is fresh only while the event loop runs."""
import asyncio
import os
import time

from chipforge import heartbeat


async def test_heartbeat_loop_writes_and_check_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIPFORGE_DATA_DIR", str(tmp_path))
    stop = asyncio.Event()
    task = asyncio.create_task(heartbeat.heartbeat_loop("validator", stop, interval=0.05))
    await asyncio.sleep(0.1)
    stop.set()
    await task
    assert (tmp_path / "heartbeat-validator").exists()
    assert heartbeat.main(["validator", "--max-age", "60"]) == 0


def test_missing_or_stale_heartbeat_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIPFORGE_DATA_DIR", str(tmp_path))
    assert heartbeat.main(["miner"]) == 1
    heartbeat.beat("miner")
    old = time.time() - 600
    os.utime(tmp_path / "heartbeat-miner", (old, old))
    assert heartbeat.main(["miner", "--max-age", "180"]) == 1
