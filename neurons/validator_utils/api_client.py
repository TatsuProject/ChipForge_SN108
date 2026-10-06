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
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Dict, Optional, Any
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
        self.signature_mode = os.getenv("SIGNATURE_MODE", "both").lower()   # both | v2 | v1

        # /validator/sync cache (see _sync)
        self._sync_supported: Optional[bool] = None
        self._sync_state: Optional[Dict] = None
        # Submissions the server says this validator already evaluated (HTTP 409 on download)
        self.already_evaluated: set = set()
        self._sync_etag: Optional[str] = None
        self._sync_fetched_at = 0.0
        self._sync_checked_at = 0.0
        self._sync_ttl = 10
        self._pending_testcases_version: Dict[str, Optional[str]] = {}
        
        self.eda_max_concurrency = max(1, int(os.getenv("EDA_MAX_CONCURRENCY", "4")))
        # Seconds kept free before the server's evaluation deadline for submitting scores
        self.eda_deadline_buffer = int(os.getenv("EDA_DEADLINE_BUFFER_SECONDS", "90"))

        # Directories
        self.base_dir = data_path('validator_data')
        self.submissions_dir = self.base_dir / 'submissions'
        self.submissions_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Signed requests to the challenge server
    # ------------------------------------------------------------------

    def create_signature(self, message: str) -> str:
        """Sign with the validator hotkey (Bittensor native, sr25519), hex encoded."""
        return self.wallet.hotkey.sign(data=message).hex()

    def _auth(self, method: str, url: str, form: Optional[Dict] = None) -> tuple:
        """Query params + headers authenticating one request.

        SIGNATURE_MODE=both (default) sends the legacy v1 signature (query params, over
        f"{hotkey}{iso_ts}") AND the v2 headers. Servers that know v2 use it (it binds the
        signature to method, path, body and a single-use nonce); older servers ignore the
        headers and verify v1. Set SIGNATURE_MODE=v2 once every server you talk to has v2.
        """
        params = {'validator_hotkey': self.validator_hotkey}
        headers = {'X-Validator-Secret': self.validator_secret}
        if self.signature_mode in ('both', 'v1'):
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            params['signature'] = self.create_signature(f"{self.validator_hotkey}{ts}")
            params['timestamp'] = ts
        if self.signature_mode in ('both', 'v2'):
            ts = str(int(time.time()))
            nonce = secrets.token_hex(16)
            canonical = "&".join(f"{k}={v}" for k, v in sorted((form or {}).items()))
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            path = urlparse(url).path
            message = f"chipforge-sn108:v2:{method.upper()}:{path}:{self.validator_hotkey}:{ts}:{nonce}:{digest}"
            headers.update({
                'X-Signature-Version': '2',
                'X-Timestamp': ts,
                'X-Nonce': nonce,
                'X-Signature': self.create_signature(message),
            })
        return params, headers

    async def _signed(self, method: str, url: str, *, form: Optional[Dict] = None, timeout: float = 30,
                      attempts: int = 1, read: str = "text", extra_headers: Optional[Dict] = None):
        """Signed request with retries on network errors and 5xx (fresh signature and nonce
        per attempt). Returns (status, body, headers); status None = unreachable."""
        for attempt in range(attempts):
            params, headers = self._auth(method, url, form)
            if extra_headers:
                headers.update(extra_headers)
            try:
                async with self.session.request(method, url, params=params, headers=headers, data=form,
                                                timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                    body = await (response.read() if read == "bytes" else response.text())
                    if response.status >= 500 and attempt < attempts - 1:
                        logger.warning(f"{method} {urlparse(url).path}: HTTP {response.status}, retrying")
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return response.status, body, response.headers
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                logger.warning(f"{method} {urlparse(url).path} failed (attempt {attempt + 1}/{attempts}): {e!r}")
                if attempt < attempts - 1:
                    await asyncio.sleep(2 ** attempt)
        return None, None, None

    # ------------------------------------------------------------------
    # /validator/sync (one cached call instead of separate polls)
    # ------------------------------------------------------------------

    async def _sync(self, fresh: bool = False) -> Optional[Dict]:
        """Cached /validator/sync state, or None when the server doesn't offer it (older
        servers answer 404; re-checked hourly) or it failed - callers then use the
        individual endpoints."""
        now = time.monotonic()
        if self._sync_supported is False:
            if now - self._sync_checked_at < 3600:
                return None
            self._sync_supported = None
        if not fresh and self._sync_state is not None and now - self._sync_fetched_at < self._sync_ttl:
            return self._sync_state

        extra = {'If-None-Match': self._sync_etag} if (self._sync_etag and self._sync_state) else None
        status, body, headers = await self._signed("GET", f"{self.api_url}/api/v1/validator/sync",
                                                   timeout=15, extra_headers=extra)
        self._sync_checked_at = now
        if status == 404:
            if self._sync_supported is not False:
                logger.info("Challenge server has no /validator/sync - using individual endpoints")
            self._sync_supported = False
            return None
        if status == 304 and self._sync_state is not None:
            self._sync_fetched_at = now
            return self._sync_state
        if status == 200:
            try:
                state = json.loads(body)
            except ValueError:
                return None
            self._sync_supported = True
            self._sync_state, self._sync_fetched_at = state, now
            self._sync_etag = headers.get('ETag')
            self._sync_ttl = max(5, min(60, int(state.get('next_poll_seconds') or 10)))
            return state
        if status is not None:
            logger.error(f"/validator/sync: HTTP {status} {str(body)[:200]}")
        return None

    def _testcases_version_file(self, challenge_id: str) -> Path:
        return self.base_dir / 'testcases' / f"{challenge_id}.version"

    def _needs_testcases(self, challenge_id: str, version: Optional[str], flag: bool) -> bool:
        """Download test cases only when their version changed (sync) or, on older servers,
        at most every 10 minutes while the server's download flag is set."""
        zip_path = self.get_testcase_files(challenge_id)
        if not zip_path.exists():
            return True
        if version:
            vf = self._testcases_version_file(challenge_id)
            return not vf.exists() or vf.read_text().strip() != version
        return bool(flag) and (time.time() - zip_path.stat().st_mtime) > 600

    # ------------------------------------------------------------------
    # Challenge state
    # ------------------------------------------------------------------

    def server_miner_emission_percentage(self, challenge: Optional[Dict] = None):
        """(controlled, value): the challenge server's miner emission percentage from the last
        /validator/sync (or, on servers without sync, the active challenge). controlled=False
        when the server doesn't send one; value None means "not set on the server"."""
        if self._sync_state is not None and 'miner_emission_percentage' in self._sync_state:
            return True, self._sync_state['miner_emission_percentage']
        if isinstance(challenge, dict) and 'miner_emission_percentage' in challenge:
            return True, challenge['miner_emission_percentage']
        return False, None

    async def get_active_challenge(self) -> Optional[Dict]:
        """
        Get active challenge from server with connection error handling

        Returns:
            Dict: Challenge data if active challenge exists (includes winner_reward_hours)
            {"status": "no_active_challenge"}: Server accessible but no challenge (intentional)
        Raises ConnectionError when the server is unreachable.
        """
        state = await self._sync()
        if state is not None:
            return state['challenge'] or {"status": "no_active_challenge"}

        try:
            url = f"{self.api_url}/api/v1/challenges/active"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    logger.debug(f"No active challenge found: HTTP {response.status}")
                    return {"status": "None"}
                challenge = await response.json()
                if isinstance(challenge, dict) and challenge.get('status') == 'no_active_challenge':
                    return {"status": "no_active_challenge"}
                if not isinstance(challenge, dict) or 'challenge_id' not in challenge:
                    logger.warning(f"Invalid challenge response format: {challenge}")
                    return {"status": "None"}
                if 'winner_reward_hours' not in challenge:
                    logger.warning(f"Active challenge {challenge['challenge_id']} missing winner_reward_hours - will use local fallback")
                return challenge
        except asyncio.TimeoutError:
            raise ConnectionError("Challenge server timeout")
        except aiohttp.ClientError as e:
            raise ConnectionError(f"Challenge server unreachable: {e}")

    async def get_challenge_info(self, challenge_id: str, fresh: bool = False) -> Optional[Dict]:
        """Remaining time, baseline, ban_emissions, batch windows and whether new test cases
        must be downloaded. fresh=True bypasses the sync cache (e.g. right before evaluating)."""
        state = await self._sync(fresh=fresh)
        challenge = state.get('challenge') if state else None
        if challenge and challenge.get('challenge_id') == challenge_id:
            result = {
                'winner_baseline_score': challenge.get('winner_baseline_score'),
                'ban_emissions': challenge.get('ban_emissions', False),
                # The margin a new winner needs (0 until the challenge's first winner); absent on older servers
                'min_improvement_percent': challenge.get('min_improvement_percent'),
                'batch_download_window_seconds': state['batch_windows']['download_seconds'],
                'batch_evaluation_window_seconds': state['batch_windows']['evaluation_seconds'],
            }
            expires = parse_server_time(challenge.get('expires_at'))
            if expires:
                result['remaining_time'] = max(0, (expires - datetime.now(timezone.utc)).total_seconds())
            tc = state.get('testcases') or {}
            if self._needs_testcases(challenge_id, tc.get('version'), tc.get('download_new_testcases')):
                result['download_new_testcases'] = True
            self._pending_testcases_version[challenge_id] = tc.get('version')
            return result

        try:
            url = f"{self.api_url}/api/v1/challenges/{challenge_id}/info"
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status != 200:
                    return None
                challenge = await response.json()
        except Exception as e:
            logger.error(f"Error getting challenge info: {e}")
            return None
        if not challenge:
            return None
        result = {}
        expires = parse_server_time(challenge.get('expires_at'))
        if expires:
            result['remaining_time'] = max(0, (expires - datetime.now(timezone.utc)).total_seconds())
        for key in ('winner_baseline_score', 'ban_emissions', 'batch_download_window_seconds',
                    'batch_evaluation_window_seconds'):
            if key in challenge:
                result[key] = challenge[key]
        if self._needs_testcases(challenge_id, None, challenge.get('download_new_testcases')):
            result['download_new_testcases'] = True
        return result

    async def get_current_batch(self, challenge_id: str) -> Optional[Dict]:
        """The batch currently exposed for evaluation (as this validator sees it), or None."""
        state = await self._sync()
        if state is not None and (state.get('challenge') or {}).get('challenge_id') == challenge_id:
            batch = state.get('batch')
            return batch if batch and batch.get('batch_id') else None

        status, body, _ = await self._signed(
            "GET", f"{self.api_url}/api/v1/challenges/{challenge_id}/batch/current", timeout=15)
        if status != 200:
            if status is not None:
                logger.debug(f"No current batch: HTTP {status}")
            return None
        batch = json.loads(body)
        if batch.get('batch_id'):
            logger.info(f"Found current batch: {batch['batch_id']} with {batch.get('available_submissions', 0)} submissions")
            return batch
        return None

    async def get_banned_coldkeys(self, challenge_id: str) -> Optional[Dict]:
        """Permanent + this challenge's bans ({"count", "bans": [...]}), or None on failure
        (the caller keeps its cached list)."""
        state = await self._sync()
        if state is not None and state.get('bans') is not None and \
                (state.get('challenge') or {}).get('challenge_id') == challenge_id:
            return {"challenge_id": challenge_id, **state['bans']}

        status, body, _ = await self._signed(
            "GET", f"{self.api_url}/api/v1/challenges/{challenge_id}/banned_coldkeys", timeout=15)
        if status != 200:
            logger.error(f"Failed to fetch banned coldkeys: HTTP {status} {str(body)[:200]}")
            return None
        data = json.loads(body)
        logger.info(f"Fetched {data.get('count', len(data.get('bans', [])))} banned coldkeys for {challenge_id}")
        return data

    # ------------------------------------------------------------------
    # Submissions, scores, test cases
    # ------------------------------------------------------------------

    async def download_submission(self, challenge_id: str, submission_id: str) -> Optional[Dict]:
        """Download one submission: {'content', 'filename', 'submission_id'} or None."""
        url = f"{self.api_url}/api/v1/challenges/{challenge_id}/submissions/{submission_id}/download"
        status, content, headers = await self._signed("GET", url, timeout=60, attempts=3, read="bytes")
        if status == 409:
            # A re-batched submission this validator already scored: nothing to do, not an error
            self.already_evaluated.add(submission_id)
            logger.info(f"Skipping {submission_id}: already evaluated by this validator")
            return None
        if status != 200:
            hint = {401: "authentication failed", 403: "not in the exposed batch / not permitted",
                    404: "submission not found or already fully evaluated"}.get(status, "")
            logger.error(f"Download of {submission_id} failed: HTTP {status} {hint}")
            return None
        filename = None
        disposition = headers.get('Content-Disposition', '')
        if 'filename=' in disposition:
            filename = disposition.split('filename=', 1)[1].split(';')[0].strip().strip('"')
        if not content.startswith(b'PK'):
            logger.warning(f"Downloaded {submission_id} does not look like a ZIP file")
        logger.info(f"Downloaded {submission_id}: {len(content)} bytes")
        return {'content': content, 'filename': filename or f"{submission_id}.zip", 'submission_id': submission_id}

    async def submit_evaluation(self, challenge_id: str, submission_id: str, evaluation: Dict) -> bool:
        """Submit one evaluation (form data). Retries network errors and 5xx."""
        _EVAL_DETAILS_LIMIT = 16384
        details = evaluation.get('evaluation_details') or ''
        if len(details) > _EVAL_DETAILS_LIMIT:
            details = details[:_EVAL_DETAILS_LIMIT] + '...[truncated]'
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
            'evaluation_details': details,
        }
        logger.debug(f"Submitting evaluation for {submission_id}: score={form_data['overall_score']}, "
                     f"gates={form_data['functional_gate']}/{form_data['overall_gate']}")
        url = f"{self.api_url}/api/v1/challenges/{challenge_id}/submissions/{submission_id}/submit_score"
        status, body, _ = await self._signed("POST", url, form=form_data, timeout=60, attempts=3)
        if status == 200:
            logger.info(f"Submitted evaluation for {submission_id}: score {evaluation['overall_score']}")
            return True
        logger.error(f"Failed to submit evaluation for {submission_id}: HTTP {status} {str(body)[:300]}")
        return False

    async def download_test_cases(self, challenge_id: str) -> bool:
        """Download the challenge's test cases (atomically replacing the local copy)."""
        logger.info(f"Downloading test cases for challenge {challenge_id}")
        url = f"{self.api_url}/api/v1/challenges/{challenge_id}/test_cases/download"
        status, content, _ = await self._signed("GET", url, timeout=240, attempts=2, read="bytes")
        if status != 200:
            logger.error(f"Failed to download test cases: HTTP {status}")
            return False
        zip_path = self.get_testcase_files(challenge_id)
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = zip_path.with_suffix('.zip.part')
        async with aiofiles.open(tmp, 'wb') as f:
            await f.write(content)
        os.replace(tmp, zip_path)
        version = self._pending_testcases_version.get(challenge_id)
        if version:
            self._testcases_version_file(challenge_id).write_text(version)
        logger.info(f"Downloaded test cases for {challenge_id}: {len(content)} bytes")
        return True

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