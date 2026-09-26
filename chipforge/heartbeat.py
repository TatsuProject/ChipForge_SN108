"""Liveness file for container healthchecks.

A running neuron rewrites <CHIPFORGE_DATA_DIR>/heartbeat-<name> every HEARTBEAT_SECONDS from
an asyncio task, so the file only stays fresh while the event loop is actually running
(a blocked or wedged loop lets it go stale). `python -m chipforge.heartbeat <name>` exits
non-zero when the file is missing or older than --max-age seconds.
"""
import argparse
import asyncio
import logging
import os
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def heartbeat_path(name: str) -> Path:
    return Path(os.getenv("CHIPFORGE_DATA_DIR", ".")).expanduser() / f"heartbeat-{name}"


def beat(name: str) -> None:
    path = heartbeat_path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(str(int(time.time())))
        os.replace(tmp, path)
    except OSError as e:
        logger.debug(f"heartbeat write failed: {e}")


async def heartbeat_loop(name: str, stop: asyncio.Event, interval: float = None) -> None:
    interval = interval or float(os.getenv("HEARTBEAT_SECONDS", "30"))
    while not stop.is_set():
        beat(name)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


def age_seconds(name: str) -> float:
    try:
        return time.time() - heartbeat_path(name).stat().st_mtime
    except OSError:
        return float("inf")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Exit 0 if the neuron's heartbeat is fresh")
    parser.add_argument("name", choices=["validator", "miner"])
    parser.add_argument("--max-age", type=float, default=180)
    args = parser.parse_args(argv)
    age = age_seconds(args.name)
    if age > args.max_age:
        print(f"{args.name} heartbeat stale ({age:.0f}s > {args.max_age:.0f}s)")
        return 1
    print(f"{args.name} alive ({age:.0f}s ago)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
