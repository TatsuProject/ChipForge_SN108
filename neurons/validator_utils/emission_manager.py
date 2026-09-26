#!/usr/bin/env python3
# neurons/validator_utils/emission_manager.py
"""
Emission Manager for ChipForge Validator
Handles emission phases based on challenge lifecycle
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
from dotenv import load_dotenv
load_dotenv()

from .storage import atomic_write_json, data_path, load_json

logger = logging.getLogger(__name__)


class EmissionManager:
    """Manages emission phases based on challenge lifecycle"""
    
    def __init__(self, config_file: str = "emission_config.json", miner_emission_percentage: float = 100.0):
        self.config_file = config_file
        self.load_config(miner_emission_percentage=miner_emission_percentage)
        
        # State tracking with persistence
        self.subnet_start_time = None
        self.first_challenge_end_time = None
        self.winner_reward_start_time = None
        self.current_phase = "initial_burn"
        self.current_winner = None  # Add persistence for current winner
        self.current_winner_score = 0.0  # Track winner's score
        self.winner_qualified_baseline = 0.0  # Baseline snapshot when winner was crowned
        self.last_challenge_end_time = None  # Track when last challenge ended

        self.load_state()
    
    def load_config(self, miner_emission_percentage: float = 100.0):
        """Load timing configuration from env or defaults"""
        # Load local fallback value
        self.local_winner_reward_hours = float(os.getenv('TOTAL_HOURS_FOR_WINNER_REWARD', '0.5'))

        # Active value (can be updated from challenge server)
        self.total_hours_for_winner_reward = self.local_winner_reward_hours
        self.burn_after_winner_days = float(os.getenv('BURN_AFTER_WINNER_DAYS', '0'))

        # Percentage of emissions given to the winner miner (0-100); remainder is burned.
        # Value comes from --miner_emission_percentage CLI arg (hardcoded default: 100).
        self.miner_emission_percentage = max(0.0, min(100.0, miner_emission_percentage))

        # Convert hours to days for internal consistency
        self.initial_burn_days = self.total_hours_for_winner_reward / 24
        self.winner_reward_days = self.total_hours_for_winner_reward / 24

        logger.info(f"Emission config: Local fallback winner reward={self.local_winner_reward_hours}h, Active={self.total_hours_for_winner_reward}h, Miner emission={self.miner_emission_percentage}%")

    def update_winner_reward_hours_from_server(self, winner_reward_hours: Optional[float]):
        """
        Update winner reward hours from challenge server
        Falls back to local .env value if server value is invalid
        
        Args:
            winner_reward_hours: Hours from challenge server, or None if not available
        """
        if winner_reward_hours is not None and winner_reward_hours > 0:
            if self.total_hours_for_winner_reward != winner_reward_hours:
                logger.info(f"Updating winner reward hours: {self.total_hours_for_winner_reward}h → {winner_reward_hours}h (from challenge server)")
                self.total_hours_for_winner_reward = winner_reward_hours
                # Update derived values
                self.initial_burn_days = self.total_hours_for_winner_reward / 24
                self.winner_reward_days = self.total_hours_for_winner_reward / 24
                self.save_state()
            else:
                logger.debug(f"Winner reward hours unchanged: {winner_reward_hours}h")
        else:
            # Fallback to local value
            if self.total_hours_for_winner_reward != self.local_winner_reward_hours:
                logger.warning(f"Invalid winner reward hours from server ({winner_reward_hours}), using local fallback: {self.local_winner_reward_hours}h")
                self.total_hours_for_winner_reward = self.local_winner_reward_hours
                self.initial_burn_days = self.total_hours_for_winner_reward / 24
                self.winner_reward_days = self.total_hours_for_winner_reward / 24
                self.save_state()
    
    def load_state(self):
        """Load emission state from file"""
        try:
            data = load_json(data_path('emission_state.json'))
            if data is not None:
                self.subnet_start_time = data.get('subnet_start_time')
                self.first_challenge_end_time = data.get('first_challenge_end_time')
                self.winner_reward_start_time = data.get('winner_reward_start_time')
                self.current_phase = data.get('current_phase', 'initial_burn')
                self.current_winner = data.get('current_winner')
                self.current_winner_score = data.get('current_winner_score', 0.0)
                self.winner_qualified_baseline = data.get('winner_qualified_baseline', 0.0)
                self.last_challenge_end_time = data.get('last_challenge_end_time')
                    
                # Restore winner reward hours if saved
                saved_hours = data.get('total_hours_for_winner_reward')
                if saved_hours is not None:
                    self.total_hours_for_winner_reward = saved_hours
                    self.initial_burn_days = self.total_hours_for_winner_reward / 24
                    self.winner_reward_days = self.total_hours_for_winner_reward / 24
                    
                # Convert ISO strings back to datetime objects
                if self.subnet_start_time:
                    self.subnet_start_time = datetime.fromisoformat(self.subnet_start_time)
                if self.first_challenge_end_time:
                    self.first_challenge_end_time = datetime.fromisoformat(self.first_challenge_end_time)
                if self.winner_reward_start_time:
                    self.winner_reward_start_time = datetime.fromisoformat(self.winner_reward_start_time)
                if self.last_challenge_end_time:
                    self.last_challenge_end_time = datetime.fromisoformat(self.last_challenge_end_time)
                        
                logger.info(f"Loaded emission state: phase={self.current_phase}, winner={self.current_winner[:12] + '...' if self.current_winner else 'None'}, score={self.current_winner_score}, qualified_baseline={self.winner_qualified_baseline}, reward_hours={self.total_hours_for_winner_reward}h")
        except Exception as e:
            logger.error(f"Error loading emission state: {e}")
    
    def save_state(self):
        """Save emission state to file"""
        try:
            data = {
                'subnet_start_time': self.subnet_start_time.isoformat() if self.subnet_start_time else None,
                'first_challenge_end_time': self.first_challenge_end_time.isoformat() if self.first_challenge_end_time else None,
                'winner_reward_start_time': self.winner_reward_start_time.isoformat() if self.winner_reward_start_time else None,
                'current_phase': self.current_phase,
                'current_winner': self.current_winner,
                'current_winner_score': self.current_winner_score,
                'winner_qualified_baseline': self.winner_qualified_baseline,
                'last_challenge_end_time': self.last_challenge_end_time.isoformat() if self.last_challenge_end_time else None,
                'total_hours_for_winner_reward': self.total_hours_for_winner_reward,
                'updated_at': datetime.now(timezone.utc).isoformat()
            }
            atomic_write_json(data_path('emission_state.json'), data)
        except Exception as e:
            logger.error(f"Error saving emission state: {e}")
    
    def initialize_subnet(self):
        """Initialize subnet start time"""
        if not self.subnet_start_time:
            self.subnet_start_time = datetime.now(timezone.utc)
            self.current_phase = "initial_burn"
            self.save_state()
            logger.info("Initialized subnet start time")
    
    def mark_first_challenge_complete(self, winner_hotkey: str):
        """Mark first challenge as complete with winner"""
        self.first_challenge_end_time = datetime.now(timezone.utc)
        self.winner_reward_start_time = datetime.now(timezone.utc)
        self.current_phase = "winner_reward"
        self.current_winner = winner_hotkey
        self.last_challenge_end_time = datetime.now(timezone.utc)
        self.save_state()
        logger.info(f"First challenge completed, winner: {winner_hotkey[:12]}...")
    
    def update_winner(self, winner_hotkey: str, winner_score: float, qualified_baseline: float, winner_timestamp: Optional[datetime] = None):
        """
        Update current winner ONLY if it's a new winner or score improved.
        CAPTURES the baseline snapshot that the winner beat to qualify.

        Rules:
        1. Different hotkey → Reset timer, capture new baseline snapshot
        2. Same hotkey, higher score → Reset timer, capture new baseline snapshot
        3. Same hotkey, same/lower score → Do nothing (keep existing timer)

        Args:
            winner_hotkey: Hotkey of the winner
            winner_score: Winner's score
            qualified_baseline: The baseline score this winner beat to qualify
            winner_timestamp: When this winner was found (defaults to now)
        """
        if winner_timestamp is None:
            winner_timestamp = datetime.now(timezone.utc)

        # Check if this is actually a new winner or improvement
        is_new_winner = self.current_winner != winner_hotkey
        is_improvement = (self.current_winner == winner_hotkey and winner_score > self.current_winner_score)

        if is_new_winner:
            # Different hotkey - new winner
            logger.info(f"NEW WINNER: {winner_hotkey[:12]}... (score: {winner_score}, beat baseline: {qualified_baseline}) replaces {self.current_winner[:12] if self.current_winner else 'None'}... (score: {self.current_winner_score})")
            self.current_winner = winner_hotkey
            self.current_winner_score = winner_score
            self.winner_qualified_baseline = qualified_baseline  # Capture baseline snapshot
            self.winner_reward_start_time = winner_timestamp
            self.current_phase = "winner_reward"
            logger.info(f"Reward period starts at {winner_timestamp} for {self.total_hours_for_winner_reward}h")
            self.save_state()

        elif is_improvement:
            # Same winner improved their score - reset timer and update baseline
            logger.info(f"WINNER IMPROVED: {winner_hotkey[:12]}... score {self.current_winner_score} → {winner_score}, beat baseline: {qualified_baseline}")
            self.current_winner_score = winner_score
            self.winner_qualified_baseline = qualified_baseline  # Update baseline snapshot
            self.winner_reward_start_time = winner_timestamp
            self.current_phase = "winner_reward"
            logger.info(f"Reward timer RESET at {winner_timestamp} for {self.total_hours_for_winner_reward}h")
            self.save_state()

        else:
            # Same winner, same/lower score - do nothing
            logger.debug(f"Same winner {winner_hotkey[:12]}... with same/lower score ({winner_score} vs {self.current_winner_score}) - keeping existing timer")
            # Don't save state - no changes made
    
    def mark_challenge_ended(self):
        """Mark that current challenge has ended"""
        self.last_challenge_end_time = datetime.now(timezone.utc)
        self.save_state()
        logger.info("Challenge ended, marked in emission state")
    
    def recover_emission_state_after_crash(self, current_challenge_active: bool, validator_best_miner: Optional[Tuple[str, float]] = None):
        """Recover emission state after validator crash/restart"""
        now = datetime.now(timezone.utc)
        
        # Handle case where we have a winner in validator state but not in emission state
        if not self.current_winner and validator_best_miner and validator_best_miner[0]:
            logger.info(f"Found winner in validator state but not in emission state: {validator_best_miner[0][:12]}...")
            
            # If we have a last challenge end time, use it to determine reward period
            if self.last_challenge_end_time:
                time_since_challenge_end = now - self.last_challenge_end_time
                
                if time_since_challenge_end < timedelta(hours=self.total_hours_for_winner_reward):
                    # Start winner reward period from challenge end time
                    self.current_winner = validator_best_miner[0]
                    self.winner_reward_start_time = self.last_challenge_end_time
                    self.current_phase = "winner_reward"
                    remaining_time = timedelta(hours=self.total_hours_for_winner_reward) - time_since_challenge_end
                    logger.info(f"Crash recovery: Starting winner reward for {self.current_winner[:12]}... ({remaining_time.total_seconds()/3600:.1f}h remaining)")
                    self.save_state()
                    return
                else:
                    # Winner reward period would have expired
                    logger.info(f"Crash recovery: Winner reward period expired ({time_since_challenge_end.total_seconds()/3600:.1f}h since challenge end)")
                    self.current_phase = "burn_until_submission"
                    self.save_state()
                    return
            else:
                # No challenge end time, assume winner period expired
                logger.info("Crash recovery: No challenge end time, assuming winner period expired")
                self.current_phase = "burn_until_submission"
                self.save_state()
                return
        
        # If we have a winner and reward period info
        if self.current_winner and self.winner_reward_start_time:
            time_since_reward_start = now - self.winner_reward_start_time
            reward_period_remaining = timedelta(hours=self.total_hours_for_winner_reward) - time_since_reward_start
            
            if reward_period_remaining > timedelta(0):
                # Still in reward period
                self.current_phase = "winner_reward"
                logger.info(f"Crash recovery: Continuing winner reward for {self.current_winner[:12]}... ({reward_period_remaining.total_seconds()/3600:.1f}h remaining)")
                return
            else:
                logger.info(f"Crash recovery: Winner reward period has expired")
                # Clear the winner since period expired
                self.current_winner = None
                self.current_phase = "burn_until_submission"
                self.save_state()
                return
        
        # Check if there was a challenge that ended while we were down
        if self.last_challenge_end_time and not current_challenge_active:
            time_since_challenge_end = now - self.last_challenge_end_time
            
            if time_since_challenge_end < timedelta(hours=self.total_hours_for_winner_reward):
                # We're still in the post-challenge winner reward period
                if self.current_winner:
                    self.current_phase = "winner_reward"
                    remaining_time = timedelta(hours=self.total_hours_for_winner_reward) - time_since_challenge_end
                    logger.info(f"Crash recovery: Restoring winner {self.current_winner[:12]}... reward ({remaining_time.total_seconds()/3600:.1f}h remaining)")
                    return
            else:
                # Winner reward period has expired, should burn
                logger.info(f"Crash recovery: Winner reward period expired, will burn emissions")
                self.current_phase = "burn_until_submission"
                self.current_winner = None  # Clear expired winner
                self.save_state()
                return
        
        # Default case - determine phase based on current state
        if current_challenge_active:
            self.current_phase = "normal"
        else:
            self.current_phase = "burn_until_submission"
        
        self.save_state()
        logger.info(f"Crash recovery: Set phase to {self.current_phase}")

    
    def should_burn_emissions(self, best_miner_score: float = 0.0) -> bool:
        """Determine if emissions should be burned"""
        now = datetime.now(timezone.utc)
        
        if not self.subnet_start_time:
            logger.info("No subnet start time - burning emissions")
            return True
        
        # Phase 1: Initial burn period (X days from subnet start)
        if self.current_phase == "initial_burn":
            initial_period_end = self.subnet_start_time + timedelta(days=self.initial_burn_days)
            
            if now < initial_period_end:
                if best_miner_score > 0:
                    logger.info("Good submission found during initial burn period - transitioning to normal")
                    self.current_phase = "normal"
                    self.save_state()
                    return False
                logger.info("Initial burn period active - burning emissions")
                return True
            else:
                # Initial period ended
                if best_miner_score > 0:
                    logger.info("Initial burn period ended, good submission found - transitioning to normal")
                    self.current_phase = "normal"
                    self.save_state()
                    return False
                else:
                    logger.info("Initial burn period ended, no good submissions - burning until submission")
                    self.current_phase = "burn_until_submission"
                    self.save_state()
                    return True
        
        # Phase 2: Winner reward period
        elif self.current_phase == "winner_reward":
            if self.winner_reward_start_time:
                reward_period_end = self.winner_reward_start_time + timedelta(hours=self.total_hours_for_winner_reward)
                if now < reward_period_end:
                    remaining_hours = (reward_period_end - now).total_seconds() / 3600
                    logger.info(f"Winner {self.current_winner[:12] if self.current_winner else 'Unknown'}... (score: {self.current_winner_score}) reward period active - {remaining_hours:.1f}h remaining")
                    return False
                else:
                    # Winner period ended - ONLY clear winner, KEEP score for comparison
                    logger.info(f"Winner reward period EXPIRED for {self.current_winner[:12] if self.current_winner else 'Unknown'}... (score: {self.current_winner_score})")
                    old_winner = self.current_winner
                    self.current_winner = None
                    # DO NOT reset current_winner_score - keep it for future comparisons
                    
                    if best_miner_score > 0:
                        logger.info("Winner period ended, challenge still active - transitioning to normal")
                        self.current_phase = "normal"
                        self.save_state()
                        return False
                    else:
                        logger.info("Winner period ended, no new submissions - burning until submission")
                        self.current_phase = "burn_until_submission"
                        self.save_state()
                        return True
        
        # Phase 3: Burn until good submission
        elif self.current_phase == "burn_until_submission":
            if best_miner_score > 0:
                logger.info("Good submission found during burn phase - transitioning to normal")
                self.current_phase = "normal"
                self.save_state()
                return False
            logger.info("No good submissions found - burning emissions")
            return True
        
        # Phase 4: Normal operation
        elif self.current_phase == "normal":
            if best_miner_score > 0:
                # Log winner info if we have one
                if self.current_winner:
                    logger.info(f"Normal operation: Current winner {self.current_winner[:12]}... (no time limit during active challenge)")
                logger.info("Normal operation with submissions - not burning emissions")
                return False
            else:
                logger.info("Normal operation but no submissions - burning emissions")
                return True
        
        logger.info("Unknown phase - burning emissions as fallback")
        return True
    
    def get_reward_hotkey(self, current_best_hotkey: Optional[str] = None, current_best_score: float = 0.0) -> Optional[str]:
        """
        Get hotkey that should receive rewards.

        NEW LOGIC - No baseline re-checking:
        - If in winner_reward phase: Return current_winner (already qualified with their baseline snapshot)
        - If not: Only return hotkey if score BEATS our last rewarded score
        - Baseline checking happens ONLY during evaluation in batch_processor
        - Otherwise: Return None (burn)
        """
        if self.current_phase == "winner_reward" and self.current_winner:
            # Active winner reward period
            if self.winner_reward_start_time:
                now = datetime.now(timezone.utc)
                reward_period_end = self.winner_reward_start_time + timedelta(hours=self.total_hours_for_winner_reward)

                if now < reward_period_end:
                    # NO baseline check - winner already qualified when they beat their snapshot baseline
                    remaining = (reward_period_end - now).total_seconds() / 3600
                    logger.info(f"Winner reward active: {self.current_winner[:12]}... ({remaining:.2f}h remaining, score: {self.current_winner_score}, qualified_baseline: {self.winner_qualified_baseline})")
                    return self.current_winner
                else:
                    # Period expired
                    logger.info(f"Winner reward period expired in get_reward_hotkey")
                    self.current_winner = None
                    self.save_state()
                    return None

        # No active winner_reward phase
        # Only reward if new submission BEATS the last rewarded score
        # NOTE: Baseline checking already done in batch_processor during evaluation
        if current_best_hotkey:
            if current_best_score > self.current_winner_score:
                logger.info(f"New best found: {current_best_hotkey[:12]}... (score: {current_best_score}) beats last rewarded (score: {self.current_winner_score})")
                return current_best_hotkey
            else:
                # Same or worse than what we already rewarded
                if current_best_score == self.current_winner_score:
                    logger.debug(f"Same winner {current_best_hotkey[:12]}... (score: {current_best_score}) - already rewarded, now burning")
                else:
                    logger.debug(f"Hotkey {current_best_hotkey[:12]}... (score: {current_best_score}) worse than last rewarded ({self.current_winner_score}) - burning")
                return None

        return None