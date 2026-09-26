#!/bin/sh
# chipforge-entrypoint validator|miner|cli [extra args...]
# Settings come from the environment (compose passes .env); extra args are appended,
# so any bittensor flag can still be given on the command line.
set -eu

role="${1:-validator}"
[ $# -gt 0 ] && shift

: "${NETUID:?NETUID is not set (see .env.example)}"
: "${WALLET_NAME:?WALLET_NAME is not set (see .env.example)}"

set -- --netuid "$NETUID" \
       --subtensor.network "${SUBTENSOR_NETWORK:-finney}" \
       --wallet.name "$WALLET_NAME" \
       --wallet.path "${WALLET_PATH:-/wallets}" \
       "--logging.${BT_LOG_LEVEL:-info}" \
       "$@"
if [ -n "${SUBTENSOR_CHAIN_ENDPOINT:-}" ]; then
    set -- --subtensor.chain_endpoint "$SUBTENSOR_CHAIN_ENDPOINT" "$@"
fi

case "$role" in
  validator)
    exec python /app/neurons/validator.py --wallet.hotkey "${VALIDATOR_HOTKEY:-default}" "$@"
    ;;
  miner)
    exec python /app/neurons/miner.py --wallet.hotkey "${MINER_HOTKEY:-default}" \
        --axon.port "${AXON_PORT:-8091}" "$@"
    ;;
  *)
    echo "unknown role '$role' (expected validator or miner)" >&2
    exit 2
    ;;
esac
