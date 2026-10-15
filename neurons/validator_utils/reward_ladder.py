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
from typing import Callable, Dict, List, Optional, Tuple

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


def _in(seconds: float) -> str:
    m = max(0, int(seconds // 60))
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def report(records: List[Dict], now: datetime, size: int, ratio: float,
           payable: Callable[[str], Tuple[Optional[int], Optional[str]]],
           miner_percentage: float = 100.0) -> Tuple[tuple, List[str]]:
    """The ladder as log lines, and a signature that changes when the lines would say something new
    (a record changes state or place, becomes unpayable, or the miners' percentage changes).
    payable(hotkey) -> (uid, why not paid). Each place shows its ladder share (of the miners'
    percentage) and what it gets of all the validator's weight once unpaid places' share is
    given to the others."""
    held = {p["submission_id"]: p for p in places(records, now, size, ratio)}
    pay = {sid: payable(p["hotkey"]) for sid, p in held.items()}
    paid_total = sum(p["share"] for sid, p in held.items() if not pay[sid][1])
    newest_held = {p["hotkey"] for p in held.values()}
    rows, sig = [], []
    for r in sorted(records, key=lambda r: (_time(r["qualified_at"]), float(r.get("score") or 0)), reverse=True):
        state = record_state(r, now)
        if state == "finished":
            continue
        uid, why = pay[r["submission_id"]] if r["submission_id"] in pay else payable(r["hotkey"])
        who = f"{'UID ' + str(uid) if uid is not None else 'no UID'} {r['hotkey'][:8]}... score {float(r.get('score') or 0):g}"
        p = held.get(r["submission_id"])
        if p:
            total = 0.0 if why or not paid_total else p["share"] / paid_total * miner_percentage
            line = (f"  #{p['place']} {who}: {p['share'] * 100:.1f}% of the miners' share -> {total:.2f}% of all weight, "
                    f"paid until {_time(r['pays_until']):%H:%M} UTC ({_in((_time(r['pays_until']) - now).total_seconds())} left)")
            if why:
                line += f" - NOT PAID ({why}): its share goes to the other places, or is burned if none"
        elif state == "paying":
            line = (f"  -- {who}: in its paid window but holds no place "
                    + ("(the same miner's newer record does)" if r["hotkey"] in newest_held else f"({size} newer records hold them)"))
        else:
            line = (f"  probation {who}: paid from {_time(r['pays_from']):%H:%M} UTC "
                    f"(in {_in((_time(r['pays_from']) - now).total_seconds())})")
            if why:
                line += f" - will not be paid ({why})"
        rows.append(line)
        sig.append((r["submission_id"], state, p["place"] if p else None, why))
    paying = len(held)
    probation = sum(record_state(r, now) == "probation" for r in records)
    if not rows:
        return (), ["Reward ladder: no record in probation or being paid - the miners' share is burned"]
    burned = 100.0 if not paid_total else 100.0 - miner_percentage
    head = (f"Reward ladder ({size} places, ratio {ratio:g}): {paying} paying, {probation} in probation; "
            f"miners get {miner_percentage:g}% of the weight, burn {burned:g}%"
            + ("" if paid_total else " (nothing payable now: the miners' share is burned too)")
            + (" (unpaid places' share goes to the paid ones)" if 0 < paid_total < 0.999999 else ""))
    return tuple(sig) + (miner_percentage,), [head] + rows


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

    def report(self, payable, miner_percentage: float = 100.0, now: Optional[datetime] = None) -> Tuple[tuple, List[str]]:
        now = now or datetime.now(timezone.utc)
        sched = self.schedule or {}
        return report(sched.get("records", []), now, int(sched.get("size", 3)),
                      float(sched.get("decay_ratio", 0.6)), payable, miner_percentage)

    def in_probation(self, now: Optional[datetime] = None) -> int:
        now = now or datetime.now(timezone.utc)
        return sum(record_state(r, now) == "probation" for r in (self.schedule or {}).get("records", []))
