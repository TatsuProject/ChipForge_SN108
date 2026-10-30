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
from .logutil import info_on_change

logger = logging.getLogger(__name__)

_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
_EVALUATOR_PATH = re.compile(r"^/api/v1/challenges/[A-Za-z0-9_-]{1,128}/evaluator/[0-9a-f]{64}/(zip|sshsig)$")


def safe_filename(name: Optional[str], fallback: str) -> str:
    """A server-provided filename reduced to a plain basename; anything else -> fallback."""
    base = os.path.basename((name or "").strip().strip('"'))
    return base if base and base not in (".", "..") and _SAFE_FILENAME.match(base) else fallback


MAX_RESPONSE_BYTES = int(float(os.getenv("MAX_RESPONSE_MB", "100")) * 1024 * 1024)


class ResponseTooLarge(Exception):
    """A server answered with more than MAX_RESPONSE_MB: refused before it can exhaust memory."""


async def read_capped(response, limit: Optional[int] = None) -> bytes:
    """The response body, refusing more than `limit` bytes (MAX_RESPONSE_MB) whatever the server sends."""
    limit = limit or MAX_RESPONSE_BYTES
    length = getattr(response, "content_length", None)
    if isinstance(length, int) and length > limit:
        raise ResponseTooLarge(f"announced {length} bytes (limit {limit})")
    content = getattr(response, "content", None)
    if content is not None and hasattr(content, "iter_chunked"):
        buf = bytearray()
        async for chunk in content.iter_chunked(1 << 16):
            buf += chunk
            if len(buf) > limit:
                raise ResponseTooLarge(f"more than {limit} bytes")
        return bytes(buf)
    data = await response.read()
    if len(data) > limit:
        raise ResponseTooLarge(f"{len(data)} bytes (limit {limit})")
    return data


async def json_capped(response):
    """response.json() with the MAX_RESPONSE_MB limit."""
    if not hasattr(response, "read") and not hasattr(getattr(response, "content", None), "iter_chunked"):
        return await response.json()          # minimal test doubles
    return json.loads(await read_capped(response))


def path_component(value, what: str = "id") -> str:
    """A server-provided id used as one path component (directory or file name stem): plain characters
    only, never absolute, never '..'. Anything else is replaced by a hash, so a malicious or broken server
    can never make the validator write outside its data directory."""
    text = str(value or "")
    if re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", text) and text not in (".", ".."):
        return text
    safe = f"{what}-{hashlib.sha256(text.encode()).hexdigest()[:16]}"
    logger.warning(f"Unsafe {what} from the server ({text[:60]!r}); using {safe} as its path name")
    return safe


def check_chain_commit(content: bytes, listed: Optional[str], header: Optional[str]) -> tuple:
    """(chain_commit text or None, problem or None) for a downloaded design.

    The batch listing and the download header must carry the same text, and its package_sha256 must
    be the sha256 of the exact bytes received. Older servers send none: (None, None).
    """
    if listed and header and listed != header:
        return None, "chain_commit differs between the batch listing and the download"
    text = listed or header
    if not text:
        return None, None
    try:
        package_sha256 = json.loads(text)['package_sha256']
    except (ValueError, TypeError, KeyError):
        return None, "chain_commit is not valid cf-chain-commit/1 JSON"
    if package_sha256 != hashlib.sha256(content).hexdigest():
        return None, "zip sha256 differs from chain_commit package_sha256"
    return text, None


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
        # v2 = the npu-v2 evaluator (POST /v2/evaluate + /v2/status, chain_commit, score S); v1 = the retired
        # /evaluate of the npu-v1 EDA server (main branch), kept for older test setups
        self.eda_api = os.getenv("EDA_API", "v2").strip().lower()
        self.eda_mode = os.getenv("EDA_MODE", "T2").strip().upper()
        self.eda_poll_seconds = max(1.0, float(os.getenv("EDA_POLL_SECONDS", "30")))
        self._v2_evaluator: Optional[tuple] = None          # (sha256, fetched_at)
        self.use_dummy_evaluation = os.getenv("USE_DUMMY_EVALUATION", "false").lower() == "true"
        
        # Validator authentication
        self.validator_hotkey = self.wallet.hotkey.ss58_address
        self.signature_mode = os.getenv("SIGNATURE_MODE", "both").lower()   # both | v2 | v1

        # /validator/sync cache (see _sync)
        self._sync_supported: Optional[bool] = None
        self._sync_state: Optional[Dict] = None
        # Submissions the server says this validator already evaluated (HTTP 409 on download)
        self.already_evaluated: set = set()
        # submission_id -> cf-chain-commit/1 JSON text from the challenge server, passed to the v2
        # EDA server verbatim (every validator must send the identical object)
        self.chain_commits: Dict[str, str] = {}
        self._sync_etag: Optional[str] = None
        self._sync_fetched_at = 0.0
        self._sync_checked_at = 0.0
        self._sync_ttl = 10
        self._pending_testcases_version: Dict[str, Optional[str]] = {}
        
        self.eda_max_concurrency = max(1, int(os.getenv("EDA_MAX_CONCURRENCY", "4")))
        # Requests to the challenge server in progress at once (it refuses more than VALIDATOR_MAX_IN_FLIGHT)
        self._server_slots = asyncio.Semaphore(max(1, int(os.getenv("SERVER_MAX_PARALLEL", "4"))))
        # Seconds kept free before the server's evaluation deadline for submitting scores
        self.eda_deadline_buffer = int(os.getenv("EDA_DEADLINE_BUFFER_SECONDS", "90"))
        # Of an EDA request's budget, the EDA server gets all but this: a run stopped at its limit
        # then answers with where it stopped (EDA_TIMEOUT_<STAGE>) before this validator gives up.
        self.eda_answer_margin = 60
        # A submission whose EDA time limit would be shorter than this is not sent (it cannot finish)
        self.eda_min_limit = 120

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

    def _auth(self, method: str, url: str, form: Optional[Dict] = None, raw_body: Optional[bytes] = None) -> tuple:
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
            if raw_body is not None:                    # JSON body: the server hashes the raw bytes
                digest = hashlib.sha256(raw_body).hexdigest()
            else:
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
                      attempts: int = 1, read: str = "text", extra_headers: Optional[Dict] = None,
                      extra_params: Optional[Dict] = None, files: Optional[Dict[str, tuple]] = None,
                      json_body: Optional[Dict] = None):
        """Signed request with retries on network errors and 5xx (fresh signature and nonce
        per attempt). Returns (status, body, headers); status None = unreachable.
        files: {field: (filename, bytes, content type)} sent as multipart parts; the signature covers
        their filenames (as the server canonicalises file parts), so callers sign a hash of the content."""
        raw = json.dumps(json_body, separators=(",", ":")).encode() if json_body is not None else None
        async with self._server_slots:     # the server allows a few requests in progress per validator
            return await self._signed_attempts(method, url, form=form, timeout=timeout, attempts=attempts, read=read,
                                               extra_headers=extra_headers, extra_params=extra_params, files=files,
                                               raw=raw)

    async def _signed_attempts(self, method: str, url: str, *, form, timeout, attempts, read, extra_headers,
                               extra_params, files, raw):
        for attempt in range(attempts):
            signed_form = dict(form or {}, **{k: v[0] for k, v in (files or {}).items()}) if files else form
            params, headers = self._auth(method, url, signed_form, raw_body=raw)
            body = form
            if raw is not None:
                body = raw
                headers['Content-Type'] = 'application/json'
            if files:
                body = aiohttp.FormData()
                for k, v in (form or {}).items():
                    body.add_field(k, str(v))
                for k, (filename, content, ctype) in files.items():
                    body.add_field(k, content, filename=filename, content_type=ctype)
            if extra_params:
                params.update(extra_params)
            if extra_headers:
                headers.update(extra_headers)
            try:
                async with self.session.request(method, url, params=params, headers=headers, data=body,
                                                timeout=aiohttp.ClientTimeout(total=timeout)) as response:
                    payload = await read_capped(response)
                    body = payload if read == "bytes" else payload.decode("utf-8", errors="replace")
                    if response.status == 429 and attempt < attempts - 1:
                        # busy: too many requests in progress, or a rate limit; wait as told and retry
                        try:
                            wait = min(10.0, float(response.headers.get('Retry-After') or 1))
                        except ValueError:
                            wait = 1.0
                        await asyncio.sleep(wait + attempt)
                        continue
                    if response.status >= 500 and attempt < attempts - 1:
                        logger.warning(f"{method} {urlparse(url).path}: HTTP {response.status}, retrying")
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return response.status, body, response.headers
            except ResponseTooLarge as e:
                logger.error(f"{method} {urlparse(url).path}: response refused, too large ({e})")
                return None, None, None
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
        return self.base_dir / 'testcases' / f"{path_component(challenge_id, 'challenge')}.version"

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
                challenge = await json_capped(response)
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
                # Bumped by the subnet when it voids a record or changes the score to beat (absent on older servers)
                'baseline_epoch': challenge.get('baseline_epoch'),
                'current_winner': challenge.get('current_winner'),
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
                challenge = await json_capped(response)
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

    async def get_current_batch(self, challenge_id: str, claim: bool = False,
                                evaluator_sha256: Optional[str] = None) -> Optional[Dict]:
        """The batch currently exposed for evaluation (as this validator sees it), or None.

        Split evaluation mode: the challenge server hands each validator its own design (a batch of one).
        claim=True means this validator is idle and its EDA server is ready: when nothing is waiting for
        it, ask the server for work (an older server ignores the parameter). evaluator_sha256: the evaluator
        its EDA server has ready (the server gives work only with the evaluator in use ready)."""
        state = await self._sync()
        if state is not None and (state.get('challenge') or {}).get('challenge_id') == challenge_id:
            batch = state.get('batch')
            if batch and batch.get('batch_id'):
                return batch
            if not claim:
                return None

        status, body, _ = await self._signed(
            "GET", f"{self.api_url}/api/v1/challenges/{challenge_id}/batch/current", timeout=15,
            extra_params=({'claim': 'true', **({'evaluator_sha256': evaluator_sha256} if evaluator_sha256 else {})}
                          if claim else None))
        if status != 200:
            if status is not None:
                logger.debug(f"No current batch: HTTP {status}")
            return None
        batch = json.loads(body)
        if batch.get('batch_id'):
            logger.debug(f"Found current batch: {batch['batch_id']} with {batch.get('available_submissions', 0)} submissions")
            return batch
        return None

    async def heartbeat(self, batch_id: str) -> bool:
        """Split mode: tell the challenge server this validator is still evaluating its assignment."""
        status, _, _ = await self._signed(
            "POST", f"{self.api_url}/api/v1/validator/assignments/{batch_id}/heartbeat", timeout=15)
        return status == 200

    async def heartbeat_loop(self, batch_id: str, every_seconds: Optional[float] = None) -> None:
        """Heartbeats until cancelled (the evaluation finished). Missing heartbeats make the server hand the
        design to another validator, so the loop never stops on errors."""
        every = every_seconds or float(os.getenv("SPLIT_HEARTBEAT_SECONDS", "300"))
        while True:
            try:
                if not await self.heartbeat(batch_id):
                    logger.warning(f"Heartbeat for {batch_id} not accepted (assignment expired or reassigned)")
            except Exception as e:
                logger.warning(f"Heartbeat for {batch_id} failed: {e}")
            await asyncio.sleep(every)

    async def report_status(self, payload: Dict) -> Optional[int]:
        """POST /validator/status (see StatusReporter). Returns the HTTP status (None: unreachable)."""
        status, body, _ = await self._signed("POST", f"{self.api_url}/api/v1/validator/status", timeout=15,
                                             json_body=payload)
        if status not in (200, 404, None):
            logger.debug(f"/validator/status: HTTP {status} {str(body)[:200]}")
        return status

    # ------------------------------------------------------------------
    # Evaluator distribution (see EvaluatorManager)
    # ------------------------------------------------------------------

    @staticmethod
    def evaluator_path_ok(url_path: str, kind: str) -> bool:
        """Only this server's own evaluator download paths are followed (a path such as "@host/..." appended to
        the server URL would send the signed request, API key included, to another host)."""
        return bool(_EVALUATOR_PATH.match(url_path or "")) and url_path.endswith("/" + kind)

    async def download_evaluator(self, url_path: str, dest: Path, sha256: str, size: Optional[int] = None) -> bool:
        """Stream the signed evaluator zip to `dest` and check its sha256 (a cached file with the right hash
        is reused)."""
        if not self.evaluator_path_ok(url_path, "zip") or f"/{sha256}/" not in url_path:
            logger.error(f"Refusing evaluator download path {url_path!r}")
            return False
        if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == sha256:
            return True
        limit = int(os.getenv("MAX_EVALUATOR_MB", "300")) * 1024 * 1024
        url = f"{self.api_url}{url_path}"
        tmp = dest.with_suffix(".part")
        for attempt in range(3):
            params, headers = self._auth("GET", url)
            digest, got = hashlib.sha256(), 0
            try:
                async with self.session.get(url, params=params, headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=3600, sock_read=120)) as response:
                    if response.status != 200:
                        logger.error(f"Evaluator download: HTTP {response.status}")
                        if response.status < 500:
                            return False
                        raise aiohttp.ClientError(f"HTTP {response.status}")
                    async with aiofiles.open(tmp, 'wb') as f:
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            got += len(chunk)
                            if got > limit:
                                logger.error("Evaluator download larger than MAX_EVALUATOR_MB - refused")
                                tmp.unlink(missing_ok=True)
                                return False
                            digest.update(chunk)
                            await f.write(chunk)
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                logger.warning(f"Evaluator download failed (attempt {attempt + 1}/3): {e!r}")
                await asyncio.sleep(5 * (attempt + 1))
                continue
            if digest.hexdigest() != sha256:
                logger.error(f"Downloaded evaluator has sha256 {digest.hexdigest()[:16]}, expected {sha256[:16]} - discarded")
                tmp.unlink(missing_ok=True)
                return False
            os.replace(tmp, dest)
            logger.info(f"Downloaded evaluator {sha256[:12]} ({got / 1e6:.1f} MB)")
            return True
        return False

    async def download_evaluator_signature(self, url_path: str) -> Optional[bytes]:
        if not self.evaluator_path_ok(url_path, "sshsig"):
            logger.error(f"Refusing evaluator signature path {url_path!r}")
            return None
        status, body, _ = await self._signed("GET", f"{self.api_url}{url_path}", timeout=30, attempts=3, read="bytes")
        if status != 200 or not body:
            logger.error(f"Evaluator signature download: HTTP {status}")
            return None
        return body

    async def eda_health(self) -> Optional[Dict]:
        """GET /v2/health of the EDA server: its JSON (also while it is not ready), or None when unreachable."""
        try:
            async with self.session.get(f"{self.eda_server_url}/v2/health",
                                        timeout=aiohttp.ClientTimeout(total=15)) as response:
                body = await json_capped(response)
                return body if isinstance(body, dict) else {}
        except Exception as e:
            info_on_change(logger, "eda_health", f"EDA server {self.eda_server_url} unreachable: {e!r}")
            return None

    async def eda_evaluator_state(self, sha256: str) -> Optional[Dict]:
        """GET /v2/evaluator/<sha>: {phase, error, ...}; {} when the EDA server never saw it; None unreachable."""
        try:
            async with self.session.get(f"{self.eda_server_url}/v2/evaluator/{sha256}",
                                        timeout=aiohttp.ClientTimeout(total=15)) as response:
                if response.status == 404:
                    return {}
                body = await json_capped(response)
                return body if isinstance(body, dict) else {}
        except Exception as e:
            logger.debug(f"EDA evaluator state unavailable: {e!r}")
            return None

    async def eda_install_evaluator(self, zip_path: Path, signature: bytes, sha256: str) -> tuple:
        """POST /v2/evaluator (the EDA server verifies the signature, then installs). (accepted, error code)."""
        form = aiohttp.FormData()
        form.add_field('evaluator_zip', zip_path.read_bytes(), filename=zip_path.name, content_type='application/zip')
        form.add_field('evaluator_sig', signature, filename=zip_path.name + '.sshsig', content_type='text/plain')
        form.add_field('evaluator_sha256', sha256)
        headers = {}
        token = os.getenv("EDA_ADMIN_TOKEN", "").strip()
        if token:
            headers['Authorization'] = f"Bearer {token}"
        try:
            async with self.session.post(f"{self.eda_server_url}/v2/evaluator", data=form, headers=headers,
                                         timeout=aiohttp.ClientTimeout(total=600)) as response:
                body = await json_capped(response)
                if response.status in (200, 201, 202):
                    return True, None
                code = (body.get('error') or {}).get('code') if isinstance(body, dict) else None
                logger.error(f"EDA server refused the evaluator: HTTP {response.status} {str(body)[:300]}")
                return False, code or f"EDA_HTTP_{response.status}"
        except Exception as e:
            logger.error(f"Sending the evaluator to the EDA server failed: {e!r}")
            return False, "EDA_UNREACHABLE"

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
        logger.debug(f"Fetched {data.get('count', len(data.get('bans', [])))} banned coldkeys for {challenge_id}")
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
        logger.debug(f"Downloaded {submission_id}: {len(content)} bytes")
        return {'content': content, 'filename': filename or f"{submission_id}.zip", 'submission_id': submission_id,
                'chain_commit': headers.get('X-Chain-Commit')}

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
        # v2 evaluator: result status and projection hash, compared across validators by the server
        for key in ('eval_status', 'projection_hash', 'evaluator_sha256'):
            if evaluation.get(key):
                form_data[key] = str(evaluation[key])
        files = None
        report_gz = evaluation.get('public_report_gz')
        if report_gz:                       # v2: the EDA server's public report, gzipped, its sha256 signed
            form_data['public_report_sha256'] = hashlib.sha256(report_gz).hexdigest()
            files = {'public_report': ('public_report.json.gz', report_gz, 'application/gzip')}
        logger.debug(f"Submitting evaluation for {submission_id}: score={form_data['overall_score']}, "
                     f"gates={form_data['functional_gate']}/{form_data['overall_gate']}")
        url = f"{self.api_url}/api/v1/challenges/{challenge_id}/submissions/{submission_id}/submit_score"
        status, body, _ = await self._signed("POST", url, form=form_data, timeout=60, attempts=3, files=files)
        if status == 200:
            logger.debug(f"Submitted evaluation for {submission_id}: score {evaluation['overall_score']}")
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
        
        logger.debug(f"Downloading {len(submissions)} submissions in parallel")
        
        # Hash the miner signed, when the server provides it (challenge server >= 2026-09)
        expected_hash = {s['submission_id']: s.get('file_hash') for s in submissions}
        listed_commit = {s['submission_id']: s.get('chain_commit') for s in submissions}
        self.chain_commits = {k: v for k, v in self.chain_commits.items() if k in listed_commit}

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
                commit, problem = check_chain_commit(content, listed_commit.get(submission_id), result.get('chain_commit'))
                if problem:
                    # Never send or score a design whose bytes differ from its commitment
                    logger.error(f"Download of {submission_id}: {problem} - discarded")
                    continue
                if commit:
                    self.chain_commits[submission_id] = commit
                downloaded[submission_id] = content
                
                logger.debug(f"Successfully downloaded {submission_id}: {len(content)} bytes")
                
                # Save to local file using server-provided filename
                batch_dir = self.submissions_dir / path_component(batch['batch_id'], 'batch')
                batch_dir.mkdir(exist_ok=True)
                
                file_path = batch_dir / filename
                async with aiofiles.open(file_path, 'wb') as f:
                    await f.write(content)
                logger.debug(f"Saved {submission_id} as: {filename}")
            else:
                logger.warning(f"Invalid or empty result for {submission_id}")
        
        logger.debug(f"Successfully downloaded {len(downloaded)} submissions")
        return downloaded
    
    async def eda_server_ready(self) -> bool:
        """Pre-flight: is the EDA server reachable? Any HTTP answer counts (older gateways
        have no /health and answer 404); only a connection failure means unreachable."""
        if self.use_dummy_evaluation:
            return True
        if self.eda_api == "v2":
            return await self._v2_active_evaluator(fresh=True) is not None
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

    def _fair_limit(self, deadline: Optional[datetime], window_start: Optional[datetime]) -> Optional[float]:
        """The run time every submission of the batch is entitled to: the evaluation window (from the
        close of downloads to the deadline) minus the margins. An EDA run cut shorter than this (it waited
        for an earlier submission, or the batch was picked up late) is not held against the submission."""
        if deadline is None or window_start is None:
            return None
        fair = (deadline - window_start).total_seconds() - self.eda_deadline_buffer - self.eda_answer_margin
        return fair if fair > 0 else None

    async def evaluate_submissions_with_eda_server(self, challenge_id: str, submissions: Dict[str, bytes],
                                                   deadline: Optional[datetime] = None,
                                                   window_start: Optional[datetime] = None,
                                                   evaluator_sha256: Optional[str] = None) -> Dict[str, Dict]:
        """Evaluate submissions on the EDA server, at most EDA_MAX_CONCURRENCY at a time, each
        within the time left before the server's evaluation deadline. The EDA server is told that time
        (time_limit_s) and the time every submission of the batch is entitled to (fair_limit_s), so it
        stops a run on the batch's clock and says whose fault a timeout is."""
        logger.debug(f"Evaluating {len(submissions)} submissions with EDA server using test cases")

        if self.use_dummy_evaluation:
            return await self._dummy_evaluate_submissions(submissions)
        if self.eda_api == "v2":
            return await self._evaluate_v2(submissions, deadline, evaluator_sha256)

        evaluator_zip_path = self.get_testcase_files(challenge_id)
        if not evaluator_zip_path.exists():
            # Never score miners without test cases: evaluate nothing, the batch is skipped
            logger.error(f"Evaluator zip file not found: {evaluator_zip_path} - not evaluating")
            return {}
        evaluator_bytes = evaluator_zip_path.read_bytes()
        semaphore = asyncio.Semaphore(self.eda_max_concurrency)
        fair_limit = self._fair_limit(deadline, window_start)

        async def evaluate_single_submission(submission_id: str, submission_data: bytes) -> tuple:
            async with semaphore:
                budget = self._eda_time_budget(deadline)
                eda_limit = budget - self.eda_answer_margin
                if eda_limit < self.eda_min_limit:
                    return submission_id, self._generate_fallback_evaluation(
                        submission_id,
                        evaluation_details=self._timeout_details(
                            'VALIDATOR_NO_TIME_TO_START',
                            f"Not sent to the EDA server: only {max(0, budget):.0f}s of the batch's evaluation time "
                            "was left. Not a property of the submission: it will be evaluated again in a later batch.",
                            budget=budget),
                        timeout_occurred=True,
                    )
                form_data = aiohttp.FormData()
                form_data.add_field('design_zip', submission_data, filename=f'{submission_id}.zip',
                                    content_type='application/zip')
                form_data.add_field('evaluator_zip', evaluator_bytes, filename=f'{challenge_id}_validator.zip',
                                    content_type='application/zip')
                form_data.add_field('submission_id', submission_id)
                form_data.add_field('time_limit_s', f"{eda_limit:.0f}")
                if fair_limit is not None:
                    form_data.add_field('fair_limit_s', f"{fair_limit:.0f}")
                logger.info(f"Sending {submission_id} to EDA server ({len(submission_data)} bytes, "
                            f"time limit {eda_limit:.0f}s"
                            + (f", full window {fair_limit:.0f}s" if fair_limit is not None else "") + ")")
                try:
                    async with self.session.post(f"{self.eda_server_url}/evaluate", data=form_data,
                                                 timeout=aiohttp.ClientTimeout(total=budget)) as response:
                        if response.status == 200:
                            result = await json_capped(response)
                            logger.debug(f"EDA response for {submission_id}: {result}")
                            return submission_id, self._transform_eda_response(result, submission_id)
                        error_text = await response.text()
                        logger.error(f"EDA server error for {submission_id}: {response.status} - {error_text[:500]}")
                        return submission_id, self._generate_fallback_evaluation(
                            submission_id, evaluation_details={
                                'status': 'eda_http_error', 'code': 'VALIDATOR_EDA_HTTP_ERROR', 'where': 'validator',
                                'http_status': response.status,
                                'message': f"The EDA server answered HTTP {response.status}. Not a property of the "
                                           "submission: it will be evaluated again in a later batch."})
                except asyncio.TimeoutError:
                    logger.error(f"Timeout evaluating {submission_id} with EDA server after {budget:.0f}s")
                    return submission_id, self._generate_fallback_evaluation(
                        submission_id,
                        evaluation_details=self._timeout_details(
                            'VALIDATOR_EDA_NO_RESPONSE',
                            f"Timeout in the validator: the EDA server did not answer within the {budget:.0f}s left "
                            "of the batch's evaluation time. Not a property of the submission: it will be "
                            "evaluated again in a later batch.", budget=budget),
                        timeout_occurred=True,
                    )
                except Exception as e:
                    logger.error(f"Exception during EDA evaluation for {submission_id}: {e}")
                    return submission_id, self._generate_fallback_evaluation(
                        submission_id, evaluation_details={
                            'status': 'eda_unreachable', 'code': 'VALIDATOR_EDA_UNREACHABLE', 'where': 'validator',
                            'error': str(e)[:300],
                            'message': "The validator could not reach its EDA server. Not a property of the "
                                       "submission: it will be evaluated again in a later batch."})

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

        logger.debug(f"EDA server evaluation completed for {len(evaluations)} submissions")
        return evaluations

    @property
    def needs_testcases(self) -> bool:
        """v1 sends the challenge's test-case zip with every design; the v2 evaluator brings its own."""
        return not self.use_dummy_evaluation and self.eda_api != "v2"

    # ------------------------------------------------------------------
    # npu-v2 evaluator: POST /v2/evaluate, poll /v2/status
    # ------------------------------------------------------------------

    async def _v2_active_evaluator(self, fresh: bool = False) -> Optional[str]:
        """sha256 of the evaluator the EDA server runs (GET /v2/health), or None when it is not ready."""
        if not fresh and self._v2_evaluator and time.monotonic() - self._v2_evaluator[1] < 300:
            return self._v2_evaluator[0]
        try:
            async with self.session.get(f"{self.eda_server_url}/v2/health",
                                        timeout=aiohttp.ClientTimeout(total=15)) as response:
                health = await json_capped(response)
        except Exception as e:
            logger.error(f"EDA server {self.eda_server_url} unreachable: {e}")
            return None
        sha = health.get('active_evaluator') if isinstance(health, dict) else None
        if not (isinstance(health, dict) and health.get('ready') and isinstance(sha, str) and len(sha) == 64):
            logger.error(f"EDA server not ready: {str(health)[:300]}")
            return None
        if self._v2_evaluator is None or self._v2_evaluator[0] != sha:
            logger.info(f"EDA evaluator {sha[:16]}...{' (MOCK MODE)' if health.get('mock') else ''}")
        self._v2_evaluator = (sha, time.monotonic())
        return sha

    async def _evaluate_v2(self, submissions: Dict[str, bytes], deadline: Optional[datetime],
                           evaluator_sha256: Optional[str] = None) -> Dict[str, Dict]:
        semaphore = asyncio.Semaphore(self.eda_max_concurrency)

        async def one(submission_id: str, data: bytes) -> tuple:
            async with semaphore:
                return submission_id, await self._evaluate_v2_one(submission_id, data, deadline, evaluator_sha256)

        evaluations = {}
        for result in await asyncio.gather(*[one(s, d) for s, d in submissions.items()], return_exceptions=True):
            if isinstance(result, Exception):
                logger.error(f"Evaluation task failed: {result}")
                continue
            evaluations[result[0]] = result[1]
        return evaluations

    def _v2_failure(self, submission_id: str, code: str, message: str, **extra) -> Dict:
        return self._generate_fallback_evaluation(submission_id, evaluation_details={
            'status': 'eda_system_error', 'code': code, 'where': extra.pop('where', 'validator'),
            'message': message, **extra})

    async def _evaluate_v2_one(self, submission_id: str, data: bytes, deadline: Optional[datetime],
                               evaluator_sha256: Optional[str] = None) -> Dict:
        budget = self._eda_time_budget(deadline)
        if budget - self.eda_answer_margin < self.eda_min_limit:
            return self._generate_fallback_evaluation(submission_id, evaluation_details=self._timeout_details(
                'VALIDATOR_NO_TIME_TO_START', f"Not sent to the EDA server: only {max(0, budget):.0f}s of the "
                "evaluation time was left. Not a property of the submission.", budget=budget), timeout_occurred=True)
        chain_commit = self.chain_commits.get(submission_id)
        if not chain_commit and self.eda_mode in ("T1", "T2"):
            return self._v2_failure(submission_id, 'VALIDATOR_NO_CHAIN_COMMIT',
                                    "The challenge server sent no chain_commit for this design, so it cannot be "
                                    "evaluated in T1/T2. Not a property of the submission.")
        sha = evaluator_sha256 or await self._v2_active_evaluator()
        if sha is None:
            return self._v2_failure(submission_id, 'VALIDATOR_EDA_NOT_READY', "The validator's EDA server is not ready.")
        submitter = json.loads(chain_commit).get('hotkey') if chain_commit else None
        rid = re.sub(r"[^A-Za-z0-9._-]", "", f"{submission_id}-{self.validator_hotkey[:10]}")[:128]
        form = aiohttp.FormData()
        form.add_field('design_zip', data, filename=f'{submission_id}.zip', content_type='application/zip')
        form.add_field('mode', self.eda_mode)
        form.add_field('request_id', rid)
        form.add_field('evaluator_sha256', sha)
        if submitter:
            form.add_field('submitter', re.sub(r"[^A-Za-z0-9._-]", "", submitter)[:128])
        if chain_commit:
            form.add_field('chain_commit', chain_commit)
        started = time.monotonic()
        logger.debug(f"Sending {submission_id} to the EDA server ({len(data)} bytes, {self.eda_mode}, request {rid}, "
                     f"evaluator {sha[:12]}, up to {budget:.0f}s)")
        try:
            async with self.session.post(f"{self.eda_server_url}/v2/evaluate", data=form,
                                         timeout=aiohttp.ClientTimeout(total=min(900, max(60, budget)))) as response:
                body = await json_capped(response)
                if response.status not in (200, 202):
                    code = (body.get('error') or {}).get('code') if isinstance(body, dict) else None
                    return self._v2_failure(submission_id, code or 'VALIDATOR_EDA_HTTP_ERROR',
                                            f"The EDA server refused the evaluation (HTTP {response.status}). Not a "
                                            "property of the submission.", http_status=response.status,
                                            where='eda_server')
            while True:
                async with self.session.get(f"{self.eda_server_url}/v2/status/{rid}",
                                            timeout=aiohttp.ClientTimeout(total=60)) as response:
                    if response.status == 200:
                        result = await json_capped(response)
                        # the loader names the evaluator it ran in X-CF-Evaluator-Sha256 (the mock too)
                        ran = (response.headers.get('X-CF-Evaluator-Sha256') or '').strip().lower() or None
                        if ran and ran != sha:
                            # never report a result under an evaluator it was not produced with
                            return self._v2_failure(submission_id, 'VALIDATOR_EDA_WRONG_EVALUATOR',
                                                    f"The EDA server ran evaluator {str(ran)[:16]} instead of "
                                                    f"{sha[:16]}. Not a property of the submission.")
                        return {**self._transform_v2(result, submission_id), 'evaluator_sha256': ran or sha}
                    if response.status != 202:
                        return self._v2_failure(submission_id, 'VALIDATOR_EDA_HTTP_ERROR',
                                                f"The EDA server answered HTTP {response.status} for the status. Not a "
                                                "property of the submission.", http_status=response.status)
                if time.monotonic() - started + self.eda_poll_seconds > budget:
                    logger.error(f"{submission_id}: no EDA result within {budget:.0f}s")
                    return self._generate_fallback_evaluation(submission_id, evaluation_details=self._timeout_details(
                        'VALIDATOR_EDA_NO_RESULT', f"The evaluation did not finish within the {budget:.0f}s the "
                        "validator had for it.", budget=budget), timeout_occurred=True)
                await asyncio.sleep(self.eda_poll_seconds)
        except (ResponseTooLarge, ValueError) as e:
            return self._v2_failure(submission_id, 'VALIDATOR_EDA_BAD_RESPONSE', f"Unreadable EDA response: {e}")
        except asyncio.TimeoutError:
            return self._v2_failure(submission_id, 'VALIDATOR_EDA_NO_RESPONSE', "The EDA server did not answer in time.")
        except Exception as e:
            logger.error(f"Exception during EDA evaluation for {submission_id}: {e}")
            return self._v2_failure(submission_id, 'VALIDATOR_EDA_UNREACHABLE',
                                    "The validator could not reach its EDA server. Not a property of the submission.",
                                    error=str(e)[:300])

    @staticmethod
    def _v2_score(result: Dict) -> Optional[float]:
        report = result.get('public_report') or {}
        s = ((report.get('score') or {}).get('S') or {}).get('value')
        if s is None:
            s = (report.get('verdict') or {}).get('score_S')
        try:
            return float(s) if s is not None else None
        except (TypeError, ValueError):
            return None

    def _transform_v2(self, result: Dict, submission_id: str) -> Dict:
        """cf-response/1 -> the evaluation the challenge server takes. PASSED: the score is S, gates passed.
        REJECTED: a real 0 (the miner's fault), with the reason. INCOMPLETE / ERROR: a system fault (never
        the miner's score; the server hands the design to another validator)."""
        status = str(result.get('status') or '').upper()
        problems = [p for p in (result.get('problems') or []) if isinstance(p, dict)]
        report = result.get('public_report')
        report_gz = None
        if isinstance(report, dict):
            import gzip
            report_gz = gzip.compress(json.dumps(report, separators=(",", ":")).encode())
        details = {'status': status, 'tier_reached': result.get('tier_reached'), 'summary': result.get('summary'),
                   'problems': [{k: p.get(k) for k in ('code', 'fault', 'stage', 'public_reason') if p.get(k)}
                                for p in problems[:20]],
                   'evaluator_sha256': result.get('evaluator_sha256'), 'mock': bool(result.get('mock'))}
        common = {'eval_status': status if status in ('PASSED', 'REJECTED', 'INCOMPLETE', 'ERROR') else None,
                  'projection_hash': result.get('projection_sha256') or None, 'public_report_gz': report_gz}
        s = self._v2_score(result)
        if status == 'PASSED' and s is not None:
            return {'overall_score': s, 'functionality_score': 100.0, 'area_score': 0.0, 'delay_score': 0.0,
                    'power_score': 0.0, 'passed_testbench': True, 'functional_gate': True, 'overall_gate': True,
                    'timeout_occurred': False, 'evaluation_details': json.dumps(details),
                    'evaluation_notes': f"EDA v2 evaluation for {submission_id}: PASSED, S={s:.6f}", **common}
        if status == 'REJECTED':
            code = next((p.get('code') for p in problems if p.get('code')), None) or \
                ((report or {}).get('verdict') or {}).get('failure_code')
            return {'overall_score': 0.0, 'functionality_score': 0.0, 'area_score': 0.0, 'delay_score': 0.0,
                    'power_score': 0.0, 'passed_testbench': False, 'functional_gate': False, 'overall_gate': False,
                    'timeout_occurred': False, 'evaluation_details': json.dumps(details),
                    'evaluation_notes': f"EDA v2 evaluation for {submission_id}: REJECTED ({code})", **common}
        code = next((p.get('code') for p in problems if p.get('code')), None) or f"EDA_{status or 'UNKNOWN'}"
        failed = self._generate_fallback_evaluation(submission_id, evaluation_details={
            'status': 'eda_system_error', 'code': code, 'where': 'eda_server',
            'message': f"The evaluator ended {status or 'without a result'}; not a property of the submission.",
            **details})
        return {**failed, **common, 'eval_status': status if status in ('INCOMPLETE', 'ERROR') else 'ERROR'}

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
            details = {'status': 'eda_system_error', 'code': error.get('code'), 'stage': error.get('stage'),
                       'where': error.get('where') or 'eda_server', 'message': error.get('message'),
                       'retryable': error.get('retryable')}
            if error.get('timing'):
                details['timing'] = error['timing']
            return self._generate_fallback_evaluation(submission_id, evaluation_details=details)

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
            details['error'] = {k: error.get(k) for k in ('code', 'stage', 'where', 'message', 'detail', 'timing')
                                if error.get(k)}

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

        code, message = evaluation_details.get('code'), evaluation_details.get('message')
        notes = (f"Evaluation FAILED for {submission_id} - {code}: {message}" if code and message
                 else f"Evaluation FAILED for {submission_id} - EDA server unavailable or error occurred")
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
            'evaluation_notes': notes,
            'evaluation_details': json.dumps(evaluation_details),
        }

    @staticmethod
    def _timeout_details(code: str, message: str, budget: Optional[float] = None) -> Dict:
        """evaluation_details of a timeout on the validator's side (never the submission's fault)."""
        d = {'status': 'timeout', 'code': code, 'where': 'validator', 'message': message,
             'error': message, 'retryable': True}
        if budget is not None:
            d['timing'] = {'budget_s': round(max(0.0, budget), 1)}
        return d

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
            'evaluation_notes': (f"Evaluation timed out for {submission_id} - VALIDATOR_BATCH_TIMEOUT: the batch was not "
                                 f"finished within its {timeout_seconds:.0f}s"),
            'evaluation_details': json.dumps({
                'status': 'batch_timeout', 'code': 'VALIDATOR_BATCH_TIMEOUT', 'where': 'validator', 'retryable': True,
                'error': f'Batch processing timed out after {timeout_seconds:.0f} seconds',
                'message': (f"Timeout in the validator: the batch was not finished within its {timeout_seconds:.0f}s. "
                            "Not a property of the submission: it will be evaluated again in a later batch.")}),
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
        logger.debug(f"Submitting {len(evaluations)} evaluations")
        
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
        logger.debug(f"Successfully submitted {successful}/{len(evaluations)} evaluations")
        
        return results
    
    def get_testcase_files(self, challenge_id: str) -> tuple:
        """Get test case files for a challenge"""
        evaluator_zip_path = self.base_dir / 'testcases' / f"{path_component(challenge_id, 'challenge')}_validator.zip"
        return evaluator_zip_path

    def check_testcase_files_exist(self, challenge_id: str) -> bool:
        """Check if all required test case files exist for a challenge"""
        try:
            evaluator_zip_path = self.base_dir / 'testcases' / f"{path_component(challenge_id, 'challenge')}_validator.zip"
            
            if not evaluator_zip_path.exists():
                logger.warning(f"Missing test case file: {evaluator_zip_path}")
                return False
            
            logger.debug(f"All test case files exist for challenge {challenge_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error checking test case files for {challenge_id}: {e}")
            return False