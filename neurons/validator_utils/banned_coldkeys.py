#!/usr/bin/env python3
# neurons/validator_utils/banned_coldkeys.py
"""
Banned Coldkeys Manager for ChipForge Validator

Maintains the list of coldkeys that should be excluded from emissions.
Permanent bans apply across all challenges; challenge-scoped bans apply only
to a specific challenge_id. Persisted to its own JSON file so a transient
server outage does not wipe the ban list.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from .storage import atomic_write_json, data_path, load_json

logger = logging.getLogger(__name__)


class BannedColdkeysManager:
    """Manages the banned-coldkey list and its persistence."""

    def __init__(self, file_path: str = "banned_coldkeys.json"):
        self.file_path = data_path(file_path)
        # coldkey -> {"reason": ..., "banned_at": ...}
        self.permanent: Dict[str, Dict] = {}
        # challenge_id -> {coldkey -> {"reason": ..., "banned_at": ...}}
        self.challenge_scoped: Dict[str, Dict[str, Dict]] = {}
        # challenge_id -> ISO timestamp of last successful sync
        self.synced_at: Dict[str, str] = {}
        self.load()

    def load(self) -> None:
        try:
            data = load_json(self.file_path)
            if data is None:
                return
            self.permanent = data.get("permanent", {}) or {}
            self.challenge_scoped = data.get("challenge_scoped", {}) or {}
            self.synced_at = data.get("synced_at", {}) or {}
            logger.info(
                f"Loaded banned coldkeys: {len(self.permanent)} permanent, "
                f"{sum(len(v) for v in self.challenge_scoped.values())} challenge-scoped "
                f"across {len(self.challenge_scoped)} challenges"
            )
        except Exception as e:
            logger.error(f"Error loading banned coldkeys file {self.file_path}: {e}")

    def save(self) -> None:
        try:
            data = {
                "permanent": self.permanent,
                "challenge_scoped": self.challenge_scoped,
                "synced_at": self.synced_at,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            atomic_write_json(self.file_path, data)
        except Exception as e:
            logger.error(f"Error saving banned coldkeys file {self.file_path}: {e}")

    def update_from_response(self, challenge_id: str, response: Dict) -> None:
        """Replace ban records derived from a fresh server response.

        The server returns permanent bans + challenge-scoped bans for the
        requested challenge_id. We rebuild the permanent set from this response
        (server is the source of truth for unbans) and overwrite the scoped
        bucket for this specific challenge.
        """
        if not isinstance(response, dict):
            logger.warning(f"Banned coldkeys response is not a dict: {type(response)}")
            return

        bans = response.get("bans", []) or []
        new_permanent: Dict[str, Dict] = {}
        new_scoped: Dict[str, Dict] = {}

        for entry in bans:
            coldkey = entry.get("coldkey")
            if not coldkey:
                continue
            info = {
                "reason": entry.get("reason", ""),
                "banned_at": entry.get("banned_at", ""),
            }
            scope = entry.get("scope", "permanent")
            if scope == "permanent":
                new_permanent[coldkey] = info
            elif scope == "challenge":
                new_scoped[coldkey] = info
            else:
                logger.warning(f"Unknown ban scope '{scope}' for coldkey {coldkey[:12]}...")

        self.permanent = new_permanent
        self.challenge_scoped[challenge_id] = new_scoped
        self.synced_at[challenge_id] = datetime.now(timezone.utc).isoformat()

        logger.info(
            f"Synced banned coldkeys for {challenge_id}: "
            f"{len(new_permanent)} permanent, {len(new_scoped)} challenge-scoped"
        )
        self.save()

    def get_active_ban_set(self, current_challenge_id: Optional[str]) -> Set[str]:
        """Return the set of coldkeys that should be denied emissions right now.

        Always includes permanent bans; includes challenge-scoped bans only
        for the active challenge.
        """
        banned = set(self.permanent.keys())
        if current_challenge_id and current_challenge_id in self.challenge_scoped:
            banned.update(self.challenge_scoped[current_challenge_id].keys())
        return banned

    def is_banned(
        self, coldkey: str, current_challenge_id: Optional[str] = None
    ) -> Tuple[bool, str]:
        """Return (banned, scope) for a coldkey. scope is 'permanent', 'challenge', or ''."""
        if coldkey in self.permanent:
            return True, "permanent"
        if (
            current_challenge_id
            and current_challenge_id in self.challenge_scoped
            and coldkey in self.challenge_scoped[current_challenge_id]
        ):
            return True, "challenge"
        return False, ""

    def last_sync_age_seconds(self, challenge_id: str) -> Optional[float]:
        """Seconds since last successful sync for a challenge, or None if never."""
        ts = self.synced_at.get(challenge_id)
        if not ts:
            return None
        try:
            synced = datetime.fromisoformat(ts)
            return (datetime.now(timezone.utc) - synced).total_seconds()
        except Exception:
            return None
