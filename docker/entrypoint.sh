#!/bin/sh
# chipforge-entrypoint validator|miner|cli [extra args...]
# Settings come from the environment (compose passes .env); extra args are appended,
# so any bittensor flag can still be given on the command line.
set -eu

role="${1:-validator}"
[ $# -gt 0 ] && shift

: "${NETUID:?NETUID is not set (see .env.example)}"
: "${WALLET_NAME:?WALLET_NAME is not set (see .env.example)}"

wallets="${WALLET_PATH:-/wallets}"
case "$role" in
  validator) hotkey="${VALIDATOR_HOTKEY:-default}" ;;
  miner)     hotkey="${MINER_HOTKEY:-default}" ;;
  *)         hotkey="" ;;
esac

# Config errors: explain, then wait before exiting so `restart: unless-stopped` doesn't spin.
config_error() {
    echo "CONFIG ERROR: $*" >&2
    echo "Fix .env, then: make up" >&2
    sleep 60
    exit 78
}

# A host path in WALLET_NAME (what the start_*.sh scripts accepted) can't be seen in the
# container: only WALLET_DIR is mounted, at /wallets. Use the last path component.
case "$WALLET_NAME" in
  */*)
    echo "WARNING: WALLET_NAME is a path ($WALLET_NAME); using wallet '$(basename "$WALLET_NAME")' from WALLET_DIR" >&2
    WALLET_NAME="$(basename "$WALLET_NAME")"
    ;;
esac

if [ -n "$hotkey" ] && [ ! -f "$wallets/$WALLET_NAME/hotkeys/$hotkey" ]; then
    available="$(ls -1 "$wallets" 2>/dev/null | tr '\n' ' ')"
    config_error "hotkey '$hotkey' of wallet '$WALLET_NAME' not found in WALLET_DIR (mounted at $wallets).
  Set WALLET_DIR in .env to the folder that contains the wallet folder, and WALLET_NAME to the wallet folder's name.
  Wallets visible in WALLET_DIR: ${available:-none}"
fi

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
    exec python /app/neurons/validator.py --wallet.hotkey "$hotkey" "$@"
    ;;
  miner)
    exec python /app/neurons/miner.py --wallet.hotkey "$hotkey" \
        --axon.port "${AXON_PORT:-8091}" "$@"
    ;;
  *)
    echo "unknown role '$role' (expected validator or miner)" >&2
    exit 2
    ;;
esac
