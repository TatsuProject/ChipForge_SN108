#!/usr/bin/env python3
# neurons/validator_utils/reward_ladder.py
"""Record ladder: who this validator pays, and how much.

The challenge server sends the ladder rules and every winner record that is in probation or
being paid (/validator/sync -> reward_ladder). Each record has a fixed window:

    qualified_at ── probation ──> pays_from ── paid ──> pays_until

At any moment the places go to the records inside their paid window: one per hotkey (its newest
record), newest first, at most `size`; place i gets decay_ratio^(i-1), normalised over the places
held. WeightManager then gives each place its share of the miners' emission
(miner_emission_percentage; the rest is burned) and drops places whose hotkey is unregistered or
banned, sharing their part among the others. No place = burn.

The validator computes the places on its own clock from the last schedule it received (kept on
disk), so probation ends and windows close on time even when the server is unreachable or the
validator restarts. `shares`, `record_state` and `places` must stay identical to the server's
copy (chipforge_challenge_server app/services/ladder.py).

A server that sends no reward_ladder (older version) leaves the ladder inactive and the
validator falls back to rewarding its own challenge winner.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .storage import atomic_write_json, data_path, load_json

logger = logging.getLogger(__name__)


def _time(value) -> Optional[datetime]:
    if value is None:
        return None
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Pure computation (identical to the server's app/services/ladder.py)
# ---------------------------------------------------------------------------

def shares(count: int, ratio: float) -> List[float]:
    """Share of each of `count` places: ratio^(i-1), normalised to sum to 1."""
    if count <= 0:
        return []
    weights = [float(ratio) ** i for i in range(count)]
    total = sum(weights)
    return [w / total for w in weights]


def record_state(record: Dict, now: datetime) -> str:
    """probation | paying | finished"""
    if now < _time(record["pays_from"]):
        return "probation"
    if now < _time(record["pays_until"]):
        return "paying"
    return "finished"


def places(records: List[Dict], now: datetime, size: int, ratio: float) -> List[Dict]:
    """The ladder at `now`: [{place, share, **record}] for the records holding a place."""
    paying = [r for r in records if record_state(r, now) == "paying"]
    paying.sort(key=lambda r: (_time(r["qualified_at"]), float(r.get("score") or 0)), reverse=True)
    held, seen = [], set()
    for r in paying:
        if r["hotkey"] in seen:
            continue
        seen.add(r["hotkey"])
        held.append(r)
        if len(held) >= max(1, int(size)):
            break
    return [{"place": i + 1, "share": s, **r} for i, (r, s) in enumerate(zip(held, shares(len(held), ratio)))]


# ---------------------------------------------------------------------------
# The schedule this validator follows
# ---------------------------------------------------------------------------

class RewardLadder:
    """Last reward_ladder received from the server, kept on disk."""

    def __init__(self, state_file: str = "reward_ladder.json"):
        self.state_file = data_path(state_file)
        self.schedule: Optional[Dict] = None
        data = load_json(self.state_file)
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            self.schedule = data
            logger.info(f"Reward ladder loaded from disk: {len(data['records'])} record(s), "
                        f"size {data.get('size')}, ratio {data.get('decay_ratio')}")

    @property
    def active(self) -> bool:
        """True once a server that runs the ladder has sent a schedule (now or before a restart)."""
        return self.schedule is not None

    def update(self, schedule: Optional[Dict]) -> bool:
        """Adopt the server's schedule. Returns True when it changed."""
        if not isinstance(schedule, dict) or not isinstance(schedule.get("records"), list):
            return False
        try:
            for r in schedule["records"]:              # reject a malformed schedule as a whole
                _time(r["qualified_at"]), _time(r["pays_from"]), _time(r["pays_until"]), r["hotkey"]
            int(schedule.get("size", 3)), float(schedule.get("decay_ratio", 0.6))
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Ignoring malformed reward_ladder from server: {e}")
            return False
        if schedule == self.schedule:
            return False
        old = {r.get("submission_id") for r in (self.schedule or {}).get("records", [])}
        new = {r.get("submission_id") for r in schedule["records"]}
        logger.info(f"Reward ladder updated: size {schedule.get('size')}, ratio {schedule.get('decay_ratio')}, "
                    f"{len(new)} record(s) in probation or paying"
                    + (f" (+{len(new - old)} / -{len(old - new)})" if self.schedule is not None else ""))
        self.schedule = schedule
        atomic_write_json(self.state_file, schedule)
        return True

    def clear(self) -> None:
        self.schedule = None
        try:
            self.state_file.unlink()
        except FileNotFoundError:
            pass

    def places(self, now: Optional[datetime] = None) -> List[Dict]:
        if not self.schedule:
            return []
        now = now or datetime.now(timezone.utc)
        return places(self.schedule["records"], now, int(self.schedule.get("size", 3)),
                      float(self.schedule.get("decay_ratio", 0.6)))

    def in_probation(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        return sum(record_state(r, now) == "probation" for r in (self.schedule or {}).get("records", []))
