#!/usr/bin/env python3
# neurons/validator_utils/batch_processor.py
"""
Batch Processor for ChipForge Validator
Handles batch evaluation and processing logic
"""

import logging
import traceback
from typing import Dict
from dotenv import load_dotenv
load_dotenv()
from .api_client import parse_server_time
from .storage import data_path
from .weight_manager import WeightTarget

logger = logging.getLogger(__name__)


class BatchProcessor:
    """Handles batch processing logic for the validator"""
    
    def __init__(self, api_client, state, emission_manager, set_weight_target):
        self.api_client = api_client
        self.state = state
        self.emission_manager = emission_manager
        # Callable(WeightTarget): the validator applies it to the chain right after the batch
        self.set_weight_target = set_weight_target
        
        # Directories
        self.base_dir = data_path('validator_data')
        self.submissions_dir = self.base_dir / 'submissions'
        self.submissions_dir.mkdir(parents=True, exist_ok=True)
    
    def extract_hotkeys_from_filenames(self, batch_id: str, successful_submissions: dict) -> dict:
        """Extract hotkeys from downloaded filenames"""
        submission_hotkeys = {}
        batch_dir = self.submissions_dir / batch_id
        
        try:
            # Look for files in the batch directory
            if batch_dir.exists():
                for file_path in batch_dir.glob("*.zip"):
                    filename = file_path.name
                    # Parse filename: challenge_id__hotkey__submission_id__attempt__processing.zip
                    parts = filename.replace('.zip', '').split('__')
                    if len(parts) >= 3:
                        hotkey = parts[1]
                        submission_id = parts[2]
                        
                        if submission_id in successful_submissions:
                            submission_hotkeys[submission_id] = hotkey
                            logger.info(f"Extracted hotkey from filename: {submission_id} -> {hotkey[:12]}...")
        
        except Exception as e:
            logger.error(f"Error extracting hotkeys from filenames: {e}")
        
        return submission_hotkeys
    
    async def process_batch(self, challenge_id: str, batch: Dict) -> bool:
        """Process a complete batch evaluation with challenge-wide best score tracking"""
        batch_id = batch['batch_id']
        
        try:
            logger.info(f"Starting batch processing for {batch_id}")
            self.state.evaluation_in_progress = True
            self.state.current_batch_id = batch_id
            self.state.save_state()

            # CRITICAL: Fetch FRESH baseline BEFORE evaluation (it may have changed since last check)
            logger.info(f"Fetching fresh baseline score for challenge {challenge_id} before evaluation")
            try:
                challenge_info = await self.api_client.get_challenge_info(challenge_id, fresh=True)
                if challenge_info and 'winner_baseline_score' in challenge_info:
                    fresh_baseline = challenge_info['winner_baseline_score']
                    if self.state.winner_baseline_score != fresh_baseline:
                        logger.info(f"Baseline updated before evaluation: {self.state.winner_baseline_score} -> {fresh_baseline}")
                        self.state.winner_baseline_score = fresh_baseline
                        self.state.save_state()
                    else:
                        logger.info(f"Using baseline score: {fresh_baseline}")
                else:
                    logger.warning(f"Could not fetch fresh baseline, using cached: {self.state.winner_baseline_score}")

                # Keep batch windows in sync with the server
                self.state.update_batch_windows(challenge_info)
            except Exception as e:
                logger.error(f"Error fetching fresh baseline: {e}, using cached: {self.state.winner_baseline_score}")

            # Store baseline snapshot for this evaluation (captured BEFORE submitting scores)
            evaluation_baseline_snapshot = self.state.winner_baseline_score
            logger.info(f"Baseline snapshot for this evaluation: {evaluation_baseline_snapshot}")

            # Download submissions
            logger.info(f"Downloading submissions for batch {batch_id}")
            downloaded_submissions = await self.api_client.download_batch_submissions(challenge_id, batch)
            logger.info(f"Downloaded {len(downloaded_submissions)} submissions")

            if not downloaded_submissions:
                logger.warning(f"No submissions downloaded for batch {batch_id}")
                return False

            # Evaluate with EDA server, within the server's evaluation deadline for this batch
            logger.info(f"Evaluating {len(downloaded_submissions)} submissions with EDA server")
            evaluations = await self.api_client.evaluate_submissions_with_eda_server(
                challenge_id, downloaded_submissions, deadline=parse_server_time(batch.get('evaluation_ends_at'))
            )
            if not evaluations:
                logger.error(f"No evaluations received from EDA server")
                return False

            logger.info(f"Received {len(evaluations)} evaluations from EDA server")

            # Submit evaluations to challenge server (this may update baseline on server)
            logger.info(f"Submitting {len(evaluations)} evaluations to challenge server")
            submission_results = await self.api_client.submit_all_evaluations(challenge_id, evaluations)
            successful_submissions = {k: v for k, v in evaluations.items() if submission_results.get(k, False)}

            if not successful_submissions:
                logger.error("No evaluations were successfully submitted")
                return False

            logger.info(f"Successfully submitted {len(successful_submissions)} evaluations")

            # Miner hotkeys: from the batch entries (challenge server provides them), falling
            # back to parsing the downloaded filenames for older servers
            submission_hotkeys = {
                s['submission_id']: s['hotkey'] for s in batch.get('submissions', [])
                if s.get('hotkey') and s['submission_id'] in successful_submissions
            }
            missing = {k: v for k, v in successful_submissions.items() if k not in submission_hotkeys}
            if missing:
                submission_hotkeys.update(self.extract_hotkeys_from_filenames(batch_id, missing))

            current_best_hotkey, current_best_score = self.state.current_challenge_best

            logger.info(f"Current challenge best: {current_best_hotkey[:12] if current_best_hotkey else 'None'}... -> {current_best_score}")

            # Find if any submission in this batch beats the challenge-wide best AND baseline SNAPSHOT
            new_champion = None
            new_best_score = current_best_score

            for submission_id, eval_data in successful_submissions.items():
                hotkey = submission_hotkeys.get(submission_id)
                overall_score = eval_data.get('overall_score', 0)

                # GATE CHECK: Both functional_gate and overall_gate must be True to proceed
                functional_gate = eval_data.get('functional_gate', False)
                overall_gate = eval_data.get('overall_gate', False)

                if not functional_gate or not overall_gate:
                    logger.info(f"Submission {submission_id} ({hotkey[:12] if hotkey else 'unknown'}...) FAILED gates - functional_gate: {functional_gate}, overall_gate: {overall_gate} - skipping winner comparison")
                    continue

                # Check if score beats both current best AND baseline snapshot (from BEFORE submission)
                if hotkey and overall_score > new_best_score:
                    if overall_score > evaluation_baseline_snapshot:
                        new_best_score = overall_score
                        new_champion = hotkey
                        logger.info(f"New challenge champion found: {hotkey[:12]}... -> {overall_score} (beats previous: {current_best_score} and baseline snapshot: {evaluation_baseline_snapshot})")
                    else:
                        logger.info(f"Submission {hotkey[:12]}... score {overall_score} beats previous ({current_best_score}) but does NOT beat baseline snapshot ({evaluation_baseline_snapshot}) - not eligible for reward")

            # Update challenge-wide best if we found a new champion
            if new_champion:
                self.state.update_best_miner(challenge_id, new_champion, new_best_score)

                # Update current challenge best (separate tracking)
                self.state.current_challenge_best = (new_champion, new_best_score)

                # Update emission manager with new winner + baseline snapshot they qualified against
                self.emission_manager.update_winner(new_champion, new_best_score, evaluation_baseline_snapshot, winner_timestamp=None)
                
                # Reward the new champion. Bans, ban_emissions, the emission split and
                # "not registered" are all handled when the weights are built and submitted.
                self.set_weight_target(WeightTarget.winner(new_champion, f"new champion, score {new_best_score}"))

            else:
                # No new champion found - check emission management policy
                logger.info(f"No submissions beat challenge best of {current_best_score}")

                # Get reward hotkey from emission manager (NO baseline check - winner already qualified)
                reward_hotkey = self.emission_manager.get_reward_hotkey(current_best_hotkey, current_best_score)
                should_burn = self.emission_manager.should_burn_emissions(current_best_score)

                if reward_hotkey and not should_burn:
                    logger.info(f"Challenge active, winner {reward_hotkey[:12]}... taking reward until next good submission")
                    self.set_weight_target(WeightTarget.winner(reward_hotkey, "current winner"))
                else:
                    if self.emission_manager.current_winner:
                        logger.info("Challenge active, submissions found but winner reward period expired - burning emissions")
                    else:
                        logger.info("Challenge active, submissions checking but no qualified winner - burning emissions")
                    self.set_weight_target(WeightTarget.burn("no qualified winner"))

            # Mark batch as processed
            self.state.mark_batch_evaluated(batch_id)
            self.state.current_batch_id = None
            self.state.evaluation_in_progress = False
            self.state.save_state()
            
            logger.info(f"Successfully processed batch {batch_id}")
            return True
            
        except Exception as e:
            logger.error(f"Error processing batch {batch_id}: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            
            self.state.evaluation_in_progress = False
            self.state.save_state()
            return False