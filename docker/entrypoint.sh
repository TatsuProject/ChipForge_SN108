#!/bin/sh
# chipforge-entrypoint validator|miner|cli [extra args...]
# Settings come from the environment (compose passes .env); extra args are appended,
# so any bittensor flag can still be given on the command line.
set -eu

role="${1:-validator}"
[ $# -gt 0 ] && shift

: "${NETUID:?NETUID is not set (see .env.example)}"
# Each role has its own wallet. Compose mounts VALIDATOR_WALLET_DIR (validator) or
# MINER_WALLET_DIR (miner) at /wallets. WALLET_NAME is the pre-split name, kept as a fallback.
wallets=/wallets
case "$role" in
  validator) wallet_name="${VALIDATOR_WALLET_NAME:-${WALLET_NAME:-}}"; hotkey="${VALIDATOR_HOTKEY:-default}"; prefix=VALIDATOR ;;
  miner)     wallet_name="${MINER_WALLET_NAME:-${WALLET_NAME:-}}";     hotkey="${MINER_HOTKEY:-default}";     prefix=MINER ;;
  *)         echo "unknown role '$role' (expected validator or miner)" >&2; exit 2 ;;
esac

# Config errors: explain, then wait before exiting so `restart: unless-stopped` doesn't spin.
config_error() {
    echo "CONFIG ERROR: $*" >&2
    echo "Fix .env, then: make up" >&2
    sleep 60
    exit 78
}

[ -n "$wallet_name" ] || config_error "${prefix}_WALLET_NAME is not set (see .env.example)"

# A host path in the wallet name can't be seen in the container (only the wallet
# directory is mounted, at /wallets). Use the last path component.
case "$wallet_name" in
  */*)
    echo "WARNING: ${prefix}_WALLET_NAME is a path ($wallet_name); using wallet '$(basename "$wallet_name")' from ${prefix}_WALLET_DIR" >&2
    wallet_name="$(basename "$wallet_name")"
    ;;
esac

if [ ! -f "$wallets/$wallet_name/hotkeys/$hotkey" ]; then
    available="$(ls -1 "$wallets" 2>/dev/null | tr '\n' ' ')"
    config_error "hotkey '$hotkey' of wallet '$wallet_name' not found in ${prefix}_WALLET_DIR (mounted at $wallets).
  Set ${prefix}_WALLET_DIR in .env to the folder that contains the wallet folder, ${prefix}_WALLET_NAME to the
  wallet folder's name and ${prefix}_HOTKEY to the hotkey's file name.
  Wallets visible in ${prefix}_WALLET_DIR: ${available:-none}"
fi

set -- --netuid "$NETUID" \
       --subtensor.network "${SUBTENSOR_NETWORK:-finney}" \
       --wallet.name "$wallet_name" \
       --wallet.path "$wallets" \
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
esac
