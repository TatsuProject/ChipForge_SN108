# ChipForge Miner CLI - Command Reference

## Overview
The `miner_cli.py` tool provides a command-line interface for miners to interact with the ChipForge challenge system.

## Common Arguments
These arguments can be used with any command, before or after the command name:

- `--wallet.name <name>` - Wallet name (default: `WALLET_NAME` from `.env`, else `"default"`)
- `--wallet.hotkey <hotkey>` - Wallet hotkey (default: `MINER_HOTKEY`, else `"default"`)
- `--wallet.path <dir>` - Wallet directory (default: `WALLET_PATH`, else `~/.bittensor/wallets`)
- `--api_url <url>` - Challenge server API URL (default: `CHALLENGE_API_URL`, else `https://api.chipforge.io`)

**Note:** values in `.env` (see `.env.example`) are used as defaults.

`status`, `submissions` and `submit --check_status` read your submission history, which the
server only returns to the hotkey's owner: the CLI signs that request with your hotkey.

**Docker:** `make cli ARGS="status"`, `make submit FILE=solution.zip`.

---

## Commands

### 1. `status` - Show Current Challenge Status

Displays the active challenge information and your submissions.

**Usage:**
```bash
python3 python_scripts/miner_cli.py status [--wallet.name NAME] [--wallet.hotkey HOTKEY] [--api_url URL]
```

**Example:**
```bash
# Using .env file
python3 python_scripts/miner_cli.py status

# With explicit arguments
python3 python_scripts/miner_cli.py status \
    --wallet.name my_wallet \
    --wallet.hotkey my_hotkey \
    --api_url https://api.chipforge.io
```

**Output:**
- Active challenge ID
- Time remaining (if challenge is active)
- Challenge description
- Winner baseline score
- Your submissions (top 10 most recent)
  - Submission ID
  - Status (pending, processing, evaluated, failed, rejected)
  - Score (if available)
  - Submission timestamp

---

### 2. `submissions` - List All Submissions

Lists all your submissions for a challenge.

**Usage:**
```bash
python3 python_scripts/miner_cli.py submissions [--challenge_id CHALLENGE_ID] [--wallet.name NAME] [--wallet.hotkey HOTKEY] [--api_url URL]
```

**Options:**
- `--challenge_id <id>` - Specific challenge ID (default: uses active challenge)

**Example:**
```bash
# List submissions for active challenge
python3 python_scripts/miner_cli.py submissions

# List submissions for specific challenge
python3 python_scripts/miner_cli.py submissions --challenge_id challenge_123
```

**Output:**
- Total number of submissions
- For each submission:
  - Submission ID
  - Status
  - Score (if available)
  - File hash
  - Submission timestamp

---

### 3. `download` - Download the Challenge

Downloads the challenge package (specification, testbench, templates) and extracts it,
plus the challenge information as JSON. Hidden test cases stay with the validators.

**Usage:**
```bash
python3 python_scripts/miner_cli.py download [--output DIR] [--challenge_id CHALLENGE_ID] [--wallet.name NAME] [--wallet.hotkey HOTKEY] [--api_url URL]
```

**Options:**
- `--output <dir>` or `-o <dir>` - Output directory (default: `./challenges/<challenge_id>`)
- `--challenge_id <id>` - Specific challenge ID (default: uses active challenge)

**Example:**
```bash
# Download active challenge to default location
python3 python_scripts/miner_cli.py download

# Download to specific directory
python3 python_scripts/miner_cli.py download --output ./my_challenges

# Download specific challenge
python3 python_scripts/miner_cli.py download --challenge_id challenge_123 --output ./challenge_123
```

**Output:**
- Challenge information JSON file: `challenge_<id>_info.json`
- Challenge package `<id>.zip`, extracted into the output directory

---

### 3b. `logs` - Full Evaluation Logs of Your Submission

The website shows a summary of each validator's evaluation (gates, metrics, error code and
message). The complete EDA logs are only given to the miner who made the submission, and only
after its batch has closed. The request is signed with your hotkey.

```bash
python3 python_scripts/miner_cli.py logs <submission_id> [--output DIR]
```

Saves one JSON file per validator to `./evaluation_logs/<submission_id>/` and prints each
validator's score and notes.

---

### 4. `submit` - Submit Solution

Submits a solution ZIP file to the challenge server.

**Usage:**
```bash
python3 python_scripts/miner_cli.py submit <file> [--challenge_id CHALLENGE_ID] [--check_status] [--dry_run] [--wallet.name NAME] [--wallet.hotkey HOTKEY] [--api_url URL]
```

**Arguments:**
- `<file>` - **Required** - Path to solution ZIP file

**Options:**
- `--challenge_id <id>` - Specific challenge ID (default: uses active challenge)
- `--check_status` - Show previous submissions before submitting
- `--dry_run` - Validate ZIP file but don't submit (safe testing)

**Example:**
```bash
# Submit solution (using .env for credentials)
python3 python_scripts/miner_cli.py submit solution.zip

# Submit with check status
python3 python_scripts/miner_cli.py submit solution.zip --check_status

# Dry run (validate only, don't submit)
python3 python_scripts/miner_cli.py submit solution.zip --dry_run

# Submit to specific challenge
python3 python_scripts/miner_cli.py submit solution.zip --challenge_id challenge_123

# Full example with all options
python3 python_scripts/miner_cli.py submit solution.zip \
    --challenge_id challenge_123 \
    --check_status \
    --wallet.name my_wallet \
    --wallet.hotkey my_hotkey \
    --api_url https://api.chipforge.io
```

**Validation:**
- File must exist
- File must be a ZIP file
- File size must be ≤ 50 MB (the server's limit)
- ZIP file must be valid

**Output:**
- File information (name, size)
- Challenge ID
- Previous submissions (if `--check_status` used)
- Submission ID
- Submission status

---

## Using with .env File

Create a `.env` file in the project root:

```bash
WALLET_NAME=your_wallet_name
MINER_HOTKEY=your_hotkey_name
CHALLENGE_API_URL=https://api.chipforge.io
FILE_TO_SUBMIT=path/to/solution.zip
```

Then you can use commands without specifying wallet/API arguments:

```bash
python3 python_scripts/miner_cli.py status
python3 python_scripts/miner_cli.py submit solution.zip
```

---

## Using submit_solution.sh Script

The shell script `submit_solution.sh` provides a convenient wrapper:

```bash
./submit_solution.sh
```

This script:
1. Checks if `.env` file exists
2. Validates all required environment variables
3. Checks if solution file exists
4. Runs the submit command with `--check_status`

**Required .env variables:**
- `WALLET_NAME`
- `MINER_HOTKEY`
- `CHALLENGE_API_URL`
- `FILE_TO_SUBMIT`

---

## Error Handling

The CLI provides clear error messages:
- ❌ File not found
- ❌ File size exceeds the 50 MB limit
- ❌ No active challenge found
- ❌ ZIP validation failed
- ❌ Missing environment variables (in shell script)

---

## Quick Reference

| Command | Description | Required Args |
|---------|-------------|---------------|
| `status` | Show challenge status | None |
| `submissions` | List submissions | None |
| `download` | Download challenge | None |
| `submit` | Submit solution | `<file>` |

---

## Examples Summary

```bash
# Check status
python3 python_scripts/miner_cli.py status

# List all submissions
python3 python_scripts/miner_cli.py submissions

# Download challenge
python3 python_scripts/miner_cli.py download

# Test submission (dry run)
python3 python_scripts/miner_cli.py submit solution.zip --dry_run

# Submit solution
python3 python_scripts/miner_cli.py submit solution.zip --check_status

# Use shell script
./submit_solution.sh
```

