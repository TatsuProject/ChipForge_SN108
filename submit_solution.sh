#!/bin/bash
# submit_solution.sh - Example solution submission

# Check if .env file exists
if [ ! -f .env ]; then
    echo "❌ Error: .env file not found!"
    echo "   Please create a .env file in the project root directory."
    exit 1
fi

# Source .env file
source .env

# Check required environment variables
missing_vars=()

MINER_WALLET_NAME="${MINER_WALLET_NAME:-$WALLET_NAME}"
MINER_WALLET_DIR="${MINER_WALLET_DIR:-${WALLET_DIR:-$HOME/.bittensor/wallets}}"
if [ -z "$MINER_WALLET_NAME" ]; then
    missing_vars+=("MINER_WALLET_NAME")
fi

if [ -z "$MINER_HOTKEY" ]; then
    missing_vars+=("MINER_HOTKEY")
fi

if [ -z "$CHALLENGE_API_URL" ]; then
    missing_vars+=("CHALLENGE_API_URL")
fi

if [ -z "$FILE_TO_SUBMIT" ]; then
    missing_vars+=("FILE_TO_SUBMIT")
fi

# Exit if any required variables are missing
if [ ${#missing_vars[@]} -ne 0 ]; then
    echo "❌ Error: Missing required environment variables in .env file:"
    for var in "${missing_vars[@]}"; do
        echo "   - $var"
    done
    exit 1
fi

# Verify FILE_TO_SUBMIT exists
if [ ! -f "$FILE_TO_SUBMIT" ]; then
    echo "❌ Error: Solution file not found: $FILE_TO_SUBMIT"
    exit 1
fi

# All checks passed, proceed with submission
python python_scripts/miner_cli.py \
    --wallet.name "$MINER_WALLET_NAME" \
    --wallet.path "$MINER_WALLET_DIR" \
    --wallet.hotkey "$MINER_HOTKEY" \
    --api_url "$CHALLENGE_API_URL" \
    submit "$FILE_TO_SUBMIT" \
    --check_status