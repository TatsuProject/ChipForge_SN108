#!/usr/bin/env python3
# neurons/validator_utils/status_reporter.py
"""What this validator is doing, told to the challenge server (POST /validator/status) and logged once per
change. The website's validators page shows it (never which design is being evaluated), and the server
gives work only to validators whose EDA server has the evaluator in use ready.

States: starting, idle, eda_offline, waiting_for_evaluator, downloading_evaluator, installing_evaluator,
install_failed, downloading_submission, evaluating, submitting_result."""

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from chipforge import __version__ as NEURON_VERSION
except Exception:                                   # run outside the repo root
    NEURON_VERSION = None


class StatusReporter:
    def __init__(self, api_client, every_seconds: Optional[float] = None):
        self.api_client = api_client
        self.every = every_seconds or float(os.getenv("STATUS_REPORT_SECONDS", "60"))
        self.state, self.detail, self.submission_id = "starting", None, None
        self.started_at: Optional[datetime] = None
        self.evaluator_sha256: Optional[str] = None
        self.evaluator_ready = False
        self.target_sha256: Optional[str] = None
        self.eda_ready = False
        self.eda_mock = False
        self._dirty = True
        self._sent_at = 0.0
        self._supported = True
        self._lock = asyncio.Lock()

    def set(self, state: str, detail: Optional[str] = None, submission_id: Optional[str] = None,
            started_at: Optional[datetime] = None) -> None:
        """Change the state (logged once per change). started_at: when the evaluation or install began
        (kept while the state stays the same)."""
        if (state, detail, submission_id) == (self.state, self.detail, self.submission_id):
            return
        if state != self.state or submission_id != self.submission_id:
            self.started_at = started_at or (datetime.now(timezone.utc)
                                             if state in ("evaluating", "downloading_evaluator", "installing_evaluator")
                                             else None)
        logger.info(f"Status: {state}" + (f" ({detail})" if detail else "")
                    + (f" - {submission_id}" if submission_id else ""))
        self.state, self.detail, self.submission_id = state, detail, submission_id
        self._dirty = True

    def evaluator(self, *, sha256: Optional[str], ready: bool, target: Optional[str], eda_ready: bool,
                  mock: bool = False) -> None:
        values = (sha256, bool(ready), target, bool(eda_ready), bool(mock))
        if values != (self.evaluator_sha256, self.evaluator_ready, self.target_sha256, self.eda_ready, self.eda_mock):
            (self.evaluator_sha256, self.evaluator_ready, self.target_sha256, self.eda_ready, self.eda_mock) = values
            self._dirty = True

    def payload(self) -> dict:
        return {"state": self.state, "detail": (self.detail or None) and str(self.detail)[:200],
                "evaluator_sha256": self.evaluator_sha256, "evaluator_ready": self.evaluator_ready,
                "target_sha256": self.target_sha256, "eda_ready": self.eda_ready, "eda_mock": self.eda_mock,
                "submission_id": self.submission_id,
                "started_at": self.started_at.isoformat() if self.started_at else None,
                "neuron_version": NEURON_VERSION}

    async def flush(self, force: bool = False) -> bool:
        """Send the status if it changed or every `every` seconds. Never raises."""
        if not self._supported:
            return False
        if not (force or self._dirty or time.monotonic() - self._sent_at >= self.every):
            return False
        async with self._lock:
            try:
                status = await self.api_client.report_status(self.payload())
            except Exception as e:
                logger.debug(f"Status report failed: {e}")
                return False
            if status == 404:
                logger.info("Challenge server has no /validator/status - status reports off")
                self._supported = False
                return False
            if status == 200:
                self._dirty, self._sent_at = False, time.monotonic()
                return True
            if status is not None:
                logger.debug(f"Status report: HTTP {status}")
            return False

    async def loop(self, stop: asyncio.Event, tick: float = 5.0) -> None:
        """Report changes within `tick` seconds and the current status every `every` seconds."""
        while not stop.is_set():
            await self.flush()
            try:
                await asyncio.wait_for(stop.wait(), timeout=tick)
            except asyncio.TimeoutError:
                pass
