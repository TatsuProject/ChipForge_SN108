#!/bin/bash
# start_miner.sh: run the miner without Docker (settings from .env)

source .env

python neurons/miner.py \
    --netuid "$NETUID" \
    --subtensor.network "$SUBTENSOR_NETWORK" \
    ${SUBTENSOR_CHAIN_ENDPOINT:+--subtensor.chain_endpoint "$SUBTENSOR_CHAIN_ENDPOINT"} \
    --wallet.path "${MINER_WALLET_DIR:-${WALLET_DIR:-$HOME/.bittensor/wallets}}" \
    --wallet.name "${MINER_WALLET_NAME:-$WALLET_NAME}" \
    --wallet.hotkey "$MINER_HOTKEY" \
    --axon.port "${AXON_PORT:-8091}" \
    --challenge_api_url "$CHALLENGE_API_URL" \
    --logging.debug
