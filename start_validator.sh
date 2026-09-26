#!/bin/bash
# start_validator.sh

source .env

# Create logs directory if it doesn't exist
mkdir -p logs

python neurons/validator.py \
    --netuid $NETUID \
    --subtensor.network $SUBTENSOR_NETWORK \
    --wallet.name $WALLET_NAME \
    --wallet.hotkey $VALIDATOR_HOTKEY \
    --challenge_api_url $CHALLENGE_API_URL \
    --logging.debug