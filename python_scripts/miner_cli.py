#!/usr/bin/env python3
"""
ChipForge Miner CLI Tool
Command-line interface for miners to check status, view submissions, download challenges, and submit solutions
"""

import os
import sys
import json
import argparse
import requests
import hashlib
import zipfile
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, List
import logging
import bittensor as bt
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SolutionSubmitter:
    """Handles solution submission to ChipForge challenge server"""
    
    def __init__(self, config):
        self.config = config
        self.wallet = bt.Wallet(config=config)
        self.api_url = config.api_url
        
        # Load private key for signatures
        self.miner_hotkey = self.wallet.hotkey.ss58_address
        
        logger.info(f"Solution Submitter initialized")
        logger.info(f"Miner hotkey: {self.miner_hotkey}")
        logger.info(f"Challenge API URL: {self.api_url}")
    
    def create_signature(self, message: str) -> str:
        """Create signature for message using bittensor wallet (native method)"""
        try:
            # Use bittensor's native signing method - sign the raw message, not a hash
            signature_bytes = self.wallet.hotkey.sign(data=message)
            signature_hex = signature_bytes.hex()
            
            logger.debug(f"Signing with bittensor wallet (native method):")
            logger.debug(f"  Message: {message}")
            logger.debug(f"  Message length: {len(message)}")
            logger.debug(f"  Signature: {signature_hex}")
            
            return signature_hex
            
        except Exception as e:
            logger.error(f"Error creating signature: {e}")
            raise
    
    def calculate_file_hash(self, file_path: Path) -> str:
        """Calculate SHA256 hash of file"""
        sha256_hash = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256_hash.update(chunk)
        return sha256_hash.hexdigest()
    
    def validate_solution_zip(self, zip_path: Path) -> bool:
        """Validate that the ZIP file exists and is valid"""
        try:
            if not zip_path.exists():
                logger.error(f"ZIP file not found: {zip_path}")
                return False
            
            if not zip_path.suffix.lower() == '.zip':
                logger.error(f"File must be a ZIP file: {zip_path}")
                return False
            
            # Check file size (10MB limit)
            MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB in bytes
            file_size = zip_path.stat().st_size
            
            if file_size > MAX_FILE_SIZE:
                size_mb = file_size / (1024 * 1024)
                logger.error(f"File size ({size_mb:.2f} MB) exceeds maximum allowed size (10 MB)")
                return False
            
            # Test if ZIP is valid
            with zipfile.ZipFile(zip_path, 'r') as zipf:
                file_list = zipf.namelist()
                logger.info(f"ZIP contains {len(file_list)} files")
                for filename in file_list[:5]:  # Show first 5 files
                    logger.debug(f"  {filename}")
                if len(file_list) > 5:
                    logger.debug(f"  ... and {len(file_list) - 5} more files")
            
            return True
            
        except Exception as e:
            logger.error(f"Error validating ZIP file: {e}")
            return False
    
    def get_active_challenge(self) -> Optional[Dict]:
        """Get active challenge information"""
        try:
            url = f"{self.api_url}/api/v1/challenges/active"
            response = requests.get(url, timeout=30)
            
            if response.status_code == 200:
                if not response.json() or response.json().get('status') == "no_active_challenge":
                    logger.info(f"Currently, No challenge is active!")
                    return None
                challenge = response.json()
                logger.info(f"Active challenge: {challenge['challenge_id']}")
                return challenge
            else:
                logger.error(f"No active challenge found: {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"Error getting active challenge: {e}")
            return None
    
    def generate_submission_id(self, challenge_id: str) -> Optional[Dict]:
        """Generate submission ID for the challenge"""
        try:
            url = f"{self.api_url}/api/v1/challenges/{challenge_id}/generate-submission-id"
            
            data = {
                'hotkey': self.miner_hotkey
            }
            
            response = requests.post(url, json=data, timeout=30)
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f"Generated submission ID: {result['submission_id']}")
                return result
            else:
                logger.error(f"Failed to generate submission ID: {response.status_code} - {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"Error generating submission ID: {e}")
            return None
    
    def submit_solution(self, challenge_id: str, submission_data: Dict, solution_zip_path: Path) -> bool:
        """Submit solution to challenge server"""
        try:
            url = f"{self.api_url}/api/v1/challenges/{challenge_id}/submit"
            
            # Calculate file hash
            file_hash = self.calculate_file_hash(solution_zip_path)
            
            # Create timestamp
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            
            # Convert timestamp to Unix format for signature
            dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            timestamp_unix = str(int(dt.timestamp()))
            
            # Create signature message using Unix timestamp
            message = f"{challenge_id}{submission_data['challenge_nonce']}{file_hash}{submission_data['submission_id']}{timestamp_unix}"
            
            # DEBUG LOGGING
            logger.debug(f"CLIENT DEBUG:")
            logger.debug(f"  challenge_id: {challenge_id}")
            logger.debug(f"  challenge_nonce: {submission_data['challenge_nonce']}")
            logger.debug(f"  file_hash: {file_hash}")
            logger.debug(f"  submission_id: {submission_data['submission_id']}")
            logger.debug(f"  timestamp_unix: {timestamp_unix}")
            logger.debug(f"  full_message: {message}")
            
            # Hash the message
            message_hash = hashlib.sha256(message.encode('utf-8')).digest()
            logger.debug(f"  message_hash: {message_hash.hex()}")
            
            signature = self.create_signature(message)
            logger.debug(f"  signature: {signature}")
            
            # Prepare form data (still use ISO timestamp for API)
            data = {
                'hotkey': self.miner_hotkey,
                'submission_id': submission_data['submission_id'],
                'signature': signature,
                'timestamp': timestamp,  # Keep ISO format for API
                'metadata': json.dumps({
                    'solution_type': 'verilog_design',
                    'submitted_by': self.miner_hotkey,
                    'submission_tool': 'chipforge_miner_cli'
                })
            }
            
            # Prepare file
            files = {
                'file': (solution_zip_path.name, open(solution_zip_path, 'rb'), 'application/zip')
            }
            
            logger.info(f"Submitting solution: {solution_zip_path.name}")
            logger.info(f"File size: {solution_zip_path.stat().st_size} bytes")
            logger.debug(f"File hash: {file_hash}")
            
            response = requests.post(url, data=data, files=files, timeout=120)
            
            # Close file
            files['file'][1].close()
            
            if response.status_code == 200:
                result = response.json()
                logger.info(f"Solution submitted successfully!")
                logger.info(f"Submission ID: {result.get('submission_id')}")
                logger.info(f"Status: {result.get('status')}")
                return True
            else:
                logger.error(f"Failed to submit solution: {response.status_code}")
                logger.error(f"Error: {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"Error submitting solution: {e}")
            return False
    
    def check_submission_status(self, challenge_id: str, submission_id: str) -> Optional[Dict]:
        """Check submission status"""
        try:
            url = f"{self.api_url}/api/v1/challenges/{challenge_id}/submissions/{submission_id}/status"
            response = requests.get(url, timeout=30)
            
            if response.status_code == 200:
                status = response.json()
                logger.info(f"Submission status: {status.get('status')}")
                return status
            else:
                logger.error(f"Failed to get submission status: {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"Error checking submission status: {e}")
            return None
    
    def get_miner_submissions(self, challenge_id: str) -> List[Dict]:
        """Get all submissions for this miner"""
        try:
            url = f"{self.api_url}/api/v1/challenges/{challenge_id}/submissions/hotkey/{self.miner_hotkey}"
            response = requests.get(url, timeout=30)
            
            if response.status_code == 200:
                submissions = response.json()
                logger.info(f"Found {len(submissions.get('submissions', []))} previous submissions")
                return submissions.get('submissions', [])
            else:
                logger.debug(f"No previous submissions found: {response.status_code}")
                return []
                
        except Exception as e:
            logger.error(f"Error getting miner submissions: {e}")
            return []


class MinerCLI:
    """Miner Command-Line Interface"""
    
    def __init__(self, config):
        self.config = config
        self.submitter = SolutionSubmitter(config)
        self.api_url = config.api_url
    
    def get_active_challenge(self) -> Optional[Dict]:
        """Get active challenge information"""
        return self.submitter.get_active_challenge()
    
    def get_challenge_info(self, challenge_id: str) -> Optional[Dict]:
        """Get detailed challenge information"""
        try:
            url = f"{self.api_url}/api/v1/challenges/{challenge_id}/info"
            response = requests.get(url, timeout=30)
            
            if response.status_code == 200:
                return response.json()
            else:
                logger.error(f"Failed to get challenge info: {response.status_code}")
                return None
        except Exception as e:
            logger.error(f"Error getting challenge info: {e}")
            return None
    
    def get_miner_submissions(self, challenge_id: Optional[str] = None) -> List[Dict]:
        """Get all submissions for this miner"""
        if challenge_id:
            return self.submitter.get_miner_submissions(challenge_id)
        else:
            # Get active challenge first
            challenge = self.get_active_challenge()
            if not challenge:
                logger.warning("No active challenge found")
                return []
            return self.submitter.get_miner_submissions(challenge['challenge_id'])
    
    def download_challenge_info(self, challenge_id: str, output_dir: Path) -> bool:
        """Download challenge information and save to file"""
        try:
            challenge_info = self.get_challenge_info(challenge_id)
            if not challenge_info:
                logger.error("Failed to retrieve challenge information")
                return False
            
            # Create output directory
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # Save challenge info as JSON
            info_file = output_dir / f"challenge_{challenge_id}_info.json"
            with open(info_file, 'w') as f:
                json.dump(challenge_info, f, indent=2)
            
            logger.info(f"Challenge information saved to: {info_file}")
            
            # Also try to download test cases if available
            try:
                url = f"{self.api_url}/api/v1/challenges/{challenge_id}/test_cases/download"
                response = requests.get(url, timeout=30)
                
                if response.status_code == 200:
                    test_cases_file = output_dir / f"challenge_{challenge_id}_test_cases.zip"
                    with open(test_cases_file, 'wb') as f:
                        f.write(response.content)
                    logger.info(f"Test cases downloaded to: {test_cases_file}")
                else:
                    logger.debug(f"Test cases not available: {response.status_code}")
            except Exception as e:
                logger.debug(f"Could not download test cases: {e}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error downloading challenge: {e}")
            return False
    
    def format_timestamp(self, timestamp_str: str) -> str:
        """Format ISO timestamp to readable format"""
        try:
            dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
            return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        except:
            return timestamp_str
    
    def format_duration(self, seconds: float) -> str:
        """Format duration in seconds to human-readable format"""
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            minutes = int(seconds // 60)
            secs = int(seconds % 60)
            return f"{minutes}m {secs}s"
        else:
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            return f"{hours}h {minutes}m"
    
    def show_status(self):
        """Show current challenge status and miner submissions"""
        logger.info("=" * 80)
        logger.info("ChipForge Miner Status")
        logger.info("=" * 80)
        logger.info("")
        
        # Get active challenge
        challenge = self.get_active_challenge()
        if not challenge:
            logger.warning("No active challenge")
            logger.info("")
            return
        
        challenge_id = challenge['challenge_id']
        logger.info(f"📋 Active Challenge: {challenge_id}")
        
        # Get detailed challenge info
        challenge_info = self.get_challenge_info(challenge_id)
        if challenge_info:
            if 'expires_at' in challenge_info:
                expires_at = datetime.fromisoformat(
                    challenge_info['expires_at'].replace('Z', '+00:00')
                )
                remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
                if remaining > 0:
                    logger.info(f"⏰ Time Remaining: {self.format_duration(remaining)}")
                else:
                    logger.info(f"⏰ Status: Expired")
            
            if 'description' in challenge_info:
                desc = challenge_info['description']
                if len(desc) > 100:
                    desc = desc[:100] + "..."
                logger.info(f"📝 Description: {desc}")
            
            if 'winner_baseline_score' in challenge_info:
                logger.info(f"🏆 Winner Baseline Score: {challenge_info['winner_baseline_score']}")
        
        logger.info("")
        
        # Get miner submissions
        submissions = self.get_miner_submissions(challenge_id)
        logger.info(f"📤 Your Submissions ({len(submissions)}):")
        logger.info("-" * 80)
        
        if not submissions:
            logger.info("  No submissions yet")
        else:
            # Sort by submission time (most recent first)
            sorted_subs = sorted(
                submissions,
                key=lambda x: x.get('submitted_at', ''),
                reverse=True
            )
            
            for i, sub in enumerate(sorted_subs[:10], 1):  # Show top 10
                sub_id = sub.get('submission_id', 'N/A')
                status = sub.get('status', 'unknown')
                score = sub.get('score', None)
                submitted_at = sub.get('submitted_at', '')
                
                status_emoji = {
                    'pending': '⏳',
                    'processing': '🔄',
                    'evaluated': '✅',
                    'failed': '❌',
                    'rejected': '🚫'
                }.get(status.lower(), '❓')
                
                logger.info(f"  {i}. {status_emoji} {sub_id[:16]}...")
                logger.info(f"     Status: {status}")
                if score is not None:
                    logger.info(f"     Score: {score}")
                if submitted_at:
                    logger.info(f"     Submitted: {self.format_timestamp(submitted_at)}")
                logger.info("")
        
        if len(submissions) > 10:
            logger.info(f"  ... and {len(submissions) - 10} more submissions")
        
        logger.info("=" * 80)
    
    def show_submissions(self, challenge_id: Optional[str] = None):
        """List all submissions for the miner"""
        logger.info("=" * 80)
        logger.info("ChipForge Miner Submissions")
        logger.info("=" * 80)
        logger.info("")
        
        if challenge_id:
            logger.info(f"Challenge ID: {challenge_id}")
        else:
            challenge = self.get_active_challenge()
            if challenge:
                challenge_id = challenge['challenge_id']
                logger.info(f"Active Challenge: {challenge_id}")
            else:
                logger.error("No active challenge specified or found")
                return
        
        logger.info("")
        
        submissions = self.get_miner_submissions(challenge_id)
        
        if not submissions:
            logger.info("No submissions found")
            logger.info("")
            return
        
        # Sort by submission time (most recent first)
        sorted_subs = sorted(
            submissions,
            key=lambda x: x.get('submitted_at', ''),
            reverse=True
        )
        
        logger.info(f"Total Submissions: {len(submissions)}")
        logger.info("-" * 80)
        logger.info("")
        
        for i, sub in enumerate(sorted_subs, 1):
            sub_id = sub.get('submission_id', 'N/A')
            status = sub.get('status', 'unknown')
            score = sub.get('score', None)
            submitted_at = sub.get('submitted_at', '')
            file_hash = sub.get('file_hash', '')
            
            status_emoji = {
                'pending': '⏳',
                'processing': '🔄',
                'evaluated': '✅',
                'failed': '❌',
                'rejected': '🚫'
            }.get(status.lower(), '❓')
            
            logger.info(f"{i}. {status_emoji} Submission ID: {sub_id}")
            logger.info(f"   Status: {status}")
            if score is not None:
                logger.info(f"   Score: {score}")
            if file_hash:
                logger.info(f"   File Hash: {file_hash[:16]}...")
            if submitted_at:
                logger.info(f"   Submitted: {self.format_timestamp(submitted_at)}")
            logger.info("")
        
        logger.info("=" * 80)
    
    def download_challenge(self, output_dir: Optional[str] = None, challenge_id: Optional[str] = None):
        """Download current challenge information and files"""
        logger.info("=" * 80)
        logger.info("Download Challenge")
        logger.info("=" * 80)
        logger.info("")
        
        # Get challenge ID
        if not challenge_id:
            challenge = self.get_active_challenge()
            if not challenge:
                logger.error("No active challenge found")
                return
            challenge_id = challenge['challenge_id']
        
        logger.info(f"Challenge ID: {challenge_id}")
        
        # Determine output directory
        if output_dir:
            output_path = Path(output_dir)
        else:
            output_path = Path(f"./challenges/{challenge_id}")
        
        logger.info(f"Output Directory: {output_path}")
        logger.info("")
        
        if self.download_challenge_info(challenge_id, output_path):
            logger.info("✅ Challenge downloaded successfully")
        else:
            logger.error("Failed to download challenge")
            sys.exit(1)
        
        logger.info("=" * 80)
    
    def submit_solution(self, solution_file: str, challenge_id: Optional[str] = None, 
                       check_status: bool = False, dry_run: bool = False):
        """Submit a solution file"""
        logger.info("=" * 80)
        logger.info("Submit Solution")
        logger.info("=" * 80)
        logger.info("")
        
        solution_path = Path(solution_file)
        if not solution_path.exists():
            logger.error(f"Solution file not found: {solution_file}")
            sys.exit(1)
        
        # Check file size (10MB limit)
        MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB in bytes
        file_size = solution_path.stat().st_size
        size_mb = file_size / (1024 * 1024)
        
        logger.info(f"Solution File: {solution_path}")
        logger.info(f"File Size: {file_size:,} bytes ({size_mb:.2f} MB)")
        
        if file_size > MAX_FILE_SIZE:
            logger.error(f"File size ({size_mb:.2f} MB) exceeds maximum allowed size (10 MB)")
            logger.error("Please reduce the file size and try again.")
            sys.exit(1)
        
        logger.info("")
        
        # Get challenge ID
        if not challenge_id:
            challenge = self.get_active_challenge()
            if not challenge:
                logger.error("No active challenge found")
                sys.exit(1)
            challenge_id = challenge['challenge_id']
        
        logger.info(f"Challenge ID: {challenge_id}")
        logger.info("")
        
        # Check previous submissions if requested
        if check_status:
            logger.info("Previous submissions:")
            submissions = self.submitter.get_miner_submissions(challenge_id)
            if submissions:
                for sub in submissions:
                    logger.info(f"  ID: {sub.get('submission_id')} - Status: {sub.get('status')} - Score: {sub.get('score', 'N/A')}")
            else:
                logger.info("  No previous submissions found")
            logger.info("")
        
        # Validate ZIP
        if not self.submitter.validate_solution_zip(solution_path):
            logger.error("Solution ZIP validation failed")
            sys.exit(1)
        
        if dry_run:
            logger.info("✅ Dry run completed. ZIP validated successfully.")
            logger.info(f"   File: {solution_path}")
            logger.info("=" * 80)
            return
        
        # Generate submission ID
        logger.info("Generating submission ID...")
        submission_data = self.submitter.generate_submission_id(challenge_id)
        if not submission_data:
            logger.error("Failed to generate submission ID")
            sys.exit(1)
        
        logger.info(f"Submission ID: {submission_data['submission_id']}")
        logger.info("")
        
        # Submit solution
        logger.info("Submitting solution...")
        success = self.submitter.submit_solution(challenge_id, submission_data, solution_path)
        
        if success:
            logger.info("✅ Solution submitted successfully!")
            logger.info("")
            
            # Check status
            status = self.submitter.check_submission_status(
                challenge_id,
                submission_data['submission_id']
            )
            if status:
                logger.info(f"Status: {status.get('status', 'unknown')}")
        else:
            logger.error("Failed to submit solution")
            sys.exit(1)
        
        logger.info("=" * 80)


def main():
    """Main CLI entry point"""
    parser = argparse.ArgumentParser(
        description="ChipForge Miner CLI Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  miner_cli.py status
  miner_cli.py submissions
  miner_cli.py download
  miner_cli.py submit solution.zip
        """
    )
    
    # Subcommands
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')
    
    # Status command
    status_parser = subparsers.add_parser('status', help='Show current challenge and submissions')
    
    # Submissions command
    submissions_parser = subparsers.add_parser('submissions', help='List all submissions')
    submissions_parser.add_argument('--challenge_id', type=str, help='Challenge ID (default: active challenge)')
    
    # Download command
    download_parser = subparsers.add_parser('download', help='Download current challenge')
    download_parser.add_argument('--output', '-o', type=str, help='Output directory (default: ./challenges/<challenge_id>)')
    download_parser.add_argument('--challenge_id', type=str, help='Challenge ID (default: active challenge)')
    
    # Submit command
    submit_parser = subparsers.add_parser('submit', help='Submit solution file')
    submit_parser.add_argument('file', type=str, help='Path to solution ZIP file')
    submit_parser.add_argument('--challenge_id', type=str, help='Challenge ID (default: active challenge)')
    submit_parser.add_argument('--check_status', action='store_true', help='Check status of previous submissions')
    submit_parser.add_argument('--dry_run', action='store_true', help='Validate ZIP but don\'t submit')
    
    # Common arguments
    parser.add_argument("--wallet.name", type=str, default=os.getenv("WALLET_NAME", "default"),
                       help="Wallet name (default: from .env or 'default')")
    parser.add_argument("--wallet.hotkey", type=str, default=os.getenv("MINER_HOTKEY", "default"),
                       help="Wallet hotkey (default: from .env or 'default')")
    parser.add_argument("--api_url", type=str, default=os.getenv("CHALLENGE_API_URL", "http://localhost:8000"),
                       help="Challenge server API URL (default: from .env or localhost:8000)")
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        sys.exit(1)
    
    try:
        # Create config object
        config = type('Config', (), {
            'wallet': type('Wallet', (), {
                'name': getattr(args, 'wallet.name'),
                'hotkey': getattr(args, 'wallet.hotkey')
            })(),
            'api_url': args.api_url
        })()
        
        # Initialize CLI
        cli = MinerCLI(config)
        
        # Execute command
        if args.command == 'status':
            cli.show_status()
        elif args.command == 'submissions':
            cli.show_submissions(challenge_id=getattr(args, 'challenge_id', None))
        elif args.command == 'download':
            cli.download_challenge(
                output_dir=getattr(args, 'output', None),
                challenge_id=getattr(args, 'challenge_id', None)
            )
        elif args.command == 'submit':
            cli.submit_solution(
                solution_file=args.file,
                challenge_id=getattr(args, 'challenge_id', None),
                check_status=getattr(args, 'check_status', False),
                dry_run=getattr(args, 'dry_run', False)
            )
        else:
            parser.print_help()
            sys.exit(1)
    
    except KeyboardInterrupt:
        logger.info("\n\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

