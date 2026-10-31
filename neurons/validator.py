#!/usr/bin/env python3
# neurons/validator.py
"""
ChipForge Subnet Validator (Refactored)
Main validator entry point with modular architecture
"""
import bittensor as bt

import os
import sys
import signal
import asyncio
import time
import aiohttp
import logging
import traceback
import argparse
from datetime import datetime, timezone

# Import validator utilities
from validator_utils import (
    ValidatorState,
    BatchProcessor,
    WeightManager,
    APIClient,
    MinerCommunications,
    setup_validator_logging,
    BannedColdkeysManager,
)
from chipforge.heartbeat import heartbeat_loop
from validator_utils.weight_manager import WeightTarget
from validator_utils.reward_ladder import RewardLadder
from validator_utils.logutil import info_on_change
from validator_utils.status_reporter import StatusReporter
from validator_utils.evaluator_manager import EvaluatorManager

# How often the validator logs what is on chain and what it wants (seconds)
WEIGHT_STATUS_SECONDS = int(os.getenv("WEIGHT_STATUS_SECONDS", "300"))
from validator_utils.api_client import parse_server_time

# Configure logging with daily rotation
setup_validator_logging(log_level="INFO")
logger = logging.getLogger(__name__)


class ChipForgeValidator:
    """ChipForge Subnet Validator.

    Each cycle: follow the active quest, keep the evaluator the challenge server asks for installed on
    the EDA server (EvaluatorManager), evaluate the designs the server hands out with it, and report what
    it is doing (StatusReporter). Weights always come from the server's record ladder (reward_ladder.py):
    the server decides records from every validator's results."""

    async def sync_banned_coldkeys(self, challenge_id: str) -> None:
        """Fetch banned coldkeys from server and update local cache.

        On failure, the cached state on disk is retained so a transient outage
        does not wipe the ban list.
        """
        if not challenge_id:
            return
        try:
            response = await self.api_client.get_banned_coldkeys(challenge_id)
            if response is not None:
                self.banned_coldkeys.update_from_response(challenge_id, response)
            else:
                age = self.banned_coldkeys.last_sync_age_seconds(challenge_id)
                if age is None:
                    logger.warning(
                        f"Banned coldkeys sync failed for {challenge_id} and no cache exists - proceeding with empty ban list"
                    )
                else:
                    logger.warning(
                        f"Banned coldkeys sync failed for {challenge_id} - using cached list (age: {age/60:.1f} min)"
                    )
        except Exception as e:
            logger.error(f"Unexpected error during banned coldkeys sync: {e}")

    def _active_banned_coldkeys(self):
        """Return the set of banned coldkeys to enforce for this weight cycle."""
        return self.banned_coldkeys.get_active_ban_set(self.state.last_challenge_id)

    def refresh_reward_ladder(self):
        """Adopt the reward_ladder of the last /validator/sync. Kept on disk, so the weights keep following
        it while the server is unreachable. A server reached without one burns."""
        state = self.api_client._sync_state if self.api_client else None
        if state is None:
            return                                   # server not reached yet: keep what is on disk
        if 'reward_ladder' in state:
            self.reward_ladder.update(state['reward_ladder'])
        elif self.reward_ladder.active:
            logger.warning("Challenge server sends no reward_ladder: burning until it does")
            self.reward_ladder.clear()
        # Sent outside the challenge too, so an emissions ban applies between quests
        if 'ban_emissions' in state and bool(state['ban_emissions']) != self.state.ban_emissions:
            logger.warning(f"Emissions ban status changed: {self.state.ban_emissions} -> {bool(state['ban_emissions'])}")
            self.state.ban_emissions = bool(state['ban_emissions'])
            self.state.save_state()

    def effective_target(self) -> WeightTarget:
        """The ladder's places at this moment (burn when there is no ladder)."""
        if not self.reward_ladder.active:
            return WeightTarget.burn("no record ladder from the challenge server")
        held = self.reward_ladder.places()
        waiting = self.reward_ladder.in_probation()
        reason = "record ladder" + (f", {waiting} record(s) in probation" if waiting else "")
        target = WeightTarget.ladder(held, reason if held else f"no record being paid ({reason})")
        if target != self._last_ladder_target:
            logger.debug(f"Weight target: {target.describe()} ({target.reason})")
            self._last_ladder_target = target
        banned = self._active_banned_coldkeys()
        miner_pct = 0.0 if self.state.ban_emissions else self.weight_manager.miner_emission_percentage
        signature, lines = self.reward_ladder.report(lambda hk: self.weight_manager.payability(hk, banned), miner_pct,
                                                     emissions_banned=bool(self.state.ban_emissions))
        if signature != self._last_ladder_report:
            self._last_ladder_report = signature
            for line in lines:
                logger.info(line)
        return target

    async def apply_weights(self):
        """Submit the current target if it changed or a refresh is due (see WeightManager)."""
        try:
            self.refresh_reward_ladder()
            target = self.effective_target()
            await self.weight_manager.apply(
                target, self._active_banned_coldkeys(), self.state.ban_emissions
            )
            now = time.monotonic()
            if now - self._last_weight_status >= WEIGHT_STATUS_SECONDS:
                self._last_weight_status = now
                logger.info(self.weight_manager.status_line(
                    target, self._active_banned_coldkeys(), self.state.ban_emissions))
        except Exception as e:
            logger.error(f"Error applying weights: {e}")

    async def maybe_sync_metagraph(self, force: bool = False):
        """Resync the metagraph at most every METAGRAPH_SYNC_SECONDS, off the event loop."""
        now = datetime.now(timezone.utc)
        if not force and self._last_metagraph_sync and \
                (now - self._last_metagraph_sync).total_seconds() < self.metagraph_sync_seconds:
            return
        try:
            await asyncio.to_thread(self.metagraph.sync, subtensor=self.subtensor)
            self._last_metagraph_sync = now
        except Exception as e:
            logger.error(f"Metagraph sync failed: {e}")

    def _background(self, coro):
        """Run a notification without blocking the loop (reference kept until it finishes)."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def apply_emission_percentage(self, controlled: bool, server_value) -> float:
        """The challenge server's miner emission percentage is authoritative when it sends one;
        otherwise this validator's own MINER_EMISSION_PERCENTAGE applies. A change goes on chain
        with the next weight set (the weights differ, so they are submitted)."""
        local = float(self.config.miner_emission_percentage)
        effective, source = local, "local MINER_EMISSION_PERCENTAGE"
        if controlled and server_value is not None:
            try:
                effective, source = max(0.0, min(100.0, float(server_value))), "challenge server"
            except (TypeError, ValueError):
                logger.error(f"Ignoring invalid miner_emission_percentage from server: {server_value!r}")
        if effective != self.weight_manager.miner_emission_percentage:
            logger.info(f"Miner emission percentage: {self.weight_manager.miner_emission_percentage:g}% -> "
                        f"{effective:g}% ({source})")
            if source == "challenge server" and effective != local:
                logger.warning(f"The challenge server sets the miner emission percentage to {effective:g}%; "
                               f"this validator's MINER_EMISSION_PERCENTAGE={local:g} is not used")
            self.weight_manager.miner_emission_percentage = effective
        return effective

    def request_stop(self):
        logger.info("Shutdown requested")
        self._stop.set()

    def __init__(self, config):
        self.config = config
        self.wallet = bt.Wallet(config=config)
        self.subtensor = bt.Subtensor(config=config)
        self.metagraph = self.subtensor.metagraph(config.netuid)
        self.dendrite = bt.Dendrite(wallet=self.wallet)

        self.state = ValidatorState()
        self.banned_coldkeys = BannedColdkeysManager()
        self.weight_manager = WeightManager(
            self.wallet, self.subtensor, self.metagraph, self.config,
            miner_emission_percentage=config.miner_emission_percentage,
            refresh_seconds=int(os.getenv("WEIGHTS_REFRESH_SECONDS", "1200")),
        )
        # Record ladder from the challenge server (kept on disk): decides the weights
        self.reward_ladder = RewardLadder()
        self._last_ladder_target = None
        self._last_ladder_report = None
        self.metagraph_sync_seconds = int(os.getenv("METAGRAPH_SYNC_SECONDS", "600"))
        self._last_metagraph_sync = datetime.now(timezone.utc)
        self._stop = asyncio.Event()
        self._last_info_check = {}
        self._last_banned_sync = {}
        self._background_tasks = set()
        self._last_weight_status = 0.0

        self.session = None
        # Created with the HTTP session (initialize_components)
        self.api_client = None
        self.batch_processor = None
        self.miner_comms = None
        self.status = None
        self.evaluators = None

        logger.info(f"ChipForge Validator initialized, hotkey {self.wallet.hotkey.ss58_address}")

    def initialize_components(self):
        """Initialize components that require the HTTP session"""
        self.api_client = APIClient(self.config, self.wallet, self.session, state=self.state)
        self.status = StatusReporter(self.api_client)
        self.evaluators = EvaluatorManager(self.api_client, self.status)
        self.batch_processor = BatchProcessor(self.api_client, self.state, self.status)
        self.miner_comms = MinerCommunications(self.dendrite, self.metagraph)

    # ------------------------------------------------------------------
    # Quest
    # ------------------------------------------------------------------

    async def start_challenge(self, challenge: dict):
        """A quest this validator has not worked on yet (or after a restart)."""
        challenge_id = challenge['challenge_id']
        expires = parse_server_time(challenge.get('expires_at'))
        logger.info(f"Quest {challenge_id} active" + (f" until {expires.isoformat()}" if expires else ""))
        if self.state.last_challenge_id != challenge_id and challenge.get('github_url'):
            self._background(self.miner_comms.notify_miners_challenge_active(challenge_id, challenge['github_url']))
        self.state.last_challenge_id = challenge_id
        self.state.current_challenge_expires_at = expires
        self.state.save_state()
        await self.sync_banned_coldkeys(challenge_id)
        self._last_banned_sync[challenge_id] = datetime.now(timezone.utc)

    async def refresh_challenge(self, challenge_id: str):
        """Batch windows and the emissions ban every minute, bans every 10 minutes (all from the cached sync)."""
        now = datetime.now(timezone.utc)
        if (now - self._last_info_check.get(challenge_id, datetime.min.replace(tzinfo=timezone.utc))).total_seconds() > 60:
            self._last_info_check[challenge_id] = now
            info = await self.api_client.get_challenge_info(challenge_id)
            if info:
                self.state.update_batch_windows(info)
                if 'ban_emissions' in info and bool(info['ban_emissions']) != self.state.ban_emissions:
                    logger.warning(f"Emissions ban status changed: {self.state.ban_emissions} -> {info['ban_emissions']}")
                    self.state.ban_emissions = bool(info['ban_emissions'])
                    self.state.save_state()
                # Quest 1 (EDA_API=v1) only: the v2 evaluator is installed by EvaluatorManager
                if self.api_client.needs_testcases and info.get('download_new_testcases'):
                    await self.api_client.download_test_cases(challenge_id)
        if (now - self._last_banned_sync.get(challenge_id, datetime.min.replace(tzinfo=timezone.utc))).total_seconds() > 600:
            await self.sync_banned_coldkeys(challenge_id)
            self._last_banned_sync[challenge_id] = now

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    async def run_evaluation_cycle(self):
        """One pass: quest, evaluator, then at most one batch (split mode: one design)."""
        await self.maybe_sync_metagraph()
        try:
            challenge = await self.api_client.get_active_challenge()
        except ConnectionError as e:
            info_on_change(logger, "server", f"Challenge server unreachable ({e}) - weights keep following the stored ladder")
            return
        info_on_change(logger, "server", "Challenge server reachable")
        self.apply_emission_percentage(*self.api_client.server_miner_emission_percentage(challenge))

        if not isinstance(challenge, dict) or not challenge.get('challenge_id'):
            if self.state.last_challenge_id:
                logger.info(f"Quest {self.state.last_challenge_id} ended")
                self.state.last_challenge_id = None
                self.state.current_challenge_expires_at = None
                self.state.save_state()
            self.status.set("idle", "no active quest")
            return
        challenge_id = challenge['challenge_id']
        if self.state.last_challenge_id != challenge_id:
            await self.start_challenge(challenge)
        await self.refresh_challenge(challenge_id)
        if self.api_client.needs_testcases and not self.api_client.check_testcase_files_exist(challenge_id):
            await self.api_client.download_test_cases(challenge_id)

        # The evaluator the server asks for must be ready on the EDA server before asking for work
        target = (self.api_client._sync_state or {}).get('evaluator')
        eda_up, ready = await self.evaluators.ready_sha(target)
        if not eda_up:
            self.status.set("eda_offline")
            return
        if ready is None:
            if self.status.state not in ("downloading_evaluator", "installing_evaluator", "install_failed"):
                self.status.set("waiting_for_evaluator", f"v{target.get('version')}" if target
                                else "the EDA server has no evaluator ready")
            return

        batch = await self.api_client.get_current_batch(challenge_id, claim=True, evaluator_sha256=ready)
        if not isinstance(batch, dict) or not batch.get('batch_id') or batch['batch_id'] in self.state.evaluated_batches:
            self.status.set("idle")
            return
        batch_id = batch['batch_id']

        # A batch names its evaluator (all mode can still finish an older version's designs)
        evaluator = batch.get('evaluator') or None
        sha = (evaluator or {}).get('sha256') or ready
        if evaluator and sha != ready:
            _, other = await self.evaluators.ready_sha(evaluator)
            if other != sha:
                info_on_change(logger, "batch_evaluator",
                               f"Batch {batch_id} uses {self.evaluators.describe(evaluator)}, not ready here - skipped")
                self.status.set("waiting_for_evaluator", f"v{evaluator.get('version')}")
                return

        # Time budget: the server's deadline for this batch. Results are accepted until results_due_at
        # (the deadline plus the server's grace); the EDA evaluations themselves stop before evaluation_ends_at.
        deadline = parse_server_time(batch.get('results_due_at')) or parse_server_time(batch.get('evaluation_ends_at'))
        if deadline is not None:
            batch_timeout = (deadline - datetime.now(timezone.utc)).total_seconds() - 15
            if batch_timeout < int(os.getenv("MIN_BATCH_SECONDS", "180")):
                info_on_change(logger, "batch_late", f"Batch {batch_id} closes in {batch_timeout:.0f}s - too late to evaluate, skipped")
                return
        else:
            dl, ev = self.state.batch_download_window_seconds, self.state.batch_evaluation_window_seconds
            batch_timeout = (dl + ev - 45) if dl > 0 and ev > 0 else 2700
        logger.info(f"Batch {batch_id}: {len(batch.get('submissions', []))} design(s), "
                    f"{self.evaluators.describe(evaluator) if evaluator else 'evaluator ' + sha[:12]}, "
                    f"{batch_timeout / 60:.0f} min left")

        # Split mode: heartbeats while evaluating, so a validator that dies mid-run is noticed early
        heartbeat = asyncio.create_task(self.api_client.heartbeat_loop(batch_id)) \
            if str(batch_id).startswith("split-") else None
        try:
            success = await asyncio.wait_for(self.batch_processor.process_batch(challenge_id, batch, sha),
                                             timeout=batch_timeout)
        except asyncio.TimeoutError:
            logger.error(f"Batch {batch_id} timed out after {batch_timeout:.0f}s")
            success = False
            # Zero scores with timeout_occurred=true: the miner sees the timeout, and the server allows a
            # later result to replace it
            timeouts = {s['submission_id']: {**self.api_client.build_timeout_evaluation(s['submission_id'], batch_timeout),
                                             'evaluator_sha256': sha}
                        for s in batch.get('submissions', [])}
            if timeouts:
                await self.api_client.submit_all_evaluations(challenge_id, timeouts)
        except Exception as e:
            logger.error(f"Batch {batch_id} failed: {e}")
            logger.error(traceback.format_exc())
            success = False
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
        if success:
            self._background(self.miner_comms.notify_miners_batch_complete(batch_id))
        self.status.set("idle")

    async def run(self):
        """Main validator loop"""
        logger.info("Starting ChipForge Validator")
        connector = aiohttp.TCPConnector(limit=100, limit_per_host=30)
        self.session = aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=300))
        self.initialize_components()

        # Liveness file for the container healthcheck (goes stale if the loop blocks)
        self._background(heartbeat_loop("validator", self._stop))
        self._background(self.status.loop(self._stop))
        if self.reward_ladder.active:
            logger.info("Weights follow the stored record ladder until the challenge server is reached")

        consecutive_errors = 0
        try:
            while not self._stop.is_set():
                pause = 10
                try:
                    await self.run_evaluation_cycle()
                    consecutive_errors = 0
                except Exception as e:
                    consecutive_errors += 1
                    pause = min(300, 30 * (2 ** min(consecutive_errors - 1, 4)))
                    logger.error(f"Error in evaluation cycle (#{consecutive_errors}): {e}; retrying in {pause}s")
                    logger.error(traceback.format_exc())

                # Put the current target on chain (on change or periodic refresh)
                await self.apply_weights()

                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=pause)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.state.save_state()
            await self.evaluators.close()
            await self.session.close()
            logger.info("ChipForge Validator stopped")


def get_config():
    """Get validator configuration"""
    # bittensor>=10 ships with CLI parsing disabled by default
    # (BT_NO_PARSE_CLI_ARGS defaults to "true"), in which case bt.Config(parser)
    # silently returns only defaults (wallet "default", netuid None, ...).
    # Opt back in unless the operator has explicitly overridden it.
    os.environ.setdefault("BT_NO_PARSE_CLI_ARGS", "false")

    parser = argparse.ArgumentParser(description="ChipForge Subnet Validator")
    
    # Add bittensor arguments
    bt.Wallet.add_args(parser)
    bt.Subtensor.add_args(parser)
    bt.logging.add_args(parser)
    bt.Axon.add_args(parser)
    
    # Add custom arguments
    parser.add_argument("--challenge_api_url", type=str,
                       default=os.getenv("CHALLENGE_API_URL", "https://api.chipforge.io"),
                       help="Challenge server API URL (default: CHALLENGE_API_URL env or https://api.chipforge.io)")
    # Prefer VALIDATOR_SECRET_KEY in .env: command-line arguments are visible to every user via `ps`
    parser.add_argument("--validator_secret_key", type=str, default=os.getenv("VALIDATOR_SECRET_KEY"),
                       required=not os.getenv("VALIDATOR_SECRET_KEY"),
                       help="Validator API key for the challenge server (default: VALIDATOR_SECRET_KEY env)")
    parser.add_argument("--netuid", type=int, required=True,
                       help="Subnet netuid")
    parser.add_argument("--miner_emission_percentage", type=float,
                       default=float(os.getenv("MINER_EMISSION_PERCENTAGE", "10")),
                       help="Percentage of emissions given to the winner miner (0-100); the rest is burned. "
                            "Default: MINER_EMISSION_PERCENTAGE env or 10")

    # Parse arguments and create config
    config = bt.Config(parser)  # Pass parser, not args
    
    return config


async def main():
    """Main function"""
    try:
        config = get_config()
        validator = ChipForgeValidator(config)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, validator.request_stop)
        await validator.run()
        
    except KeyboardInterrupt:
        logger.info("Validator stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        logger.error(traceback.format_exc())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))