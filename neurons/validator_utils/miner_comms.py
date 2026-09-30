#!/usr/bin/env python3
"""
Miner Communications for ChipForge Validator
Broadcasts short SimpleMessage notices to serving miners (challenge active, batch complete).
"""

import logging
import os
from datetime import datetime, timezone
from typing import Dict

from chipforge.protocol import SimpleMessage

logger = logging.getLogger(__name__)


class MinerCommunications:
    """Sends notices to miners' axons"""

    def __init__(self, dendrite, metagraph):
        self.dendrite = dendrite
        self.metagraph = metagraph
        self.timeout = float(os.getenv("MINER_NOTIFY_TIMEOUT", "12"))

    async def _broadcast(self, message: str, label: str) -> Dict[int, str]:
        """Send `message` to every serving axon; returns {uid: response} for miners that answered."""
        try:
            serving = [(uid, axon) for uid, axon in enumerate(self.metagraph.axons) if axon.is_serving]
            if not serving:
                logger.warning("No serving miners found")
                return {}

            synapse = SimpleMessage()
            synapse.message = message
            logger.info(f"Notifying {len(serving)} miners: {label}")
            responses = await self.dendrite.forward(
                axons=[axon for _, axon in serving], synapse=synapse, timeout=self.timeout
            )

            answered = {}
            for (uid, _), response in zip(serving, responses):
                if getattr(response, 'response', None):
                    answered[uid] = response.response
                else:
                    logger.debug(f"Miner {uid} did not respond to {label}")
            logger.info(f"{len(answered)}/{len(serving)} miners acknowledged {label}")
            return answered
        except Exception as e:
            logger.error(f"Error notifying miners ({label}): {e}")
            return {}

    async def notify_miners_challenge_active(self, challenge_id: str, github_url: str) -> Dict[int, str]:
        timestamp = datetime.now(timezone.utc).isoformat()
        return await self._broadcast(f"CHALLENGE_ACTIVE:{challenge_id}:{github_url}:{timestamp}",
                                     f"challenge {challenge_id} active")

    async def notify_miners_batch_complete(self, batch_id: str = None) -> Dict[int, str]:
        timestamp = datetime.now(timezone.utc).isoformat()
        return await self._broadcast(f"BATCH_COMPLETE:{batch_id or 'unknown'}:{timestamp}",
                                     f"batch {batch_id} complete")
