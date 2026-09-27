#!/bin/bash
# start_validator.sh: run the validator without Docker (settings from .env)

source .env

# Create logs directory if it doesn't exist
mkdir -p logs

python neurons/validator.py \
    --netuid "$NETUID" \
    --subtensor.network "$SUBTENSOR_NETWORK" \
    --wallet.path "${VALIDATOR_WALLET_DIR:-${WALLET_DIR:-$HOME/.bittensor/wallets}}" \
    --wallet.name "${VALIDATOR_WALLET_NAME:-$WALLET_NAME}" \
    --wallet.hotkey "$VALIDATOR_HOTKEY" \
    --challenge_api_url "$CHALLENGE_API_URL" \
    --logging.debug
