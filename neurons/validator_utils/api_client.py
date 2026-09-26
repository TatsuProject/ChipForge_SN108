#!/usr/bin/env python3
# neurons/validator_utils/api_client.py
"""
API Client for ChipForge Validator
Handles all API communications with challenge server and EDA server
"""

import asyncio
import aiohttp
import aiofiles
import hashlib
import logging
import os
import re
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any
from cryptography.hazmat.primitives.asymmetric import ed25519
import zipfile
from dotenv import load_dotenv
load_dotenv()

from .storage import data_path

logger = logging.getLogger(__name__)

_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]{1,200}$")


def safe_filename(name: Optional[str], fallback: str) -> str:
    """A server-provided filename reduced to a plain basename; anything else -> fallback."""
    base = os.path.basename((name or "").strip().strip('"'))
    return base if base and base not in (".", "..") and _SAFE_FILENAME.match(base) else fallback


def parse_server_time(value) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


class APIClient:
    """Handles API communications for the validator"""
    
    def __init__(self, config, wallet, session: aiohttp.ClientSession, state=None):
        self.config = config
        self.wallet = wallet
        self.session = session
        self.state = state  # ValidatorState for reading dynamic timeout windows
        
        # Challenge server configuration
        self.api_url = getattr(config, 'challenge_api_url', 'http://localhost:8000')
        self.validator_secret = getattr(config, 'validator_secret_key', '')
        
        # EDA Server configuration
        self.eda_server_url = os.getenv("EDA_SERVER_URL", "http://localhost:8080")
        self.use_dummy_evaluation = os.getenv("USE_DUMMY_EVALUATION", "false").lower() == "true"
        
        # Validator authentication
        self.validator_hotkey = self.wallet.hotkey.ss58_address
        
        self.eda_max_concurrency = max(1, int(os.getenv("EDA_MAX_CONCURRENCY", "4")))
        # Seconds kept free before the server's evaluation deadline for submitting scores
        self.eda_deadline_buffer = int(os.getenv("EDA_DEADLINE_BUFFER_SECONDS", "90"))

        # Directories
        self.base_dir = data_path('validator_data')
        self.submissions_dir = self.base_dir / 'submissions'
        self.submissions_dir.mkdir(parents=True, exist_ok=True)

    async def check_server_connectivity(self) -> bool:
        """Check if challenge server is reachable"""
        try:
            url = f"{self.api_url}/api/v1/health"
            async with self.session.get(url, timeout=5) as response:
                return response.status in [200, 404]  # 404 is ok, means server is up
        except Exception:
            return False
    
    def create_signature(self, message: str) -> str:
        """Create signature using Bittensor's native signing method"""
        try:
            # Use Bittensor wallet's native signing (same as submission signatures)
            signature_bytes = self.wallet.hotkey.sign(data=message)
            signature_hex = signature_bytes.hex()
            
            logger.debug(f"Validator signature created:")
            logger.debug(f"  Message: {message}")
            logger.debug(f"  Signature: {signature_hex}")
            
            return signature_hex
            
        except Exception as e:
            logger.error(f"Error creating validator signature: {e}")
            raise
    
    async def get_active_challenge(self) -> Optional[Dict]:
        """
        Get active challenge from server with connection error handling
        
        Returns:
            Dict: Challenge data if active challenge exists (includes winner_reward_hours)
            {"status": "no_active_challenge"}: Server accessible but no challenge (intentional)
            None: Only on connection errors (will raise ConnectionError instead)
        """
        try:
            url = f"{self.api_url}/api/v1/challenges/active"
            async with self.session.get(url, timeout=10) as response:
                if response.status == 200:
                    challenge = await response.json()
                    
                    # Handle new response format - server intentionally says no challenge
                    if isinstance(challenge, dict) and challenge.get('status') == 'no_active_challenge':
                        logger.info("Server accessible: No active challenge (server response)")
                        return {"status": "no_active_challenge"}
                    
                    # Old format - null response
                    if challenge is None:
                        logger.info("Server accessible: No active challenge (null response)")
                        return {"status": "None"}
                    
                    # Validate challenge structure
                    if not isinstance(challenge, dict) or 'challenge_id' not in challenge:
                        logger.warning(f"Invalid challenge response format: {challenge}")
                        return {"status": "None"}
                    
                    # Extract winner_reward_hours if present
                    if 'winner_reward_hours' in challenge:
                        logger.info(f"Active challenge: {challenge['challenge_id']} (winner_reward_hours: {challenge['winner_reward_hours']}h)")
                    else:
                        logger.warning(f"Active challenge {challenge['challenge_id']} missing winner_reward_hours - will use local fallback")
                    
                    return challenge
                else:
                    logger.debug(f"No active challenge found: HTTP {response.status}")
                    return {"status": "None"}
                    
        except asyncio.TimeoutError:
            logger.error("Timeout connecting to challenge server")
            raise ConnectionError("Challenge server timeout")
        except aiohttp.ClientError as e:
            logger.error(f"Connection error: {e}")
            raise ConnectionError(f"Challenge server unreachable: {e}")
        except Exception as e:
            logger.error(f"Unexpected error getting challenge: {e}")
            raise

    async def get_challenge_info(self, challenge_id: str) -> Optional[Dict]:
        """Get challenge information including remaining time and winner baseline score"""
        try:
            url = f"{self.api_url}/api/v1/challenges/{challenge_id}/info"
            async with self.session.get(url) as response:
                if response.status == 200:
                    challenge = await response.json()
                    if challenge:
                        result = {}

                        # Extract remaining time
                        if 'expires_at' in challenge:
                            expires_at = datetime.fromisoformat(challenge['expires_at'].replace('Z', '+00:00'))
                            remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
                            result['remaining_time'] = max(0, remaining)

                        # Extract winner baseline score
                        if 'winner_baseline_score' in challenge:
                            result['winner_baseline_score'] = challenge['winner_baseline_score']
                            logger.info(f"Challenge {challenge_id} winner baseline score: {challenge['winner_baseline_score']}")

                        # Extract ban_emissions flag
                        if 'ban_emissions' in challenge:
                            result['ban_emissions'] = challenge['ban_emissions']
                            if challenge['ban_emissions']:
                                logger.warning(f"Challenge {challenge_id} has EMISSIONS BANNED")

                        # Extract download_new_testcases flag
                        if challenge.get('download_new_testcases'):
                            result['download_new_testcases'] = True

                        # Extract batch window configuration
                        if 'batch_download_window_seconds' in challenge:
                            result['batch_download_window_seconds'] = challenge['batch_download_window_seconds']
                        if 'batch_evaluation_window_seconds' in challenge:
                            result['batch_evaluation_window_seconds'] = challenge['batch_evaluation_window_seconds']

                        return result
                return None
        except Exception as e:
            logger.error(f"Error getting challenge info: {e}")
            return None

    async def get_challenge_remaining_time(self, challenge_id: str) -> Optional[float]:
        """Get remaining time for challenge in seconds (legacy method)"""
        info = await self.get_challenge_info(challenge_id)
        return info.get('remaining_time') if info else None
    
    async def get_current_batch(self, challenge_id: str) -> Optional[Dict]:
        """Get current evaluation batch with dynamic scheduling"""
        try:
            url = f"{self.api_url}/api/v1/challenges/{challenge_id}/batch/current"
            headers = {'X-Validator-Secret': self.validator_secret}
            
            # Create signature
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            message = f"{self.validator_hotkey}{timestamp}"
            signature = self.create_signature(message)
            
            params = {
                'validator_hotkey': self.validator_hotkey,
                'signature': signature,
                'timestamp': timestamp
            }
            
            async with self.session.get(url, headers=headers, params=params) as response:
                if response.status == 200:
                    batch = await response.json()
                    if batch.get('batch_id'):
                        logger.info(f"Found current batch: {batch['batch_id']} with {batch.get('available_submissions', 0)} submissions")
                        return batch
                    else:
                        logger.info(f"{batch}")
                else:
                    logger.debug(f"No current batch: {response.status}")
                return None
                
        except Exception as e:
            logger.error(f"Error getting current batch: {e}")
            return None
    
    async def download_batch_submissions(self, challenge_id: str, batch: Dict) -> Dict[str, bytes]:
        """Download all submissions in batch in parallel with proper filename handling"""
        submissions = batch.get('submissions', [])
        if not submissions:
            return {}
        
        logger.info(f"Downloading {len(submissions)} submissions in parallel")
        
        # Hash the miner signed, when the server provides it (challenge server >= 2026-09)
        expected_hash = {s['submission_id']: s.get('file_hash') for s in submissions}

        # Create download tasks
        tasks = []
        for submission in submissions:
            submission_id = submission['submission_id']
            task = self.download_submission(challenge_id, submission_id)
            tasks.append((submission_id, task))
        
        # Execute downloads in parallel
        downloaded = {}
        results = await asyncio.gather(*[task for _, task in tasks], return_exceptions=True)
        
        for (submission_id, _), result in zip(tasks, results):
            if isinstance(result, Exception):
                logger.error(f"Failed to download {submission_id}: {result}")
            elif result is not None and isinstance(result, dict):
                # Handle new return format with content, filename, and submission_id
                content = result['content']
                filename = safe_filename(result.get('filename'), f"{submission_id}.zip")

                expected = expected_hash.get(submission_id)
                if expected and hashlib.sha256(content).hexdigest() != expected:
                    logger.error(f"Download of {submission_id} does not match the hash the miner signed - discarded")
                    continue
                downloaded[submission_id] = content
                
                logger.info(f"Successfully downloaded {submission_id}: {len(content)} bytes")
                
                # Save to local file using server-provided filename
                batch_dir = self.submissions_dir / batch['batch_id']
                batch_dir.mkdir(exist_ok=True)
                
                file_path = batch_dir / filename
                async with aiofiles.open(file_path, 'wb') as f:
                    await f.write(content)
                logger.info(f"Saved {submission_id} as: {filename}")
            else:
                logger.warning(f"Invalid or empty result for {submission_id}")
        
        logger.info(f"Successfully downloaded {len(downloaded)} submissions")
        return downloaded
    
    async def download_submission(self, challenge_id: str, submission_id: str) -> Optional[Dict]:
        """Download a single submission with enhanced debugging and filename extraction"""
        max_retries = 3
        
        logger.info(f"Starting download for submission {submission_id} in challenge {challenge_id}")
        
        for attempt in range(max_retries):
            try:
                url = f"{self.api_url}/api/v1/challenges/{challenge_id}/submissions/{submission_id}/download"
                
                # Create fresh signature for each attempt
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                message = f"{self.validator_hotkey}{timestamp}"
                
                # Use the updated create_signature method (Bittensor native signing)
                signature = self.create_signature(message)
                
                headers = {
                    'X-Validator-Secret': self.validator_secret
                }
                
                params = {
                    'validator_hotkey': self.validator_hotkey,
                    'signature': signature,
                    'timestamp': timestamp
                }
                
                # Debug logging
                logger.info(f"Download attempt {attempt + 1} for {submission_id}")
                logger.info(f"  URL: {url}")
                logger.info(f"  Validator hotkey: {self.validator_hotkey}")
                logger.info(f"  Timestamp: {timestamp}")
                logger.info(f"  Message for signing: {message}")
                logger.info(f"  Generated signature: {signature}")
                logger.info(f"  Validator secret present: {'Yes' if self.validator_secret else 'No'}")
                
                async with self.session.get(url, headers=headers, params=params, timeout=30) as response:
                    logger.info(f"Response status for {submission_id}: {response.status}")
                    
                    if response.status == 200:
                        content = await response.read()
                        content_length = len(content)
                        logger.info(f"Successfully downloaded submission {submission_id}: {content_length} bytes")
                        
                        # Verify it's actually a ZIP file
                        if content.startswith(b'PK'):
                            logger.info(f"Downloaded content appears to be a valid ZIP file")
                        else:
                            logger.warning(f"Downloaded content may not be a valid ZIP file")
                        
                        # Extract filename from Content-Disposition header
                        content_disposition = response.headers.get('Content-Disposition', '')
                        if 'filename=' in content_disposition:
                            # Extract filename from header (handles both quoted and unquoted)
                            filename_part = content_disposition.split('filename=')[1]
                            if filename_part.startswith('"') and filename_part.endswith('"'):
                                filename = filename_part[1:-1]  # Remove quotes
                            else:
                                filename = filename_part.split(';')[0].strip()  # Handle multiple params
                            logger.info(f"Using server-provided filename: {filename}")
                        else:
                            filename = f"{submission_id}.zip"
                            logger.info(f"No Content-Disposition header, using fallback: {filename}")
                        
                        return {
                            'content': content,
                            'filename': filename,
                            'submission_id': submission_id
                        }
                        
                    else:
                        # Read the error response body for detailed error info
                        try:
                            error_text = await response.text()
                            logger.error(f"Failed to download submission {submission_id}")
                            logger.error(f"  Status: {response.status}")
                            logger.error(f"  Error response: {error_text}")
                            
                            # Log response headers for additional debugging
                            response_headers = dict(response.headers)
                            if response_headers:
                                logger.error(f"  Response headers: {response_headers}")
                                
                        except Exception as read_error:
                            logger.error(f"Failed to read error response body: {read_error}")
                        
                        # Handle specific error codes
                        if response.status == 401:
                            logger.error("Authentication failed - check signature generation and server verification")
                        elif response.status == 403:
                            logger.error("Forbidden - check validator secret or batch permissions")
                        elif response.status == 404:
                            logger.error("Not found - submission may not exist or not in current batch")
                        elif response.status == 409:
                            logger.error("Conflict - you may have already evaluated this submission")
                        
                        if attempt < max_retries - 1:
                            wait_time = 2 ** attempt
                            logger.info(f"Retrying in {wait_time} seconds...")
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            logger.error(f"Max retries exceeded for {submission_id}")
                            return None
                            
            except asyncio.TimeoutError:
                logger.error(f"Timeout downloading submission {submission_id} (attempt {attempt + 1})")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.info(f"Retrying after timeout in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Max retries exceeded due to timeouts for {submission_id}")
                    return None
                    
            except Exception as e:
                logger.error(f"Exception downloading submission {submission_id} (attempt {attempt + 1}): {e}")
                logger.error(f"Exception type: {type(e).__name__}")
                
                # Log full traceback for debugging
                import traceback
                logger.error(f"Traceback:\n{traceback.format_exc()}")
                
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.info(f"Retrying after exception in {wait_time} seconds...")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"Max retries exceeded after exceptions for {submission_id}")
                    return None
        
        logger.error(f"Complete failure: could not download {submission_id} after all attempts")
        return None
    
    async def get_submission_details(self, challenge_id: str, submission_ids: List[str]) -> Dict[str, str]:
        """Get miner hotkeys for submission IDs with enhanced debugging"""
        submission_hotkeys = {}
        
        logger.info(f"Getting submission details for {len(submission_ids)} submissions")
        logger.info(f"Submission IDs: {submission_ids}")
        
        try:
            url = f"{self.api_url}/api/v1/challenges/{challenge_id}/submissions"
            headers = {'X-Validator-Secret': self.validator_secret}
            
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            message = f"{self.validator_hotkey}{timestamp}"
            signature = self.create_signature(message)
            
            params = {
                'validator_hotkey': self.validator_hotkey,
                'signature': signature,
                'timestamp': timestamp
            }
            
            logger.info(f"Making API request to: {url}")
            logger.debug(f"Request params: {params}")
            
            async with self.session.get(url, headers=headers, params=params) as response:
                logger.info(f"API response status: {response.status}")
                
                if response.status == 200:
                    data = await response.json()
                    logger.info(f"API response data keys: {list(data.keys())}")
                    
                    submissions = data.get('submissions', [])
                    logger.info(f"Found {len(submissions)} total submissions in API response")
                    
                    # Debug: Show structure of first submission
                    if submissions:
                        first_sub = submissions[0]
                        logger.info(f"First submission structure: {list(first_sub.keys())}")
                        logger.info(f"First submission sample: {first_sub}")
                    
                    for submission in submissions:
                        sub_id = submission.get('submission_id')
                        # Try both possible field names
                        miner_hotkey = submission.get('miner_hotkey') or submission.get('hotkey')
                        
                        logger.debug(f"Processing submission: id={sub_id}, hotkey={miner_hotkey}")
                        
                        if sub_id in submission_ids:
                            if miner_hotkey:
                                submission_hotkeys[sub_id] = miner_hotkey
                                logger.info(f"✅ Mapped {sub_id} -> {miner_hotkey[:12]}...")
                            else:
                                logger.warning(f"❌ No hotkey found for submission {sub_id}")
                                logger.warning(f"Available fields: {list(submission.keys())}")
                        else:
                            logger.debug(f"Skipping submission {sub_id} (not in requested list)")
                    
                    logger.info(f"Successfully mapped {len(submission_hotkeys)} of {len(submission_ids)} submissions to hotkeys")
                    
                    # Show what we couldn't map
                    unmapped = set(submission_ids) - set(submission_hotkeys.keys())
                    if unmapped:
                        logger.warning(f"Could not map these submissions: {unmapped}")
                    
                else:
                    error_text = await response.text()
                    logger.error(f"API request failed with status {response.status}")
                    logger.error(f"Error response: {error_text}")
                    
                    if response.status == 401:
                        logger.error("Authentication failed - check signature generation")
                    elif response.status == 403:
                        logger.error("Forbidden - check validator secret")
                    elif response.status == 404:
                        logger.error("Challenge or submissions not found")
                        
        except Exception as e:
            logger.error(f"Exception getting submission details: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
        
        if not submission_hotkeys:
            logger.error("FAILED: No submissions mapped to hotkeys")
        
        return submission_hotkeys
    
    async def eda_server_ready(self) -> bool:
        """Pre-flight: is the EDA server reachable? Any HTTP answer counts (older gateways
        have no /health and answer 404); only a connection failure means unreachable."""
        if self.use_dummy_evaluation:
            return True
        try:
            async with self.session.get(f"{self.eda_server_url}/health",
                                        timeout=aiohttp.ClientTimeout(total=10)) as response:
                return response.status < 500 or response.status == 404
        except Exception as e:
            logger.error(f"EDA server {self.eda_server_url} unreachable: {e}")
            return False

    def _eda_time_budget(self, deadline: Optional[datetime]) -> float:
        """Seconds an EDA request may take: until the server's evaluation deadline minus a
        buffer for submitting scores; falls back to the configured batch windows."""
        if deadline is not None:
            return (deadline - datetime.now(timezone.utc)).total_seconds() - self.eda_deadline_buffer
        dl_window = getattr(self.state, 'batch_download_window_seconds', 0) if self.state else 0
        eval_window = getattr(self.state, 'batch_evaluation_window_seconds', 0) if self.state else 0
        if dl_window > 0 and eval_window > 0:
            return dl_window + eval_window - 45 - self.eda_deadline_buffer
        return 2640

    async def evaluate_submissions_with_eda_server(self, challenge_id: str, submissions: Dict[str, bytes],
                                                   deadline: Optional[datetime] = None) -> Dict[str, Dict]:
        """Evaluate submissions on the EDA server, at most EDA_MAX_CONCURRENCY at a time, each
        within the time left before the server's evaluation deadline."""
        logger.info(f"Evaluating {len(submissions)} submissions with EDA server using test cases")

        if self.use_dummy_evaluation:
            return await self._dummy_evaluate_submissions(submissions)

        evaluator_zip_path = self.get_testcase_files(challenge_id)
        if not evaluator_zip_path.exists():
            # Never score miners without test cases: evaluate nothing, the batch is skipped
            logger.error(f"Evaluator zip file not found: {evaluator_zip_path} - not evaluating")
            return {}
        evaluator_bytes = evaluator_zip_path.read_bytes()
        semaphore = asyncio.Semaphore(self.eda_max_concurrency)

        async def evaluate_single_submission(submission_id: str, submission_data: bytes) -> tuple:
            async with semaphore:
                budget = self._eda_time_budget(deadline)
                if budget < 30:
                    return submission_id, self._generate_fallback_evaluation(
                        submission_id,
                        evaluation_details={'status': 'timeout', 'error': 'No time left before the evaluation window closes'},
                        timeout_occurred=True,
                    )
                form_data = aiohttp.FormData()
                form_data.add_field('design_zip', submission_data, filename=f'{submission_id}.zip',
                                    content_type='application/zip')
                form_data.add_field('evaluator_zip', evaluator_bytes, filename=f'{challenge_id}_validator.zip',
                                    content_type='application/zip')
                form_data.add_field('submission_id', submission_id)
                logger.info(f"Sending {submission_id} to EDA server ({len(submission_data)} bytes, "
                            f"time budget {budget:.0f}s)")
                try:
                    async with self.session.post(f"{self.eda_server_url}/evaluate", data=form_data,
                                                 timeout=aiohttp.ClientTimeout(total=budget)) as response:
                        if response.status == 200:
                            result = await response.json()
                            logger.debug(f"EDA response for {submission_id}: {result}")
                            return submission_id, self._transform_eda_response(result, submission_id)
                        error_text = await response.text()
                        logger.error(f"EDA server error for {submission_id}: {response.status} - {error_text[:500]}")
                        return submission_id, self._generate_fallback_evaluation(
                            submission_id, evaluation_details={'status': 'eda_http_error', 'http_status': response.status})
                except asyncio.TimeoutError:
                    logger.error(f"Timeout evaluating {submission_id} with EDA server after {budget:.0f}s")
                    return submission_id, self._generate_fallback_evaluation(
                        submission_id,
                        evaluation_details={'status': 'timeout', 'error': f'EDA server evaluation timed out after {budget:.0f} seconds'},
                        timeout_occurred=True,
                    )
                except Exception as e:
                    logger.error(f"Exception during EDA evaluation for {submission_id}: {e}")
                    return submission_id, self._generate_fallback_evaluation(
                        submission_id, evaluation_details={'status': 'eda_unreachable', 'error': str(e)[:300]})

        results = await asyncio.gather(
            *[evaluate_single_submission(sid, data) for sid, data in submissions.items()],
            return_exceptions=True,
        )
        evaluations = {}
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Evaluation task failed: {result}")
                continue
            submission_id, evaluation_result = result
            evaluations[submission_id] = evaluation_result

        logger.info(f"EDA server evaluation completed for {len(evaluations)} submissions")
        return evaluations

    def _transform_eda_response(self, eda_result: Dict, submission_id: str) -> Dict:
        """Transform EDA server response to expected format.

        result ERROR (fault "system": toolchain/bundle/infra, overall is null) is never the
        miner's score: it becomes a retryable failed evaluation. REJECTED (fault "miner")
        is a real 0 and carries the reason for the miner."""
        final_score = eda_result.get('final_score') or {}
        error = eda_result.get('error') or {}
        if not isinstance(error, dict):
            error = {'message': str(error)}
        if eda_result.get('result') == 'ERROR' or final_score.get('overall') is None:
            logger.error(f"EDA system error for {submission_id}: {error.get('code')} {error.get('message')}")
            return self._generate_fallback_evaluation(submission_id, evaluation_details={
                'status': 'eda_system_error', 'code': error.get('code'), 'stage': error.get('stage'),
                'message': error.get('message'), 'retryable': error.get('retryable'),
            })

        # Extract functionality score from verilator results
        verilator_results = eda_result.get('verilator_results', {})
        verilator_success = verilator_results.get('success', False)
        functionality_score = 0.0

        if verilator_success:
            verilator_inner_results = verilator_results.get('results', {})
            functionality_score = verilator_inner_results.get('functionality_score', 0.0)

        # Extract gate flags from final_score
        functional_gate = final_score.get('functional_gate', False)
        overall_gate = final_score.get('overall_gate', False)

        # Check if the submission passed the testbench (based on functional gate)
        passed_testbench = functional_gate and functionality_score > 0

        # Log gate status
        if not functional_gate or not overall_gate:
            logger.warning(f"Submission {submission_id} failed gates - functional_gate: {functional_gate}, overall_gate: {overall_gate}")

        # Build structured evaluation_details for miners to diagnose their submission
        openlane_results = eda_result.get('openlane_results', {})
        details: Dict[str, Any] = {}

        if verilator_success:
            inner = verilator_results.get('results', {})
            details['verilator'] = {
                'ipc': inner.get('ipc'),
                'total_instructions': inner.get('total_instructions'),
                'instructions_passed': inner.get('instructions_passed'),
            }
        else:
            details['verilator_error'] = verilator_results.get('error_message', '')
            raw_log = verilator_results.get('evaluator_log', '')
            try:
                log_data = json.loads(raw_log)
                details['verilator_build_log'] = log_data.get('log', raw_log)
            except Exception:
                details['verilator_build_log'] = raw_log

        if error:
            # Miner-fault rejection: tell the miner what failed and where
            details['error'] = {k: error.get(k) for k in ('code', 'stage', 'message', 'detail') if error.get(k)}

        openlane_success = openlane_results.get('success', False)
        if openlane_success:
            inner = openlane_results.get('results', {})
            details['openlane'] = {
                'area_um2': inner.get('area_um2'),
                'fmax_mhz': inner.get('fmax_mhz'),
                'wns_ns': inner.get('wns_ns'),
                'sdc_period_ns': inner.get('sdc_period_ns'),
            }
        else:
            details['openlane_error'] = openlane_results.get('error_message', '')
            details['openlane_log'] = openlane_results.get('logs', '')

        return {
            'overall_score': final_score.get('overall', 0.0),
            'functionality_score': final_score.get('func_score', 0.0),
            'area_score': final_score.get('area_score', 0.0),
            'delay_score': final_score.get('perf_score', 0.0),
            'power_score': final_score.get('power_score', 0.0),
            'passed_testbench': passed_testbench,
            'functional_gate': functional_gate,
            'overall_gate': overall_gate,
            'timeout_occurred': False,
            'evaluation_notes': (
                f"EDA evaluation for {submission_id} - Functionality: {float(functionality_score or 0):.2f}, "
                f"Overall: {float(final_score.get('overall') or 0):.2f}, Gates: func={functional_gate}, overall={overall_gate}"
                + (f", Rejected: {error.get('code')} ({error.get('stage')})" if error.get('code') else "")
            ),
            'evaluation_details': json.dumps(details),
        }

    def _generate_fallback_evaluation(self, submission_id: str, evaluation_details: Optional[Dict] = None, timeout_occurred: bool = False) -> Dict:
        """Generate fallback evaluation when EDA server fails - marks as FAILED"""
        logger.warning(f"Marking evaluation as FAILED for {submission_id}")

        if evaluation_details is None:
            evaluation_details = {'status': 'failed', 'error': 'EDA server unavailable or error occurred'}

        return {
            'overall_score': 0.0,
            'functionality_score': 0.0,
            'area_score': 0.0,
            'delay_score': 0.0,
            'power_score': 0.0,
            'passed_testbench': False,
            'functional_gate': False,
            'overall_gate': False,
            'timeout_occurred': timeout_occurred,
            'evaluation_notes': f"Evaluation FAILED for {submission_id} - EDA server unavailable or error occurred",
            'evaluation_details': json.dumps(evaluation_details),
        }

    def build_timeout_evaluation(self, submission_id: str, timeout_seconds: int) -> Dict:
        """Build a zero-score evaluation dict for a submission that timed out at the batch level"""
        return {
            'overall_score': 0.0,
            'functionality_score': 0.0,
            'area_score': 0.0,
            'delay_score': 0.0,
            'power_score': 0.0,
            'passed_testbench': False,
            'functional_gate': False,
            'overall_gate': False,
            'timeout_occurred': True,
            'evaluation_notes': f"Evaluation timed out for {submission_id} - batch processing exceeded {timeout_seconds}s time limit",
            'evaluation_details': json.dumps({'status': 'batch_timeout', 'error': f'Batch processing timed out after {timeout_seconds} seconds'}),
        }

    async def _dummy_evaluate_submissions(self, submissions: Dict[str, bytes]) -> Dict[str, Dict]:
        """Original dummy evaluation for testing"""
        evaluations = {}
        for submission_id in submissions.keys():
            import random
            evaluations[submission_id] = {
                'overall_score': 0.0,
                'functionality_score': 0.0,
                'area_score': 0.0,
                'delay_score': 0.0,
                'power_score': 0.0,
                'passed_testbench': False,
                'functional_gate': False,
                'overall_gate': False,
                'evaluation_notes': f"FAILED! Dummy evaluation for {submission_id}, There is an error in evaluation pipeline"
            }

        await asyncio.sleep(2)  # Simulate processing time
        return evaluations
    
    async def submit_all_evaluations(self, challenge_id: str, evaluations: Dict[str, Dict]) -> Dict[str, bool]:
        """Submit all evaluations in parallel"""
        logger.info(f"Submitting {len(evaluations)} evaluations")
        
        tasks = []
        for submission_id, evaluation in evaluations.items():
            task = self.submit_evaluation(challenge_id, submission_id, evaluation)
            tasks.append((submission_id, task))
        
        results = {}
        submission_results = await asyncio.gather(*[task for _, task in tasks], return_exceptions=True)
        
        for (submission_id, _), result in zip(tasks, submission_results):
            if isinstance(result, Exception):
                logger.error(f"Failed to submit evaluation for {submission_id}: {result}")
                results[submission_id] = False
            else:
                results[submission_id] = result
        
        successful = sum(1 for success in results.values() if success)
        logger.info(f"Successfully submitted {successful}/{len(evaluations)} evaluations")
        
        return results
    
    async def submit_evaluation(self, challenge_id: str, submission_id: str, evaluation: Dict) -> bool:
        """Submit evaluation for a single submission using form data - skip if evaluation failed"""
        # Check if evaluation failed - if so, skip submission
        if evaluation['overall_score'] == 'failed':
            logger.warning(f"Skipping submission of failed evaluation for {submission_id}")
            logger.info(f"Evaluation failed for {submission_id}, validator can retry this submission in next batch")
            return False  # Return False but don't treat as error

        max_retries = 3

        for attempt in range(max_retries):
            try:
                url = f"{self.api_url}/api/v1/challenges/{challenge_id}/submissions/{submission_id}/submit_score"

                # Create signature for authentication
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                message = f"{self.validator_hotkey}{timestamp}"
                signature = self.create_signature(message)

                # Authentication parameters go in query params
                params = {
                    'validator_hotkey': self.validator_hotkey,
                    'signature': signature,
                    'timestamp': timestamp
                }

                # Headers (no Content-Type needed for form data)
                headers = {
                    'X-Validator-Secret': self.validator_secret
                }

                # Truncate evaluation_details to 16KB to keep form data size bounded
                _EVAL_DETAILS_LIMIT = 16384
                evaluation_details = evaluation.get('evaluation_details', '')
                if len(evaluation_details) > _EVAL_DETAILS_LIMIT:
                    evaluation_details = evaluation_details[:_EVAL_DETAILS_LIMIT] + '...[truncated]'

                # Evaluation data as FORM DATA (not JSON)
                form_data = {
                    'overall_score': str(evaluation['overall_score']),
                    'functionality_score': str(evaluation['functionality_score']),
                    'area_score': str(evaluation['area_score']),
                    'delay_score': str(evaluation['delay_score']),
                    'power_score': str(evaluation['power_score']),
                    'passed_testbench': str(evaluation['passed_testbench']).lower(),
                    'functional_gate': str(evaluation.get('functional_gate', False)).lower(),
                    'overall_gate': str(evaluation.get('overall_gate', False)).lower(),
                    'timeout_occurred': str(evaluation.get('timeout_occurred', False)).lower(),
                    'evaluation_notes': evaluation.get('evaluation_notes', ''),
                    'evaluation_details': evaluation_details,
                }

                if attempt == 0:
                    logger.info(f"Submitting evaluation for {submission_id} as form data:")
                    logger.info(f"  Form data: {form_data}")
                else:
                    logger.info(f"Retry attempt {attempt + 1}/{max_retries} for {submission_id}")

                # Send as form data (not JSON)
                async with self.session.post(url, params=params, headers=headers, data=form_data) as response:
                    if response.status == 200:
                        result = await response.json()
                        logger.info(f"Successfully submitted evaluation for {submission_id}: score {evaluation['overall_score']}")
                        logger.info(f"Server response: {result}")
                        return True
                    else:
                        error_text = await response.text()
                        logger.error(f"Failed to submit evaluation for {submission_id}: {response.status} - {error_text}")

                        # Retry on server errors (500-599) or specific client errors
                        if response.status >= 500 and attempt < max_retries - 1:
                            wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                            logger.warning(f"Server error {response.status}, retrying in {wait_time}s...")
                            await asyncio.sleep(wait_time)
                            continue
                        else:
                            return False

            except asyncio.TimeoutError:
                logger.error(f"Timeout submitting evaluation for {submission_id} (attempt {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.info(f"Retrying after timeout in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    return False

            except Exception as e:
                logger.error(f"Error submitting evaluation for {submission_id} (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt == 0:  # Only log traceback on first attempt to reduce noise
                    import traceback
                    logger.error(f"Traceback: {traceback.format_exc()}")

                if attempt < max_retries - 1:
                    wait_time = 2 ** attempt
                    logger.info(f"Retrying after exception in {wait_time}s...")
                    await asyncio.sleep(wait_time)
                else:
                    return False

        logger.error(f"Failed to submit evaluation for {submission_id} after {max_retries} attempts")
        return False

    async def get_banned_coldkeys(self, challenge_id: str) -> Optional[Dict]:
        """Fetch banned coldkeys (permanent + this challenge's scoped bans) from the server.

        Returns the parsed JSON response on success, or None on failure. Caller
        should fall back to cached state when this returns None.
        """
        try:
            url = f"{self.api_url}/api/v1/challenges/{challenge_id}/banned_coldkeys"

            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            message = f"{self.validator_hotkey}{timestamp}"
            signature = self.create_signature(message)

            params = {
                'validator_hotkey': self.validator_hotkey,
                'signature': signature,
                'timestamp': timestamp,
            }
            headers = {
                'X-Validator-Secret': self.validator_secret,
            }

            async with self.session.get(url, headers=headers, params=params, timeout=15) as response:
                if response.status == 200:
                    data = await response.json()
                    count = data.get('count', len(data.get('bans', [])))
                    logger.info(f"Fetched {count} banned coldkeys for {challenge_id}")
                    return data
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to fetch banned coldkeys: {response.status} - {error_text}")
                    return None
        except Exception as e:
            logger.error(f"Error fetching banned coldkeys for {challenge_id}: {e}")
            return None

    async def download_test_cases(self, challenge_id: str) -> bool:
        """Download and extract test cases for a challenge"""
        try:
            logger.info(f"Downloading test cases for challenge {challenge_id}")
            
            url = f"{self.api_url}/api/v1/challenges/{challenge_id}/test_cases/download"
            
            # Create signature for authentication
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            message = f"{self.validator_hotkey}{timestamp}"
            signature = self.create_signature(message)
            
            headers = {
                'X-Validator-Secret': self.validator_secret
            }
            
            params = {
                'validator_hotkey': self.validator_hotkey,
                'signature': signature,
                'timestamp': timestamp
            }
            
            logger.info(f"Requesting test cases from: {url}")
            
            async with self.session.get(url, headers=headers, params=params, timeout=240) as response:
                if response.status == 200:
                    content = await response.read()
                    logger.info(f"Downloaded test cases: {len(content)} bytes")
                    
                    # Create test cases directory
                    testcases_dir = self.base_dir / 'testcases'
                    testcases_dir.mkdir(exist_ok=True)
                    
                    # Save the zip file
                    zip_path = testcases_dir / f"{challenge_id}_validator.zip"
                    async with aiofiles.open(zip_path, 'wb') as f:
                        await f.write(content)
                    
                    return True
                    
                else:
                    error_text = await response.text()
                    logger.error(f"Failed to download test cases: {response.status} - {error_text}")
                    return False
                    
        except Exception as e:
            logger.error(f"Error downloading test cases for {challenge_id}: {e}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    def get_testcase_files(self, challenge_id: str) -> tuple:
        """Get test case files for a challenge"""
        evaluator_zip_path = self.base_dir / 'testcases' / f"{challenge_id}_validator.zip"
        return evaluator_zip_path

    def check_testcase_files_exist(self, challenge_id: str) -> bool:
        """Check if all required test case files exist for a challenge"""
        try:
            evaluator_zip_path = self.base_dir / 'testcases' / f"{challenge_id}_validator.zip"
            
            if not evaluator_zip_path.exists():
                logger.warning(f"Missing test case file: {evaluator_zip_path}")
                return False
            
            logger.debug(f"All test case files exist for challenge {challenge_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error checking test case files for {challenge_id}: {e}")
            return False