#!/usr/bin/env python3
"""
ChipForge Subnet Miner (Updated for Built-in Synapse Fields)
Receives challenge notifications and manages challenge downloads
"""

import bittensor as bt
from chipforge.protocol import SimpleMessage
from chipforge.heartbeat import heartbeat_loop
from dotenv import load_dotenv
import os
import asyncio
import shutil
import signal
import tempfile
import time
import zipfile
import requests
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import logging
import traceback
import json
import argparse
from typing import Dict, Optional, Tuple

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
# Global registration - try multiple approaches
try:
    # Method 1: Global synapse registry
    if hasattr(bt, '_synapse_registry'):
        bt._synapse_registry['SimpleMessage'] = SimpleMessage
    
    # Method 2: Add to Synapse class
    if hasattr(bt.Synapse, '_synapses'):
        bt.Synapse._synapses['SimpleMessage'] = SimpleMessage
    
    # Method 3: Module-level globals
    globals()['SimpleMessage'] = SimpleMessage
    
    logger.info("SimpleMessage registered globally")
except Exception as e:
    logger.error(f"Failed to register SimpleMessage: {e}")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class ChallengeDownloadError(Exception):
    pass


def resolve_download_url(challenge_id: str, url: str, api_url: str) -> str:
    """Where to fetch the challenge package from.

    GitHub repository URLs become archive ZIP URLs; any other URL (the challenge server's
    /challenges/{id}/download link, a pre-signed S3 link) is used as-is; with no URL we fall
    back to the challenge server's download endpoint."""
    url = (url or "").strip()
    if not url:
        return f"{api_url.rstrip('/')}/api/v1/challenges/{challenge_id}/download"
    parsed = urlparse(url)
    if parsed.netloc.lower() not in ("github.com", "www.github.com") or "/archive/" in parsed.path:
        return url
    if url.endswith('.git'):
        url = url[:-4]
    if '/tree/' in url:
        base_url, branch_path = url.split('/tree/', 1)
        return f"{base_url}/archive/{branch_path.split('/')[0]}.zip"
    return f"{url.rstrip('/')}/archive/main.zip"


def safe_extract(zip_path: Path, dest: Path, max_members: int, max_total_bytes: int) -> int:
    """Extract with limits on member count and total uncompressed size (zip bombs).
    zipfile itself already strips '..' and absolute paths."""
    with zipfile.ZipFile(zip_path) as zf:
        members = zf.infolist()
        if len(members) > max_members:
            raise ChallengeDownloadError(f"archive has {len(members)} entries (limit {max_members})")
        total = sum(m.file_size for m in members)
        if total > max_total_bytes:
            raise ChallengeDownloadError(f"archive expands to {total} bytes (limit {max_total_bytes})")
        zf.extractall(dest)
        return len(members)


class ChipForgeMiner:
    """ChipForge Subnet Miner using built-in bt.Synapse fields"""

    def __init__(self, config):
        self.config = config
        self.wallet = bt.Wallet(config=config)
        self.subtensor = bt.Subtensor(config=config)
        self.metagraph = self.subtensor.metagraph(config.netuid)
        self.axon = bt.Axon(wallet=self.wallet, config=config)

        # Challenge storage
        self.challenge_dir = Path(os.getenv("MINER_CHALLENGE_DIR", "./downloaded_active_challenge"))
        self.challenge_dir.mkdir(parents=True, exist_ok=True)

        # Tunables (environment)
        self.poll_seconds = max(30.0, _env_float("MINER_POLL_SECONDS", 300))
        self.metagraph_sync_seconds = max(60.0, _env_float("METAGRAPH_SYNC_SECONDS", 600))
        self.max_download_bytes = int(_env_float("MINER_MAX_CHALLENGE_MB", 200) * 1024 * 1024)
        self.max_extracted_bytes = int(_env_float("MINER_MAX_EXTRACTED_MB", 1024) * 1024 * 1024)
        self.max_zip_members = int(_env_float("MINER_MAX_ZIP_MEMBERS", 10000))
        self.require_validator_permit = os.getenv("MINER_REQUIRE_VALIDATOR_PERMIT", "true").lower() != "false"

        # State tracking
        self.current_challenge_id = None
        self.current_github_url = None
        self.downloaded_challenges = set()
        self._poll_now = asyncio.Event()
        self._stop = asyncio.Event()
        self._last_metagraph_sync = time.monotonic()

        logger.info(f"ChipForge Miner initialized")
        logger.info(f"Miner hotkey: {self.wallet.hotkey.ss58_address}")
        logger.info(f"Challenge directory: {self.challenge_dir.absolute()}")
        logger.info(f"Challenge server: {self.config.challenge_api_url} (poll every {self.poll_seconds:.0f}s)")

        # Setup axon handlers
        self.setup_axon_handlers()

    def setup_axon_handlers(self):
        """Setup axon handlers with function-based registration"""
        self.axon.attach(
            forward_fn=self.handle_simple_message,
            blacklist_fn=self.blacklist_simple_message,
            priority_fn=self.priority_simple_message
        )
        logger.info("Axon handlers registered for SimpleMessage")

    def request_stop(self):
        self._stop.set()
        self._poll_now.set()

    async def handle_simple_message(self, synapse: SimpleMessage) -> SimpleMessage:
        """Handle SimpleMessage synapse - function signature is critical"""
        try:
            message = synapse.message or ""
            logger.info(f"RECEIVED SimpleMessage: {message}")
            if message.startswith('CHALLENGE_ACTIVE:'):
                return await self.handle_challenge_message(synapse, message)
            elif message.startswith('BATCH_COMPLETE:'):
                return await self.handle_batch_message(synapse, message)

            synapse.response = "OK"
            return synapse

        except Exception as e:
            logger.error(f"Error handling SimpleMessage: {e}")
            synapse.response = f"ERROR: {str(e)}"
            return synapse

    async def blacklist_simple_message(self, synapse: SimpleMessage) -> Tuple[bool, str]:
        """Only registered validators (validator_permit) may message this miner."""
        hotkey = getattr(getattr(synapse, 'dendrite', None), 'hotkey', None)
        if not hotkey or hotkey not in self.metagraph.hotkeys:
            return True, "unregistered hotkey"
        if self.require_validator_permit:
            uid = self.metagraph.hotkeys.index(hotkey)
            if not bool(self.metagraph.validator_permit[uid]):
                return True, "no validator permit"
        return False, ""

    async def priority_simple_message(self, synapse: SimpleMessage) -> float:
        """Priority by caller stake"""
        try:
            uid = self.metagraph.hotkeys.index(synapse.dendrite.hotkey)
            return float(self.metagraph.S[uid])
        except Exception:
            return 0.0

    async def handle_challenge_message(self, synapse: SimpleMessage, message: str) -> SimpleMessage:
        """A validator says a challenge is active. The package itself is always fetched
        from the challenge server (never from a URL inside the message); we just poll now."""
        # "CHALLENGE_ACTIVE:{challenge_id}:{github_url}:{timestamp}" (the URL contains ':')
        parts = message.split(':', 2)
        if len(parts) < 3 or not parts[1]:
            logger.warning(f"Invalid challenge message format: {message}")
            synapse.response = "INVALID_FORMAT"
            return synapse

        challenge_id = parts[1]
        logger.info(f"RECEIVED CHALLENGE NOTIFICATION: {challenge_id}")
        self.current_challenge_id = challenge_id
        if challenge_id not in self.downloaded_challenges:
            self._poll_now.set()
        synapse.response = "OK"
        return synapse

    async def handle_batch_message(self, synapse: SimpleMessage, message: str) -> SimpleMessage:
        """Handle batch completion message - just acknowledge"""
        parts = message.split(':', 2)
        if len(parts) >= 2 and parts[0] == "BATCH_COMPLETE":
            logger.info(f"RECEIVED BATCH COMPLETION NOTIFICATION: {parts[1]}")
            synapse.response = "OK"
        else:
            logger.warning(f"Invalid batch message format: {message}")
            synapse.response = "INVALID_FORMAT"
        return synapse

    def convert_github_url_to_download(self, github_url: str) -> str:
        """Kept for callers of the old API; see resolve_download_url."""
        return resolve_download_url("", github_url, self.config.challenge_api_url)

    def _fetch_to_file(self, url: str, dest: Path) -> None:
        """Streamed download with a size cap (runs in a worker thread)."""
        with requests.get(url, timeout=(10, 120), stream=True, allow_redirects=True) as response:
            response.raise_for_status()
            size = 0
            with open(dest, 'wb') as f:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    size += len(chunk)
                    if size > self.max_download_bytes:
                        raise ChallengeDownloadError(f"download exceeds {self.max_download_bytes} bytes")
                    f.write(chunk)

    async def download_challenge(self, challenge_id: str, github_url: str) -> bool:
        """Download the challenge package and extract it to challenge_dir/<challenge_id>.

        Everything happens in a temporary directory that is renamed into place only on
        success, so a failed attempt leaves nothing behind and the next poll retries."""
        if not challenge_id or Path(challenge_id).name != challenge_id or challenge_id in ('.', '..'):
            logger.error(f"Refusing unsafe challenge id: {challenge_id!r}")
            return False
        final_dir = self.challenge_dir / challenge_id
        if (final_dir / 'challenge_metadata.json').exists():
            logger.info(f"Challenge {challenge_id} already downloaded, skipping")
            return True

        download_url = resolve_download_url(challenge_id, github_url, self.config.challenge_api_url)
        logger.info(f"Downloading challenge {challenge_id} from: {download_url}")
        work_dir = Path(tempfile.mkdtemp(prefix=f".{challenge_id}.partial-", dir=self.challenge_dir))
        try:
            zip_path = work_dir / f"{challenge_id}.zip"
            await asyncio.to_thread(self._fetch_to_file, download_url, zip_path)
            extract_dir = work_dir / challenge_id
            extract_dir.mkdir()
            count = await asyncio.to_thread(safe_extract, zip_path, extract_dir,
                                            self.max_zip_members, self.max_extracted_bytes)
            metadata = {
                'challenge_id': challenge_id,
                'github_url': github_url,
                'download_url': download_url,
                'downloaded_at': datetime.now(timezone.utc).isoformat(),
                'miner_hotkey': self.wallet.hotkey.ss58_address
            }
            (extract_dir / 'challenge_metadata.json').write_text(json.dumps(metadata, indent=2))

            if final_dir.exists():            # leftover from an older, non-atomic miner version
                shutil.rmtree(final_dir)
            os.replace(extract_dir, final_dir)
            logger.info(f"Challenge {challenge_id} downloaded and extracted ({count} entries) to {final_dir}")
            return True

        except Exception as e:
            logger.error(f"Error downloading challenge {challenge_id}: {e}")
            return False
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def _get_active_challenge(self) -> Optional[Dict]:
        url = f"{self.config.challenge_api_url}/api/v1/challenges/active"
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            challenge = response.json()
            if isinstance(challenge, dict) and challenge.get('challenge_id'):
                return challenge
        return None

    async def check_active_challenge(self) -> Optional[Dict]:
        """Check if there's an active challenge (HTTP runs off the event loop)"""
        try:
            return await asyncio.to_thread(self._get_active_challenge)
        except Exception as e:
            logger.error(f"Error checking active challenge: {e}")
            return None

    async def poll_once(self):
        challenge = await self.check_active_challenge()
        if not challenge:
            if self.current_challenge_id:
                logger.info("No active challenge found")
            self.current_challenge_id = None
            self.current_github_url = None
            return

        challenge_id = challenge['challenge_id']
        github_url = challenge.get('github_url') or ''
        if challenge_id != self.current_challenge_id:
            logger.info(f"New challenge detected: {challenge_id}")
        self.current_challenge_id = challenge_id
        self.current_github_url = github_url

        if challenge_id not in self.downloaded_challenges:
            if await self.download_challenge(challenge_id, github_url):
                self.downloaded_challenges.add(challenge_id)
                logger.info(f"Auto-downloaded challenge {challenge_id}")
            else:
                logger.error(f"Failed to auto-download challenge {challenge_id}; will retry next poll")

    async def poll_for_challenges(self):
        """Poll the challenge server every MINER_POLL_SECONDS, or right away when a
        validator announces a challenge we don't have yet."""
        while not self._stop.is_set():
            self._poll_now.clear()
            try:
                await self.poll_once()
            except Exception as e:
                logger.error(f"Error in challenge polling: {e}")
            try:
                await asyncio.wait_for(self._poll_now.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def maybe_sync_metagraph(self):
        if time.monotonic() - self._last_metagraph_sync < self.metagraph_sync_seconds:
            return
        self._last_metagraph_sync = time.monotonic()
        try:
            await asyncio.to_thread(self.metagraph.sync, subtensor=self.subtensor)
            logger.debug(f"Metagraph synced - {len(self.metagraph.hotkeys)} neurons")
        except Exception as e:
            logger.error(f"Metagraph sync failed: {e}")

    async def run(self):
        """Main miner loop"""
        logger.info("Starting ChipForge Miner")

        self.axon.serve(netuid=self.config.netuid, subtensor=self.subtensor)
        self.axon.start()
        logger.info(f"Axon serving on {self.axon.external_ip}:{self.axon.external_port}")
        logger.info(f"Miner hotkey: {self.wallet.hotkey.ss58_address}")
        logger.info(f"Waiting for synapses from validators...")

        polling_task = asyncio.create_task(self.poll_for_challenges())
        heartbeat_task = asyncio.create_task(heartbeat_loop("miner", self._stop))
        try:
            while not self._stop.is_set():
                await self.maybe_sync_metagraph()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=30)
                except asyncio.TimeoutError:
                    pass
        finally:
            self.request_stop()
            polling_task.cancel()
            heartbeat_task.cancel()
            self.axon.stop()
            logger.info("ChipForge Miner stopped")


def get_config():
    """Get miner configuration"""
    
    # bittensor>=10 ships with CLI parsing disabled by default
    # (BT_NO_PARSE_CLI_ARGS defaults to "true"), in which case bt.Config(parser)
    # silently returns only defaults (wallet "default", netuid None, ...).
    # Opt back in unless the operator has explicitly overridden it.
    os.environ.setdefault("BT_NO_PARSE_CLI_ARGS", "false")

    parser = argparse.ArgumentParser(description="ChipForge Subnet Miner")
    
    # Add bittensor arguments
    bt.Wallet.add_args(parser)
    bt.Subtensor.add_args(parser)
    bt.logging.add_args(parser)
    bt.Axon.add_args(parser)
    
    parser.add_argument("--netuid", type=int, required=True,
                       help="Subnet netuid")
    parser.add_argument("--challenge_api_url", type=str,
                       default=os.getenv("CHALLENGE_API_URL", "https://api.chipforge.io"),
                       help="Challenge server API URL (default: CHALLENGE_API_URL or https://api.chipforge.io)")
    
    config = bt.Config(parser)
    
    return config


async def main():
    """Main function"""
    try:
        config = get_config()
        config.challenge_api_url = config.challenge_api_url.rstrip('/')
        miner = ChipForgeMiner(config)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, miner.request_stop)
            except NotImplementedError:
                pass
        await miner.run()
        
    except KeyboardInterrupt:
        logger.info("Miner stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        logger.error(traceback.format_exc())


if __name__ == "__main__":
    asyncio.run(main())