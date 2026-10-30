#!/usr/bin/env python3
# neurons/validator_utils/validator_state.py
"""
Validator State Management for ChipForge Validator
Manages validator state and persistence
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Optional
from dotenv import load_dotenv
load_dotenv()

from .storage import atomic_write_json, data_path, load_json

logger = logging.getLogger(__name__)


class ValidatorState:
    """Manages validator state and persistence"""
    
    def __init__(self, state_file: str = "validator_state.json"):
        self.state_file = data_path(state_file)
        self.current_batch_id: Optional[str] = None
        self.evaluation_in_progress: bool = False
        self.last_challenge_id: Optional[str] = None
        # Insertion-ordered (dict keys), so trimming keeps the most recent batch ids
        self.evaluated_batches: Dict[str, None] = {}
        self.current_challenge_expires_at: Optional[datetime] = None
        self.ban_emissions: bool = False  # Emergency emissions ban flag
        self.batch_download_window_seconds: int = 0  # 0 means use hardcoded fallback
        self.batch_evaluation_window_seconds: int = 0  # 0 means use hardcoded fallback

        self.load_state()
    
    def load_state(self):
        """Load state from file"""
        try:
            data = load_json(self.state_file)
            if data is not None:
                self.current_batch_id = data.get('current_batch_id')
                self.evaluation_in_progress = data.get('evaluation_in_progress', False)
                self.last_challenge_id = data.get('last_challenge_id')
                self.evaluated_batches = dict.fromkeys(data.get('evaluated_batches', []))

                self.current_challenge_expires_at = data.get('current_challenge_expires_at', None)
                if self.current_challenge_expires_at:
                    self.current_challenge_expires_at = datetime.fromisoformat(self.current_challenge_expires_at)

                self.ban_emissions = data.get('ban_emissions', False)
                self.batch_download_window_seconds = data.get('batch_download_window_seconds', 0)
                self.batch_evaluation_window_seconds = data.get('batch_evaluation_window_seconds', 0)

                logger.info(f"Loaded validator state: challenge={self.last_challenge_id}, ban_emissions={self.ban_emissions}")
                    
        except Exception as e:
            logger.error(f"Error loading validator state: {e}")
    
    def save_state(self):
        """Save state to file"""
        try:
            data = {
                'current_batch_id': self.current_batch_id,
                'evaluation_in_progress': self.evaluation_in_progress,
                'last_challenge_id': self.last_challenge_id,
                'evaluated_batches': list(self.evaluated_batches),
                'current_challenge_expires_at': self.current_challenge_expires_at.isoformat() if self.current_challenge_expires_at else None,
                'ban_emissions': self.ban_emissions,
                'batch_download_window_seconds': self.batch_download_window_seconds,
                'batch_evaluation_window_seconds': self.batch_evaluation_window_seconds,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            }
            atomic_write_json(self.state_file, data)
        except Exception as e:
            logger.error(f"Error saving validator state: {e}")

    def mark_batch_evaluated(self, batch_id: str, keep: int = 200):
        """Remember a processed batch id (most recent `keep` ids are kept)."""
        self.evaluated_batches.pop(batch_id, None)
        self.evaluated_batches[batch_id] = None
        while len(self.evaluated_batches) > keep:
            self.evaluated_batches.pop(next(iter(self.evaluated_batches)))
    
    def update_batch_windows(self, challenge_info: Optional[Dict]) -> bool:
        """Adopt the server's batch download/evaluation windows. Returns True if changed."""
        if not challenge_info:
            return False
        changed = False
        for key in ('batch_download_window_seconds', 'batch_evaluation_window_seconds'):
            value = challenge_info.get(key)
            if value is not None and value != getattr(self, key):
                logger.info(f"{key} updated: {getattr(self, key)} -> {value}")
                setattr(self, key, value)
                changed = True
        if changed:
            self.save_state()
        return changed
