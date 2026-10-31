#!/usr/bin/env python3
# neurons/validator_utils/evaluator_manager.py
"""Keeps the evaluator the challenge server asks for (/validator/sync "evaluator") installed on this
validator's EDA server, without the operator doing anything.

When the server names an evaluator the EDA server does not have ready: download the signed evaluator zip
and its .sshsig from the challenge server (sha256 checked), install them with POST /v2/evaluator (the EDA
server checks the signature against the subnet owner's key), and follow the install phases (verify, fetch
assets, post-install, preflight) until it is ready or failed. Everything is reported through the
StatusReporter, so the website shows it. A failed install is retried after EVALUATOR_RETRY_MINUTES, or at
once when the server names another evaluator.

No evaluator from the server (Quest 1 test cases, an older server): the EDA server's active evaluator is used."""

import asyncio
import logging
import os
import time
from typing import Dict, Optional, Tuple

from .storage import data_path

logger = logging.getLogger(__name__)

IN_PROGRESS = ("queued", "accepted", "verify", "extract", "fetch_assets", "salt", "post_install", "preflight")
PHASE_TEXT = {"verify": "checking the signature", "extract": "unpacking", "fetch_assets": "fetching assets",
              "salt": "preparing", "post_install": "post-install steps", "preflight": "preflight checks",
              "queued": "queued", "accepted": "accepted"}


class EvaluatorManager:
    def __init__(self, api_client, status):
        self.api_client = api_client
        self.status = status
        self.dir = data_path('validator_data') / 'evaluators'
        self.dir.mkdir(parents=True, exist_ok=True)
        self.retry_seconds = float(os.getenv("EVALUATOR_RETRY_MINUTES", "15")) * 60
        self.install_timeout = float(os.getenv("EVALUATOR_INSTALL_TIMEOUT_MINUTES", "240")) * 60
        self.poll_seconds = float(os.getenv("EVALUATOR_POLL_SECONDS", "10"))
        self._task: Optional[asyncio.Task] = None
        self._installing: Optional[str] = None
        self._failed: Dict[str, Tuple[float, str]] = {}      # sha -> (when, code)
        self.mock = False

    @staticmethod
    def describe(target: Optional[dict]) -> str:
        if not target:
            return "the EDA server's evaluator"
        label = f" '{target['label']}'" if target.get('label') else ""
        return f"evaluator v{target.get('version')}{label} ({str(target.get('sha256'))[:12]})"

    async def ready_sha(self, target: Optional[dict]) -> Tuple[bool, Optional[str]]:
        """(EDA server reachable, sha256 of the evaluator to use if it is ready). Starts an install in the
        background when the target is missing; the caller asks for work only with a ready sha."""
        health = await self.api_client.eda_health()
        if health is None:
            self.status.evaluator(sha256=None, ready=False, target=(target or {}).get('sha256'), eda_ready=False)
            return False, None
        self.mock = bool(health.get('mock'))
        active = health.get('active_evaluator') if health.get('ready') else None
        if not target:
            self.status.evaluator(sha256=active, ready=active is not None, target=None, eda_ready=True, mock=self.mock)
            return True, active
        sha = target['sha256']
        if self._task is not None and not self._task.done():
            self.status.evaluator(sha256=sha, ready=False, target=sha, eda_ready=True, mock=self.mock)
            return True, None
        state = await self.api_client.eda_evaluator_state(sha)
        phase = (state or {}).get('phase')
        if phase == 'ready' and not health.get('ready'):
            # installed, but the EDA server's service or tools are not up (it says so in /v2/health)
            self.status.evaluator(sha256=sha, ready=False, target=sha, eda_ready=False, mock=self.mock)
            self.status.set("waiting_for_evaluator", "EDA server not ready")
            return True, None
        if phase == 'ready':
            if self._failed.pop(sha, None) or self._installing == sha:
                logger.info(f"{self.describe(target)} is ready on the EDA server")
            self._installing = None
            self.status.evaluator(sha256=sha, ready=True, target=sha, eda_ready=True, mock=self.mock)
            return True, sha
        self.status.evaluator(sha256=active, ready=False, target=sha, eda_ready=True, mock=self.mock)
        if phase in IN_PROGRESS:                          # installing already (e.g. `make start`)
            self.status.set("installing_evaluator", PHASE_TEXT.get(phase, phase))
            return True, None
        failed = self._failed.get(sha)
        if failed and time.monotonic() - failed[0] < self.retry_seconds:
            self.status.set("install_failed", failed[1])
            return True, None
        self._installing = sha
        self.status.set("downloading_evaluator", f"v{target.get('version')}")
        self._task = asyncio.create_task(self._install(dict(target)))
        return True, None

    async def _install(self, target: dict) -> bool:
        sha = target['sha256']
        what = self.describe(target)
        try:
            self.status.set("downloading_evaluator", f"v{target.get('version')}")
            logger.info(f"Installing {what}: downloading it from the challenge server")
            path = self.dir / f"{sha}.zip"
            if not await self.api_client.download_evaluator(target['zip_url'], path, sha,
                                                            size=target.get('size_bytes')):
                return self._fail(sha, "DOWNLOAD_FAILED", what)
            sig = await self.api_client.download_evaluator_signature(target['sshsig_url'])
            if not sig:
                return self._fail(sha, "SIGNATURE_DOWNLOAD_FAILED", what)
            self.status.set("installing_evaluator", "sending to the EDA server")
            ok, code = await self.api_client.eda_install_evaluator(path, sig, sha)
            if not ok:
                return self._fail(sha, code or "EDA_REFUSED_INSTALL", what)
            started = time.monotonic()
            while time.monotonic() - started < self.install_timeout:
                state = await self.api_client.eda_evaluator_state(sha)
                phase = (state or {}).get('phase')
                if phase == 'ready':
                    logger.info(f"{what} installed and ready ({(time.monotonic() - started) / 60:.1f} min)")
                    self._prune(keep=sha)
                    return True
                if phase == 'failed':
                    return self._fail(sha, (state or {}).get('error') or "INSTALL_FAILED", what)
                if phase:
                    self.status.set("installing_evaluator", PHASE_TEXT.get(phase, phase))
                await asyncio.sleep(self.poll_seconds)
            return self._fail(sha, "INSTALL_TIMEOUT", what)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception(f"Installing {what} failed: {e}")
            return self._fail(sha, "VALIDATOR_ERROR", what)

    def _fail(self, sha: str, code: str, what: str) -> bool:
        self._failed[sha] = (time.monotonic(), code)
        self.status.set("install_failed", code)
        logger.error(f"Installing {what} failed: {code}. Retrying in {self.retry_seconds / 60:.0f} min "
                     f"(EDA server install log: $CF_V2_ROOT/logs/install/{sha}.*)")
        return False

    def _prune(self, keep: str, max_files: int = 3) -> None:
        """Keep the downloaded zips of the last few evaluators (the EDA server has its own copy)."""
        files = sorted(self.dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[max_files:]:
            if p.stem != keep:
                p.unlink(missing_ok=True)

    async def close(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
