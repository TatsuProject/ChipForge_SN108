#!/usr/bin/env python3
"""
ChipForge Subnet Miner (Updated for Built-in Synapse Fields)
Receives challenge notifications and manages challenge downloads
"""

import bittensor as bt
from chipforge.protocol import SimpleMessage
import os
import asyncio
import zipfile
import requests
from datetime import datetime, timezone
from pathlib import Path
import logging
import traceback
import json
import argparse
from typing import Dict, Optional, Tuple

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


class ChipForgeMiner:
    """ChipForge Subnet Miner using built-in bt.Synapse fields"""
    
    def __init__(self, config):
        self.config = config
        self.wallet = bt.Wallet(config=config)
        self.subtensor = bt.Subtensor(config=config)
        self.metagraph = self.subtensor.metagraph(config.netuid)
        self.axon = bt.Axon(wallet=self.wallet, config=config)
        
        # Challenge storage
        self.challenge_dir = Path('./downloaded_active_challenge')
        self.challenge_dir.mkdir(exist_ok=True)
        
        # State tracking
        self.current_challenge_id = None
        self.current_github_url = None
        
        logger.info(f"ChipForge Miner initialized")
        logger.info(f"Miner hotkey: {self.wallet.hotkey.ss58_address}")
        logger.info(f"Challenge directory: {self.challenge_dir.absolute()}")
        
        # Setup axon handlers
        self.setup_axon_handlers()

    def setup_axon_handlers(self):
        """Setup axon handlers with function-based registration"""
        
        # Register handler for SimpleMessage synapse specifically
        self.axon.attach(
            forward_fn=self.handle_simple_message,
            blacklist_fn=self.blacklist_simple_message,
            priority_fn=self.priority_simple_message
        )
        
        logger.info("Axon handlers registered for SimpleMessage")

    async def handle_simple_message(self, synapse: SimpleMessage) -> SimpleMessage:
        """Handle SimpleMessage synapse - function signature is critical"""
        try:
            logger.info(f"RECEIVED SimpleMessage: {synapse.message}")
            
            message = synapse.message
            if 'CHALLENGE_ACTIVE:' in message:
                return await self.handle_challenge_message(synapse, message)
            elif 'BATCH_COMPLETE:' in message:
                return await self.handle_batch_message(synapse, message)
            
            synapse.response = "OK"
            return synapse
            
        except Exception as e:
            logger.error(f"Error handling SimpleMessage: {e}")
            synapse.response = f"ERROR: {str(e)}"
            return synapse

    async def blacklist_simple_message(self, synapse: SimpleMessage) -> Tuple[bool, str]:
        """Blacklist function for SimpleMessage"""
        return False, ""

    async def priority_simple_message(self, synapse: SimpleMessage) -> float:
        """Priority function for SimpleMessage"""
        return 1000.0

    async def handle_challenge_message(self, synapse: SimpleMessage, message: str) -> SimpleMessage:
        """Handle challenge notification message - simplified to just acknowledge"""
        try:
            # Parse message: "CHALLENGE_ACTIVE:{challenge_id}:{github_url}:{timestamp}"
            parts = message.split(':')
            
            if len(parts) >= 4 and parts[0] == "CHALLENGE_ACTIVE":
                challenge_id = parts[1]
                github_url = parts[2] 
                timestamp = parts[3]
                
                logger.info(f"RECEIVED CHALLENGE NOTIFICATION: {challenge_id}")
                logger.info(f"GitHub URL from validator: {github_url}")
                logger.info(f"Timestamp: {timestamp}")
                
                # Just update current challenge info - don't download here
                self.current_challenge_id = challenge_id
                # Note: We'll get the correct URL from poll_for_challenges
                
                # Always respond OK since we'll handle download in poll_for_challenges
                synapse.response = "OK"
                logger.info(f"Acknowledged challenge notification for {challenge_id}")
                
            else:
                logger.warning(f"Invalid challenge message format: {message}")
                synapse.response = "INVALID_FORMAT"
            
            return synapse
            
        except Exception as e:
            logger.error(f"Error handling challenge message: {e}")
            logger.error(traceback.format_exc())
            synapse.response = f"ERROR: {str(e)}"
            return synapse
    
    async def handle_batch_message(self, synapse: SimpleMessage, message: str) -> SimpleMessage:
        """Handle batch completion message - just acknowledge"""
        try:
            # Parse message: "BATCH_COMPLETE:{batch_id}:{timestamp}"
            parts = message.split(':')
            
            if len(parts) >= 3 and parts[0] == "BATCH_COMPLETE":
                batch_id = parts[1]
                timestamp = parts[2]
                
                logger.info(f"RECEIVED BATCH COMPLETION NOTIFICATION: {batch_id}")
                logger.info(f"Timestamp: {timestamp}")
                
                # Just acknowledge - no action needed
                synapse.response = "OK"
                logger.info(f"Acknowledged batch completion for {batch_id}")
                
            else:
                logger.warning(f"Invalid batch message format: {message}")
                synapse.response = "INVALID_FORMAT"
            
            return synapse
            
        except Exception as e:
            logger.error(f"Error handling batch message: {e}")
            logger.error(traceback.format_exc())
            synapse.response = f"ERROR: {str(e)}"
            return synapse

    async def blacklist_synapse(self, synapse: bt.Synapse) -> Tuple[bool, str]:
        """Blacklist function for all synapses"""
        # Never blacklist - accept all synapses
        return False, ""

    async def priority_synapse(self, synapse: bt.Synapse) -> float:
        """Priority function for all synapses"""
        # High priority for all synapses
        return 1000.0
    
    async def download_challenge(self, challenge_id: str, github_url: str) -> bool:
        """Download challenge from GitHub and extract to challenge directory"""
        try:
            # Create challenge-specific directory instead of clearing all
            challenge_specific_dir = self.challenge_dir / challenge_id
            
            # Check if already exists
            if challenge_specific_dir.exists():
                logger.info(f"Challenge {challenge_id} directory already exists, skipping download")
                return True
            
            challenge_specific_dir.mkdir(parents=True, exist_ok=True)
            
            # Parse GitHub URL to get download URL
            download_url = self.convert_github_url_to_download(github_url)
            if not download_url:
                logger.error(f"Could not convert GitHub URL to download URL: {github_url}")
                return False
            
            logger.info(f"Downloading challenge {challenge_id} from: {download_url}")
            
            # Download challenge
            response = requests.get(download_url, timeout=30)
            response.raise_for_status()
            
            # Save and extract ZIP
            zip_path = challenge_specific_dir / f"{challenge_id}.zip"
            with open(zip_path, 'wb') as f:
                f.write(response.content)
            
            # Extract ZIP
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                zip_ref.extractall(challenge_specific_dir)
            
            # Remove ZIP file
            zip_path.unlink()
            
            # Create metadata file
            metadata = {
                'challenge_id': challenge_id,
                'github_url': github_url,
                'download_url': download_url,
                'downloaded_at': datetime.now(timezone.utc).isoformat(),
                'miner_hotkey': self.wallet.hotkey.ss58_address
            }
            
            metadata_path = challenge_specific_dir / 'challenge_metadata.json'
            with open(metadata_path, 'w') as f:
                json.dump(metadata, f, indent=2)
            
            logger.info(f"Challenge {challenge_id} downloaded and extracted successfully")
            
            # List downloaded files
            files = list(challenge_specific_dir.rglob('*'))
            logger.info(f"Downloaded {len(files)} files/directories for {challenge_id}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error downloading challenge {challenge_id}: {e}")
            logger.error(traceback.format_exc())
            return False
    
    def convert_github_url_to_download(self, github_url: str) -> str:
        """Convert GitHub repository URL to ZIP download URL"""
        try:
            # Handle different GitHub URL formats
            if github_url.endswith('.git'):
                github_url = github_url[:-4]
            
            if '/tree/' in github_url:
                # URL points to specific branch/folder
                parts = github_url.split('/tree/')
                base_url = parts[0]
                branch_path = parts[1]
                
                if '/' in branch_path:
                    # Extract branch name (first part before /)
                    branch = branch_path.split('/')[0]
                else:
                    branch = branch_path
                
                download_url = f"{base_url}/archive/{branch}.zip"
            else:
                # URL points to main repository
                download_url = f"{github_url}/archive/main.zip"
            
            return download_url
            
        except Exception as e:
            logger.error(f"Error converting GitHub URL: {e}")
            return ""
    
    async def check_active_challenge(self) -> Optional[Dict]:
        """Check if there's an active challenge"""
        try:
            # Use same API as validator
            url = f"{self.config.challenge_api_url}/api/v1/challenges/active"
            
            response = requests.get(url, timeout=30)
            if response.status_code == 200:
                challenge = response.json()
                if challenge and isinstance(challenge, dict) and 'challenge_id' in challenge:
                    return challenge
            return None
        except Exception as e:
            logger.error(f"Error checking active challenge: {e}")
            return None

    async def poll_for_challenges(self):
        """Periodically check for new challenges"""
        downloaded_challenges = set()  # Track downloaded challenges
        
        while True:
            try:
                challenge = await self.check_active_challenge()
                
                if challenge:
                    challenge_id = challenge['challenge_id']
                    github_url = challenge.get('github_url', '')
                    
                    # Check if this is a new challenge AND not already downloaded
                    if challenge_id != self.current_challenge_id or challenge_id not in downloaded_challenges:
                        logger.info(f"New challenge detected: {challenge_id}")
                        
                        # Update current state
                        self.current_challenge_id = challenge_id
                        self.current_github_url = github_url
                        
                        # Download challenge only if not already downloaded
                        if challenge_id not in downloaded_challenges and github_url:
                            success = await self.download_challenge(challenge_id, github_url)
                            if success:
                                downloaded_challenges.add(challenge_id)
                                logger.info(f"Auto-downloaded challenge {challenge_id}")
                            else:
                                logger.error(f"Failed to auto-download challenge {challenge_id}")
                        elif challenge_id in downloaded_challenges:
                            logger.info(f"Challenge {challenge_id} already downloaded, skipping")
                else:
                    # No active challenge
                    if self.current_challenge_id:
                        logger.info("No active challenge found")
                        self.current_challenge_id = None
                        self.current_github_url = None
            
            except Exception as e:
                logger.error(f"Error in challenge polling: {e}")
            
            # Check every 60 seconds
            await asyncio.sleep(60)
    
    
    async def run(self):
        """Main miner loop"""
        logger.info("Starting ChipForge Miner with built-in Synapse fields")
        
        # Serve axon
        self.axon.serve(netuid=self.config.netuid, subtensor=self.subtensor)
        self.axon.start()
        
        # Debug network info
        logger.info(f"Axon external IP: {self.axon.external_ip}")
        logger.info(f"Axon external port: {self.axon.external_port}")
        logger.info(f"Axon is serving: {self.axon.started}")
        
        logger.info(f"Axon serving on {self.axon.external_ip}:{self.axon.external_port}")
        logger.info(f"Miner hotkey: {self.wallet.hotkey.ss58_address}")
        logger.info(f"Waiting for synapses from validators...")
        
        # Start challenge polling task
        polling_task = asyncio.create_task(self.poll_for_challenges())
        
        try:
            while True:
                try:
                    # Sync metagraph more frequently to see validators
                    self.metagraph.sync(subtensor=self.subtensor)
                    logger.debug(f"Metagraph synced - {len(self.metagraph.neurons)} neurons")
                    
                    await asyncio.sleep(60)
                    
                except KeyboardInterrupt:
                    logger.info("Received interrupt signal")
                    break
                except Exception as e:
                    logger.error(f"Error in miner loop: {e}")
                    await asyncio.sleep(10)
        
        finally:
            polling_task.cancel()
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
                       default="http://localhost:8000",
                       help="Challenge server API URL")
    
    config = bt.Config(parser)
    
    return config


async def main():
    """Main function"""
    try:
        config = get_config()
        miner = ChipForgeMiner(config)
        await miner.run()
        
    except KeyboardInterrupt:
        logger.info("Miner stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        logger.error(traceback.format_exc())


if __name__ == "__main__":
    asyncio.run(main())