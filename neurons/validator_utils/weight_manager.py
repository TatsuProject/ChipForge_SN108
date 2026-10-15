#!/usr/bin/env python3
"""
Weight Manager for ChipForge Validator

Every weight decision goes through two steps:

  1. The validator picks a *target*: burn, reward winner X, or pay the record ladder's places
     (WeightTarget).
  2. apply() turns the target into chain weights with ONE policy (build()):
       - the miners get `miner_emission_percentage` (a single winner all of it; ladder places their
         share of it), the rest goes to the burn UID
       - a winner / place whose hotkey is unregistered, the burn UID or has a banned coldkey gets
         nothing: a ladder shares that part among the remaining places, and burns it when none is left
       - burn everything when emissions are banned
     and submits them only when they changed, or when a periodic refresh is due
     (so the validator stays active on chain), and only when the chain's weights
     rate limit allows it. A changed target that is rate limited stays pending and
     is submitted as soon as the window reopens.

The chain call runs in a worker thread so it never blocks the validator's event loop,
and success is read from ExtrinsicResponse.success (the response object itself is
always truthy).
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

BURN_UID = 0


@dataclass(frozen=True)
class WeightTarget:
    """What the validator wants to reward right now."""
    winner_hotkey: Optional[str] = None   # None = burn (unless `places` is set)
    reason: str = ""
    # Record ladder: ((hotkey, share), ...) with shares summing to 1; overrides winner_hotkey
    places: Tuple[Tuple[str, float], ...] = ()

    @classmethod
    def burn(cls, reason: str = "") -> "WeightTarget":
        return cls(None, reason)

    @classmethod
    def winner(cls, hotkey: str, reason: str = "") -> "WeightTarget":
        return cls(hotkey, reason)

    @classmethod
    def ladder(cls, places: List[Dict], reason: str = "") -> "WeightTarget":
        """From reward_ladder.places(): no place = burn."""
        held = tuple((p["hotkey"], round(float(p["share"]), 9)) for p in places)
        return cls(None, reason, held) if held else cls.burn(reason)

    def describe(self) -> str:
        if self.places:
            return "ladder " + ", ".join(f"{hk[:8]}... {s * 100:.1f}%" for hk, s in self.places)
        return f"winner {self.winner_hotkey[:12]}..." if self.winner_hotkey else "burn"


def weights_vector(uids: List[int], weights: List[float]) -> str:
    """The exact vector sent to set_weights, e.g. "[UID 0: 0.9, UID 4: 0.086207, UID 2: 0.013793] (sum 1)"."""
    pairs = ", ".join(f"UID {u}: {w:g}" for u, w in zip(uids, weights))
    return f"[{pairs}] (sum {round(sum(weights), 6):g})"


def response_succeeded(response) -> bool:
    """bittensor>=10 returns ExtrinsicResponse, which is always truthy (it defines
    __len__ == 2). Read .success; accept plain bools / (success, msg) tuples too."""
    if response is None:
        return False
    if hasattr(response, "success"):
        return bool(response.success)
    if isinstance(response, tuple) and response:
        return bool(response[0])
    return bool(response)


class WeightManager:
    """Builds and submits weights on the blockchain"""

    def __init__(self, wallet, subtensor, metagraph, config,
                 miner_emission_percentage: float = 100.0, refresh_seconds: int = 1200):
        self.wallet = wallet
        self.subtensor = subtensor
        self.metagraph = metagraph
        self.config = config
        self.miner_emission_percentage = max(0.0, min(100.0, float(miner_emission_percentage)))
        self.refresh_seconds = refresh_seconds

        self._last_applied: Optional[Tuple[Tuple[int, ...], Tuple[float, ...]]] = None
        self._last_applied_at: float = 0.0
        self._last_description: Optional[str] = None
        self._last_applied_wall: Optional[float] = None
        self._pending_logged: Optional[Tuple] = None
        # Backoff after a failed submission (unregistered hotkey, RPC down, ...): the same
        # weights are retried after 30s, 60s, ... up to 10 min instead of every loop.
        self._failed_key: Optional[Tuple] = None
        self._failures = 0
        self._retry_at = 0.0

    # ------------------------------------------------------------------
    # Metagraph lookups
    # ------------------------------------------------------------------

    def _get_coldkey_for_uid(self, uid: int) -> Optional[str]:
        """Resolve coldkey for a UID from the metagraph, if available."""
        try:
            coldkeys = getattr(self.metagraph, 'coldkeys', None)
            if coldkeys is not None and 0 <= uid < len(coldkeys):
                return coldkeys[uid]
        except Exception as e:
            logger.error(f"Error resolving coldkey for UID {uid}: {e}")
        return None

    def get_hotkey_uid(self, hotkey: str) -> Optional[int]:
        """Get UID for hotkey on current subnet"""
        try:
            hotkeys = list(getattr(self.metagraph, 'hotkeys', []) or [])
            if hotkey in hotkeys:
                return hotkeys.index(hotkey)
            return None
        except Exception as e:
            logger.error(f"Error getting UID for hotkey: {e}")
            return None

    # ------------------------------------------------------------------
    # Policy
    # ------------------------------------------------------------------

    def build(self, target: WeightTarget, banned_coldkeys: Optional[Set[str]] = None,
              ban_emissions: bool = False) -> Tuple[List[int], List[float], str]:
        """Turn a target into (uids, weights, description). Pure: no chain calls."""
        burn = ([BURN_UID], [1.0])

        if ban_emissions:
            return (*burn, f"burn (emissions banned by challenge server; {target.reason})")
        if target.places:
            return self._build_ladder(target, banned_coldkeys)
        if not target.winner_hotkey:
            return (*burn, f"burn ({target.reason})")

        hotkey = target.winner_hotkey
        uid = self.get_hotkey_uid(hotkey)
        if uid is None:
            return (*burn, f"burn (winner {hotkey[:12]}... not registered on subnet)")
        if uid == BURN_UID:
            return (*burn, f"burn (winner {hotkey[:12]}... is the burn UID)")
        coldkey = self._get_coldkey_for_uid(uid)
        if banned_coldkeys and coldkey and coldkey in banned_coldkeys:
            return (*burn, f"burn (winner UID {uid} coldkey {coldkey[:12]}... is banned)")

        miner_fraction = self.miner_emission_percentage / 100.0
        if miner_fraction >= 1.0:
            return [uid], [1.0], f"winner UID {uid} ({hotkey[:12]}...) 100% ({target.reason})"
        if miner_fraction <= 0.0:
            return (*burn, f"burn (miner emission percentage is 0; winner UID {uid})")
        return (
            [BURN_UID, uid],
            [round(1.0 - miner_fraction, 6), round(miner_fraction, 6)],
            f"winner UID {uid} ({hotkey[:12]}...) {self.miner_emission_percentage:g}%, "
            f"burn {100 - self.miner_emission_percentage:g}% ({target.reason})",
        )

    def payability(self, hotkey: str, banned_coldkeys: Optional[Set[str]] = None) -> Tuple[Optional[int], Optional[str]]:
        """(uid or None, why it cannot be paid or None), for logs."""
        _, why = self._unpayable(hotkey, banned_coldkeys)
        return self.get_hotkey_uid(hotkey), why

    def _unpayable(self, hotkey: str, banned_coldkeys: Optional[Set[str]]) -> Tuple[Optional[int], Optional[str]]:
        """(uid, None) when hotkey can be paid, else (None, why not)."""
        uid = self.get_hotkey_uid(hotkey)
        if uid is None:
            return None, "not registered"
        if uid == BURN_UID:
            return None, "is the burn UID"
        coldkey = self._get_coldkey_for_uid(uid)
        if banned_coldkeys and coldkey and coldkey in banned_coldkeys:
            return None, f"coldkey {coldkey[:12]}... banned"
        return uid, None

    def _build_ladder(self, target: WeightTarget,
                      banned_coldkeys: Optional[Set[str]]) -> Tuple[List[int], List[float], str]:
        paid, skipped = [], []
        for hotkey, share in target.places:
            uid, why = self._unpayable(hotkey, banned_coldkeys)
            if uid is None:
                skipped.append(f"{hotkey[:8]}... {why}")
            else:
                paid.append((uid, hotkey, share))
        note = f"; skipped: {', '.join(skipped)}" if skipped else ""
        if not paid:
            return [BURN_UID], [1.0], f"burn (no payable ladder place{note}; {target.reason})"
        miner_fraction = self.miner_emission_percentage / 100.0
        if miner_fraction <= 0.0:
            return [BURN_UID], [1.0], f"burn (miner emission percentage is 0; ladder {len(paid)} place(s))"
        total = sum(s for _, _, s in paid)                  # skipped places' share goes to the others
        uids, weights = [], []
        if miner_fraction < 1.0:
            uids.append(BURN_UID)
            weights.append(round(1.0 - miner_fraction, 6))
        for uid, _, share in paid:
            uids.append(uid)
            weights.append(round(miner_fraction * share / total, 6))
        places = ", ".join(f"UID {uid} ({hk[:8]}...) {miner_fraction * s / total * 100:.2f}%" for uid, hk, s in paid)
        burn = f", burn {100 - self.miner_emission_percentage:g}%" if miner_fraction < 1.0 else ""
        return uids, weights, f"ladder: {places}{burn}{note} ({target.reason})"

    # ------------------------------------------------------------------
    # Chain
    # ------------------------------------------------------------------

    def _own_uid(self) -> Optional[int]:
        return self.get_hotkey_uid(self.wallet.hotkey.ss58_address)

    def _rate_limit_allows(self) -> bool:
        """True when the chain's weights rate limit allows a new set_weights call.
        If it can't be determined, allow the attempt (the chain enforces it anyway)."""
        try:
            own_uid = self._own_uid()
            if own_uid is None:
                return True
            since = self.subtensor.blocks_since_last_update(netuid=self.config.netuid, uid=own_uid)
            limit = self.subtensor.weights_rate_limit(netuid=self.config.netuid)
            if since is None or limit is None:
                return True
            return int(since) >= int(limit)
        except Exception as e:
            logger.debug(f"Could not read weights rate limit: {e}")
            return True

    def _submit(self, uids: List[int], weights: List[float]):
        return self.subtensor.set_weights(
            wallet=self.wallet,
            netuid=self.config.netuid,
            uids=uids,
            weights=weights,
            wait_for_inclusion=True,
            wait_for_finalization=False,
        )

    async def apply(self, target: WeightTarget, banned_coldkeys: Optional[Set[str]] = None,
                    ban_emissions: bool = False, force: bool = False) -> bool:
        """Submit the weights for `target` if they changed or a refresh is due.
        Returns True when the chain now has these weights (just set, or already set)."""
        uids, weights, description = self.build(target, banned_coldkeys, ban_emissions)
        description = f"{description} | weights {weights_vector(uids, weights)}"
        key = (tuple(uids), tuple(weights))
        changed = key != self._last_applied
        refresh_due = (time.monotonic() - self._last_applied_at) >= self.refresh_seconds

        if not (changed or refresh_due or force):
            return True

        if key == self._failed_key and time.monotonic() < self._retry_at:
            return False

        if not self._rate_limit_allows():
            if changed and self._pending_logged != key:
                logger.info(f"Weights pending (chain rate limit): {description}")
                self._pending_logged = key
            return False

        logger.info(f"Setting weights: {description}")
        try:
            response = await asyncio.to_thread(self._submit, uids, weights)
        except Exception as e:
            self._record_failure(key)
            logger.error(f"set_weights raised: {e} (retry in {self._retry_at - time.monotonic():.0f}s)")
            return False

        if response_succeeded(response):
            self._last_applied = key
            self._last_applied_at = time.monotonic()
            self._pending_logged = None
            self._failed_key, self._failures = None, 0
            self._last_description, self._last_applied_wall = description, time.time()
            logger.info(f"Weights set on chain: {description}")
            return True

        self._record_failure(key)
        message = getattr(response, "message", response)
        logger.error(f"Chain rejected set_weights ({description}): {message} "
                     f"(retry in {self._retry_at - time.monotonic():.0f}s)")
        return False

    def status_line(self, target: WeightTarget, banned_coldkeys: Optional[Set[str]] = None,
                    ban_emissions: bool = False) -> str:
        """One line on what is on chain and what is wanted, for the periodic status log."""
        uids, weights, wanted = self.build(target, banned_coldkeys, ban_emissions)
        if self._last_description is None:
            on_chain = "nothing set since start"
        else:
            ago = int((time.time() - (self._last_applied_wall or time.time())) / 60)
            on_chain = f"{self._last_description}, set {ago} min ago"
        line = f"Weights on chain: {on_chain}"
        if (tuple(uids), tuple(weights)) != self._last_applied:
            reason = "waiting for the chain's rate limit" if not self._rate_limit_allows() else "being submitted"
            line += f" | wanted: {wanted} ({reason})"
        return line

    def _record_failure(self, key: Tuple) -> None:
        self._failures = self._failures + 1 if key == self._failed_key else 1
        self._failed_key = key
        self._retry_at = time.monotonic() + min(600, 30 * 2 ** (self._failures - 1))
