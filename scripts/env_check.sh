#!/bin/sh
# Compare .env with .env.example: same keys, same order. Values are never printed.
#   scripts/env_check.sh [ENV_FILE] [EXAMPLE_FILE]      (make env-check)
set -eu
env_file="${1:-.env}"
example="${2:-.env.example}"
[ -f "$env_file" ] || { echo "$env_file missing: cp $example $env_file"; exit 1; }

keys() { sed -n 's/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}\([A-Za-z_][A-Za-z0-9_]*\)[[:space:]]*=.*/\2/p' "$1"; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
keys "$example" > "$tmp/ex.order";  sort -u "$tmp/ex.order"  > "$tmp/ex"
keys "$env_file" > "$tmp/env.order"; sort -u "$tmp/env.order" > "$tmp/env"

status=0
missing="$(comm -23 "$tmp/ex" "$tmp/env")"
extra="$(comm -13 "$tmp/ex" "$tmp/env")"
dups="$(sort "$tmp/env.order" | uniq -d)"
if [ -n "$missing" ]; then echo "In $example but not in $env_file:"; echo "$missing" | sed 's/^/  /'; status=1; fi
if [ -n "$extra" ];   then echo "In $env_file but not in $example:"; echo "$extra" | sed 's/^/  /'; status=1; fi
if [ -n "$dups" ];    then echo "Set more than once in $env_file (the last one wins):"; echo "$dups" | sed 's/^/  /'; status=1; fi
if [ "$status" -eq 0 ] && ! cmp -s "$tmp/ex.order" "$tmp/env.order"; then
    echo "Same keys, but in a different order than $example"; status=1
fi
[ "$status" -eq 0 ] && echo "$env_file matches $example (same keys, same order)"
exit "$status"
