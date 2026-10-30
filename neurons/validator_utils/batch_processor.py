#!/usr/bin/env python3
# neurons/validator_utils/batch_processor.py
"""
Batch Processor for ChipForge Validator: download a batch's designs, evaluate them on the EDA server with
the evaluator the batch names, and submit the results. The challenge server decides records from every
validator's results; the weights come from its record ladder (see reward_ladder.py).
"""

import json
import logging
import traceback
from typing import Dict, Optional

from .api_client import parse_server_time
from .storage import data_path

logger = logging.getLogger(__name__)


def result_line(ev: Dict) -> str:
    """'PASSED S=6.0123', 'REJECTED (NPU_PORT_MISMATCH)', 'timeout', 'not scored (EDA_INCOMPLETE)'..."""
    status = ev.get('eval_status')
    if ev.get('timeout_occurred'):
        return "timeout"
    if status == 'PASSED' or (status is None and ev.get('functional_gate') and ev.get('overall_gate')):
        return f"PASSED S={float(ev.get('overall_score') or 0):.4f}"
    code = None
    try:
        details = json.loads(ev.get('evaluation_details') or '{}')
        code = details.get('code') or next((p.get('code') for p in details.get('problems') or [] if p.get('code')), None)
    except (ValueError, AttributeError):
        pass
    if status == 'REJECTED':
        return f"REJECTED ({code})" if code else "REJECTED"
    return f"not scored ({code or status or 'system fault'})"


class BatchProcessor:
    """Handles batch processing logic for the validator"""

    def __init__(self, api_client, state, status=None):
        self.api_client = api_client
        self.state = state
        self.status = status                     # StatusReporter (optional in tests)
        self.base_dir = data_path('validator_data')
        self.submissions_dir = self.base_dir / 'submissions'
        self.submissions_dir.mkdir(parents=True, exist_ok=True)

    def _status(self, state: str, detail: Optional[str] = None, submission_id: Optional[str] = None) -> None:
        if self.status is not None:
            self.status.set(state, detail, submission_id)

    async def process_batch(self, challenge_id: str, batch: Dict, evaluator_sha256: Optional[str] = None) -> bool:
        """Evaluate a batch (split mode: one assignment) and submit the results. True when done."""
        batch_id = batch['batch_id']
        listed = batch.get('submissions', [])
        one = listed[0]['submission_id'] if len(listed) == 1 else None
        try:
            self.state.evaluation_in_progress = True
            self.state.current_batch_id = batch_id
            self.state.save_state()

            self._status("downloading_submission", f"{len(listed)} design(s)" if not one else None, one)
            downloaded = await self.api_client.download_batch_submissions(challenge_id, batch)
            if not downloaded:
                ids = {s.get('submission_id') for s in listed}
                if ids and ids <= self.api_client.already_evaluated:
                    # Re-batched for other validators; this one already scored them: done, not a failure
                    logger.info(f"Batch {batch_id}: nothing left to evaluate for this validator")
                    self.state.mark_batch_evaluated(batch_id)
                    return True
                logger.warning(f"Batch {batch_id}: no design could be downloaded")
                return False

            self._status("evaluating", f"{len(downloaded)} design(s)" if not one else None, one)
            evaluations = await self.api_client.evaluate_submissions_with_eda_server(
                challenge_id, downloaded, deadline=parse_server_time(batch.get('evaluation_ends_at')),
                window_start=parse_server_time(batch.get('expires_at')), evaluator_sha256=evaluator_sha256)
            if not evaluations:
                logger.error(f"Batch {batch_id}: the EDA server returned no evaluation")
                return False

            self._status("submitting_result", None, one)
            submitted = await self.api_client.submit_all_evaluations(challenge_id, evaluations)
            uids = {s['submission_id']: s.get('miner_uid') for s in listed}
            for sid, ev in evaluations.items():
                uid = uids.get(sid)
                logger.info(f"Evaluated {sid} (UID {uid if uid is not None else '?'}): {result_line(ev)}"
                            + ("" if submitted.get(sid) else " - NOT accepted by the challenge server"))
            if not any(submitted.values()):
                logger.error(f"Batch {batch_id}: no result was accepted by the challenge server")
                return False

            self.state.mark_batch_evaluated(batch_id)
            return True

        except Exception as e:
            logger.error(f"Error processing batch {batch_id}: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
        finally:
            self.state.current_batch_id = None
            self.state.evaluation_in_progress = False
            self.state.save_state()
