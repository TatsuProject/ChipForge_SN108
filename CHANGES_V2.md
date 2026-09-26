# ChipForge SN108 (validator / miner): `improvements/v2`

Each change: the problem, the fix, how it behaves now, and whether it can change an existing flow.
Server-side counterpart: `SUBNET_SYNC_CHANGES.md` in the challenge-server repo.

Owner decisions this branch follows:
- The winner always gets the configured `--miner_emission_percentage` (or `MINER_EMISSION_PERCENTAGE`); the rest is burned.
- Each validator picks its own winner and reward timing from its own evaluations (no winner sync with the challenge server).
- A new winner is put on chain immediately (as soon as the chain's rate limit allows).
- The EDA server runs without an API key.

---

## Phase A: emissions correctness

### A.1 Failed weight-sets were treated as successes
- **Problem:** bittensor 10's `subtensor.set_weights()` returns an `ExtrinsicResponse`. It defines `__len__` (always 2) and no `__bool__`, so `bool(response)` is **always True**. Every `if success:` passed even when the chain rejected the call, and the "failed → burn" fallbacks could never run.
- **Fix/Now:** success is read from `response.success`, and chain rejections are logged with the chain's message. A test uses the real `ExtrinsicResponse`.
- **Changes existing flow?** Only logging and retries: failures are now visible and retried.

### A.2 One weight policy; the winner always gets the configured share
- **Problem:** the two post-batch paths called `set_weights({hotkey: 1.0})`, giving the winner **100%** and skipping the banned-coldkey list, while the six other paths used `--miner_emission_percentage` (default 10%) with bans. The same winner got 100% right after a batch and 10% on the next loop.
- **Fix:** every path now only chooses a *target* (burn, or winner X). `WeightManager.build()` turns it into weights with one policy:
  - winner gets the configured percentage, the rest goes to UID 0
  - burn when emissions are banned, the winner's coldkey is banned, the winner is not registered, or the winner is UID 0
  - The 13 direct burn calls and the dead `weights = {...}` lines in crash recovery (which now really restore the winner) go through the same path.
- **Now:** the percentage comes from `--miner_emission_percentage`, else `MINER_EMISSION_PERCENTAGE` in `.env`, else 10.
- **Changes existing flow?** Yes, intentionally: the post-batch 100% becomes the configured share. **If your `.env` sets `MINER_EMISSION_PERCENTAGE`, that value is now used** (it was ignored before).

### A.3 Weights only submitted when needed, without blocking the loop
- **Problem:** a (burn or winner) weight call was made on every 10-second loop. The call was synchronous, waited for finalization and blocked the event loop, and most attempts were rejected by the chain's rate limit anyway.
- **Fix/Now:** after every loop, and so immediately after a batch that finds a new winner, the current target is submitted only if:
  - the weights changed, or
  - a refresh is due (`WEIGHTS_REFRESH_SECONDS`, default 1200), which keeps the validator active on chain

  and only when `blocks_since_last_update ≥ weights_rate_limit`. A new winner blocked by the rate limit stays pending and goes out as soon as the window reopens. The call runs in a worker thread with `wait_for_finalization=False`. Plain lists are passed (no torch tensors).
- **Changes existing flow?** Far fewer chain calls. The weights set are the same as the policy above.

### A.4 Metagraph synced every 10 minutes instead of every loop
- **Problem:** a full metagraph sync (blocking) ran every 10 s.
- **Fix/Now:** synced at most every `METAGRAPH_SYNC_SECONDS` (default 600), in a worker thread.
- **Changes existing flow?** A miner that registers during a challenge is seen within ~10 minutes (well inside a tempo).

### A.5 Crash-safe state files
- **Problem:** `validator_state.json`, `emission_state.json` and `banned_coldkeys.json` were written in place. A crash mid-write left an unreadable file; the load failed silently, and the validator restarted with fresh state, losing the winner and reward timer. The files were relative to the working directory.
- **Fix/Now:**
  - atomic writes (temp file + fsync + `os.replace`)
  - an unreadable file is moved to `<name>.corrupt-<timestamp>` with a loud error instead of being overwritten
  - all state lives in `CHIPFORGE_DATA_DIR` (default: current directory, the old location)
  - `evaluated_batches` is kept in order, so trimming keeps the most recent batch IDs (it used to slice an unordered set)
- **Changes existing flow?** No. Existing files are read from the same place.

### A.6 Clean shutdown on SIGTERM
- **Problem:** only Ctrl-C was handled, so `docker stop` / `kill` killed the process mid-cycle.
- **Fix/Now:** SIGTERM and SIGINT stop the loop after the current step and save state before exiting.
- **Changes existing flow?** No.

---

## Phase B: evaluation correctness (protects miners)

### B.1 A validator that can't evaluate no longer claims batches
- **Problem:**
  - With the test-case zip missing, the validator ran a *dummy evaluator* and sent zeros for the whole batch.
  - With the EDA server down, every submission became a failed evaluation.
  - Either way the batch was claimed, and each failure used up one of the miner's 3 rebatch attempts. After 3, the submission becomes `EXHAUSTED`.
- **Fix/Now:**
  - Before downloading a batch, the validator checks that the EDA server is reachable (`GET /health`) and that the test cases are present. If either fails it **skips the batch** (logged as an error) so other validators handle it, and it keeps rewarding or burning per its reward window.
  - With the test cases missing, the evaluator returns nothing instead of zeros.
  - The dummy evaluator only runs when `USE_DUMMY_EVALUATION=true`.
- **Changes existing flow?** Yes, intentionally: a broken validator stops harming miners.

### B.2 EDA results read correctly
- **Problem:** the EDA server reports `result` (`ACCEPTED`/`REJECTED`/`ERROR`) and `error.fault`/`retryable`, which the client ignored. A system error (`overall: null`) crashed the notes formatting and fell back to a generic failure.
- **Fix/Now:**
  - `ERROR` (fault `system`: toolchain, bundle, infrastructure) is never the miner's score. It is sent as a retryable failed evaluation with the EDA error code in the details.
  - `REJECTED` (fault `miner`) is a real 0 with the reason (code, stage, message) in the notes and details, so miners see why.
- **Changes existing flow?** Miner-fault rejections now show the reason. Scoring is unchanged.

### B.3 EDA time budget follows the server's deadline
- **Problem:**
  - Up to 8 requests were sent at once to an EDA server with fewer lanes, so queue time counted against each timeout.
  - The batch timeout ran from when the validator picked up the batch, not from the server's `evaluation_ends_at`. A late pick-up could finish after the window closed and get 403 on every score.
- **Fix/Now:**
  - At most `EDA_MAX_CONCURRENCY` (default 4) requests in flight.
  - Each request's timeout is the time left until `evaluation_ends_at` minus `EDA_DEADLINE_BUFFER_SECONDS` (default 90, kept for submitting scores). The batch timeout is `evaluation_ends_at` minus 15 s.
  - A batch closing in under `MIN_BATCH_SECONDS` (default 180) is skipped.
  - Falls back to the old window-based budget when the server gives no deadline.
- **Changes existing flow?** Fewer timeouts; no late submissions.

### B.4 Rewarded hotkey from the batch, downloads verified
- **Problem:** the rewarded miner's hotkey was parsed out of the download filename, and that filename was used as a local path without cleaning it. Downloads weren't checked.
- **Fix/Now:**
  - The hotkey comes from the batch entry (challenge server `06218ec`+), with filename parsing only as a fallback for older servers.
  - Each download is checked against the `file_hash` the miner signed, and a mismatch is discarded.
  - Files are saved under a sanitized basename.
- **Changes existing flow?** No, apart from rejecting tampered files.

### B.5 Leaner EDA calls
- **Before:** each evaluation opened a new HTTP session, wrote the ZIP to a temp file just to read it back, and re-read the test-case zip from disk.
- **Now:** one shared session, bytes sent directly, test cases read once per batch.
- Also fixed: `check_testcase_files_exist` referenced an undefined `file_path`, and the validator data directory now follows `CHIPFORGE_DATA_DIR`.
- **Changes existing flow?** No.

### B.6 EDA server: no API key (companion change in `chipforge_eda_server`, branch `remove-api-key`)
- The gateway no longer checks `EDA_API_KEY`/`X-API-Key` (owner decision), and it gains `GET /health` for the pre-flight check. The validator never sent a key, so nothing changes on the client.
- **Operator note:** the gateway runs the uploaded evaluator's `run.py` as root. **Allow port 8080 only from your validator** (security group or firewall), or bind `127.0.0.1:8080` when co-located.

---

## Phase C: challenge-server integration

### C.1 One signing helper, v2 request-bound signatures (v1 kept for older servers)
- **Problem:**
  - The signing, parameter and header setup was copied into 6 functions, and the retry loop into 3.
  - v1 signatures cover only `hotkey + timestamp`, travel in the URL, and could be replayed for 10 minutes on any endpoint.
  - Signatures, messages and full form data were logged at INFO.
- **Fix/Now:**
  - Every challenge-server call goes through `_auth()` + `_signed()`: a fresh signature and nonce per attempt, and retries on network errors / 5xx only (not on 404/409).
  - `SIGNATURE_MODE=both` (default) sends v1 query params **and** v2 headers:
    - servers with v2 (challenge server `improvements/v2`) verify v2 (method, path, body digest, single-use nonce)
    - older servers ignore the headers and verify v1
  - Set `SIGNATURE_MODE=v2` once every server you use has v2; signatures then leave the URL entirely.
  - No signatures or secrets in logs; form data only at DEBUG.
- **Verified:** against the real server sandbox in `both` and `v2` modes: sync, batch, hash-verified downloads, test cases and signed `submit_score` (3/3).
- **Changes existing flow?** No: works with the current production server unchanged.

### C.2 `/validator/sync` with automatic fallback
- **Problem:** each loop called `/challenges/active` twice, `/batch/current` every ~12 s, `/info` every ~60 s and bans every 10 min, about 700 requests per hour while idle.
- **Fix/Now:**
  - `get_active_challenge`, `get_challenge_info`, `get_current_batch` and `get_banned_coldkeys` keep their return shapes, so the validator loop is unchanged.
  - When the server offers `/validator/sync`, all of them are answered from one cached call, revalidated with `ETag` (304) and refreshed per the server's `next_poll_seconds` (5–60 s).
  - Older servers answer 404. The client then uses the individual endpoints, and re-checks for sync hourly.
  - Right before evaluating, the baseline is fetched fresh (bypassing the cache).
- **Changes existing flow?** Far fewer requests; same data.

### C.3 Test cases re-downloaded only when they change
- **Problem:** while the server's `download_new_testcases` flag was set (55-minute TTL), the zip was downloaded again every ~60 s, about 55 times.
- **Fix/Now:**
  - With sync, test cases are downloaded only when their version (the S3 ETag) changes. The version is stored next to the zip.
  - On older servers, the flag triggers at most one download per 10 minutes.
  - The zip is written atomically.
- **Changes existing flow?** No.

### C.4 Secrets and URLs from `.env`, not the command line
- **Problem:** `--validator_secret_key` was required on the command line, visible to any user via `ps`, and the API URL defaulted to `http://localhost:8000`.
- **Fix/Now:**
  - `VALIDATOR_SECRET_KEY` and `CHALLENGE_API_URL` are read from the environment/`.env` (the flags still work and win).
  - The default URL is `https://api.chipforge.io`.
  - `start_validator.sh` no longer passes the secret.
- **Changes existing flow?** No: `.env` already had both values.

### C.5 Server values bounded
- A `winner_reward_hours` above `MAX_WINNER_REWARD_HOURS` (default 720) is ignored in favour of the local value.

---

## Phase E: cleanup

### E.1 torch removed
- **Problem:** torch (several GB with its CUDA stack) was installed only to wrap two lists in tensors for `set_weights`.
- **Fix/Now:** weights are passed as plain lists (bittensor 10 turns them into numpy arrays in `convert_and_normalize_weights_and_uids`). torch is removed from `requirements.txt`, and `requirements-lock.txt` was regenerated with the same versions for everything else: torch, triton, the CUDA/NVIDIA packages, sympy, mpmath, networkx, filelock and fsspec are gone. The test image went from 2.19 GB (with CPU-only torch) to 1.01 GB, and the suite passes with torch not installed.
- **Changes existing flow?** No. Reinstall dependencies once (`pip install -r requirements.txt`); torch can be uninstalled.

### E.2 Dead and duplicated code removed
- **Removed:**
  - `calculate_weights_from_hotkeys`, `mark_first_challenge_complete`, `BannedColdkeysManager.is_banned`
  - the never-read state fields `challenge_best_miners`, `active_challenges`, `expired_challenges` (old state files still load; the keys are ignored)
  - 19 always-true `hasattr(self.state, 'current_challenge_best')` checks
  - lazily created loop attributes (now set in `__init__`)
  - unused imports
  - (Phase C removed the dead client methods)
- **Merged:**
  - the batch-window update written twice → `ValidatorState.update_batch_windows()`
  - the two near-identical miner notifiers → one `_broadcast()`
- **Changes existing flow?** No.

### E.3 Miner notifications no longer block the validator
- **Problem:** after every batch (and on a new challenge) the validator waited up to 60 s for every miner's axon to answer before continuing.
- **Fix/Now:** notifications run as background tasks with `MINER_NOTIFY_TIMEOUT` (default 12 s), and missing answers are logged at DEBUG.
- **Changes existing flow?** Miners get the same messages; the validator doesn't wait for them.

---

## Phase F: miner

### F.1 Challenge auto-download works with server-hosted challenges
- **Problem:** the miner always rewrote the challenge URL into `<url>/archive/main.zip` (GitHub's archive format). The server now hands out its own link (`…/api/v1/challenges/{id}/download`), so auto-download always got a 404.
- **Fix/Now:** `resolve_download_url()`:
  - GitHub repository URLs are still turned into archive URLs.
  - Any other URL (server download link, pre-signed S3 link) is used as-is.
  - With no URL, it falls back to `{CHALLENGE_API_URL}/api/v1/challenges/{id}/download`.
- **Changes existing flow?** Only fixes it: GitHub URLs behave exactly as before.

### F.2 A failed download is retried
- **Problem:** the challenge directory was created before downloading. After any failure (404, timeout, bad zip) the directory existed, so every later poll said "already exists, skipping". The miner never got the challenge until someone deleted the folder by hand.
- **Fix/Now:**
  - The download and extraction happen in a temporary directory inside `downloaded_active_challenge/`, which is renamed into place only on success.
  - A challenge counts as downloaded only once `challenge_metadata.json` exists. An empty directory left by the old code is replaced.
  - Failures are retried on the next poll.
- **Changes existing flow?** No. Same folder layout: `downloaded_active_challenge/<challenge_id>/…` plus `challenge_metadata.json`.

### F.3 Download limits
- **Problem:** the whole response was read into memory with no size cap, and archives were extracted with no limit on entries or expanded size.
- **Fix/Now:**
  - The download is streamed to disk and capped at `MINER_MAX_CHALLENGE_MB` (200).
  - Extraction is refused above `MINER_MAX_ZIP_MEMBERS` (10000) entries or `MINER_MAX_EXTRACTED_MB` (1024) expanded.
  - A challenge id containing a path is refused.
  - For the record: Python's `zipfile.extractall` already strips `..` and absolute paths, so there was no zip-slip hole.
- **Changes existing flow?** No for real challenge packages (a few MB).

### F.4 Only validators can message the miner
- **Problem:** `blacklist_simple_message` accepted everyone, so any machine on the internet could spam the axon.
- **Fix/Now:**
  - Callers must be registered on the subnet and hold a validator permit (`MINER_REQUIRE_VALIDATOR_PERMIT=false` drops the permit check but still requires registration).
  - Priority is the caller's stake.
  - The notice itself is only a hint: the miner never downloads from a URL inside a message. It polls the challenge server immediately when a validator announces a challenge it doesn't have yet.
  - For the record: the audit's "miner downloads from URLs sent by anyone" was not the case, since the old handler already ignored the URL.
- **Changes existing flow?** Validators' notices are answered as before; everyone else gets 403.

### F.5 Non-blocking miner loop, configurable polling
- **Problem:**
  - `requests` calls ran on the event loop, blocking the axon while waiting.
  - The metagraph was synced synchronously every 60 s.
  - The challenge server was polled every 60 s by every miner.
  - `--challenge_api_url` defaulted to `http://localhost:8000`.
- **Fix/Now:**
  - HTTP runs in worker threads.
  - Metagraph sync every `METAGRAPH_SYNC_SECONDS` (600) in a thread.
  - Polling every `MINER_POLL_SECONDS` (default 300, minimum 30), plus immediately on a validator notice (F.4), so new challenges are still picked up within seconds.
  - The URL comes from `CHALLENGE_API_URL`/`.env`, default `https://api.chipforge.io`.
  - SIGTERM/SIGINT stop the miner cleanly.
- **Changes existing flow?** No. Miners running against a local server must set `CHALLENGE_API_URL` or pass the flag.

### F.6 Miner CLI fixes (`python_scripts/miner_cli.py`)
- **`status`/`submissions` always showed "no submissions":** the history endpoint requires a signature (`signature`, `timestamp` over `f"{hotkey}{timestamp}"`) and the CLI sent none, so it got a 422 and showed nothing. It now signs the request, and failures are shown as warnings rather than hidden at DEBUG.
- **Wrong sort key:** submissions were sorted and dated by `submitted_at`, which the server doesn't return. The CLI now uses `created_at`.
- **`download` fetched nothing useful:** it called the validator-only test-case endpoint. It now downloads and extracts the challenge package from `/challenges/{id}/download`, and still saves the challenge info JSON.
- **Flag order:**
  - `--wallet.name`, `--wallet.hotkey`, `--api_url` only worked before the subcommand. They now work on either side, and values given before the subcommand are not overwritten by defaults.
  - New flag `--wallet.path` (`WALLET_PATH`/`BT_WALLET_PATH`).
  - Default API URL `https://api.chipforge.io`.
- **Size limit:** the client refused anything over 10 MB while the server accepts 50 MB. The limit is now 50 MB (`MAX_SUBMISSION_SIZE_MB`).
- **Changes existing flow?** No: the documented invocation `miner_cli.py --wallet.name … submit file.zip` still works (tested). Submission signing is unchanged.

**Tests:** `tests/test_miner.py` (URL resolution, atomic download and retry, zip limits, blacklist, immediate poll, CLI parsing, signature and size limit), plus a real axon/dendrite loopback test that a non-validator gets 403.

---

## Phase G: packaging, configuration, docs

### G.1 Docker image and compose
- **Problem:** operators installed Python, conda and several GB of dependencies by hand and ran the neurons under `nohup`. Nothing restarted them after a crash or reboot, logs grew without limit, and state was scattered in the repo directory.
- **Fix/Now:**
  - `Dockerfile`: `python:3.12-slim`, multi-stage, installed from `requirements-lock.txt`. The image is 586 MB, has no torch, and runs as a non-root user (the host's UID/GID, so `./data` stays owned by you).
  - `docker-compose.yml` has one service per role (profiles `validator` / `miner`):
    - host networking, `restart: unless-stopped`, 60 s stop grace, log rotation (5 × 50 MB)
    - `.env` passed via `env_file`
    - wallets mounted read-only at `/wallets`, state bind-mounted at `/data`
  - In-container paths (`CHIPFORGE_DATA_DIR`, `MINER_CHALLENGE_DIR`, `WALLET_PATH`) are pinned in compose, so host values in `.env` can't break them.
  - `docker/entrypoint.sh` builds the command line from `.env` (`NETUID`, `SUBTENSOR_NETWORK`, optional `SUBTENSOR_CHAIN_ENDPOINT`, `WALLET_NAME`, hotkeys, `AXON_PORT`, `BT_LOG_LEVEL`). Extra arguments are appended.
- **Verified:** both images were started against testnet with a throwaway, unregistered wallet and a dead challenge-server URL:
  - state, logs and `validator_data/` were written to `/data`
  - the loop survived the unreachable server
  - the healthcheck reported alive
  - the miner axon answered a non-validator with HTTP 403
  - SIGTERM stopped it cleanly
- **Changes existing flow?** No. `start_validator.sh`, `start_miner.sh` and `submit_solution.sh` still work as before; Docker is an additional way to run.

### G.2 Heartbeat healthcheck (`chipforge/heartbeat.py`)
- Each neuron rewrites `<data dir>/heartbeat-<role>` every `HEARTBEAT_SECONDS` (30) from an asyncio task. The file goes stale if the event loop blocks or wedges, not only if the process dies.
- `python -m chipforge.heartbeat validator --max-age 180` is the container healthcheck, also shown by `make status`.

### G.3 Makefile
- **Run the containers:** `make up | down | restart | logs | status` (`ROLE=miner` for the miner).
- **Backups:** `make backup-state` writes a tarball of the data dir to `./backups`. It leaves out challenge packages and never prunes anything.
- **Migration:** `make migrate-state` copies (never moves or overwrites) the old repo-root state files into `./data`, so a bare-metal validator continues where it left off.
- **Miner CLI:** `make submit FILE=…` and `make cli ARGS="…"` run the miner CLI in the image.
- **Tests:** `make test` runs the suite in a throwaway container as your user.
- `DATA_DIR` is taken from `.env` if set there.

### G.4 `.env.example` rewritten
- It lists every setting the code reads, grouped as chain, wallet, challenge server, validator rewards, validator evaluation, miner, and storage/runtime, each with its default and what it does.
- It no longer contains a placeholder secret (`VALIDATOR_SECRET_KEY=abc`) or an empty `NETUID`.
- Inline comments were removed: some tools (`source .env` in the shell scripts) don't strip them.

### G.5 Docs
- **README:**
  - Docker quick start
  - What the validator does each cycle, and the reward rule (`MINER_EMISSION_PERCENTAGE`, burn to UID 0, no winner sync)
  - EDA server hardening (no API key: keep 8080 private)
  - Miner polling/download behaviour and the 50 MB limit
  - `/validator/sync` in the API list
  - Health-check commands
  - `./start_miner` / `./start_validator` typos fixed
- **`MINER_CLI_COMMANDS.md`:** flags usable on either side of the command, `--wallet.path`, signed history requests, what `download` now fetches, 50 MB, and the Docker usage.

### G.6 Found while running the containers
- **Filename fallback looked in the wrong directory:** `BatchProcessor` created and read `./validator_data/submissions` relative to the working directory, while the API client saves downloads under `CHIPFORGE_DATA_DIR`. With a data dir set, the filename fallback for hotkeys looked in the wrong place, and in the container the validator crashed at startup (read-only working dir). It now uses the data dir. Regression test added.
- **Rejected weights were retried every loop:** a rejected or failed `set_weights` (unregistered hotkey, RPC down) was retried on every 10 s loop, one extrinsic each time. The same weights are now retried after 30 s, 60 s, 120 s … up to 10 minutes. Different weights (a new winner) are still tried immediately, and a success resets the backoff.

### G.7 Clear wallet errors in Docker (found on the first real `make up`)
- **Problem:**
  - `WALLET_NAME` set to a host path (which `start_*.sh` accepted) or a `WALLET_DIR` that didn't contain the wallet made the validator crash with a `KeyFileError` traceback every few seconds.
  - The process exited with code 0 even on a fatal error.
- **Fix/Now:**
  - The entrypoint uses the last component of a path in `WALLET_NAME` and checks that `WALLET_DIR/<wallet>/hotkeys/<hotkey>` exists before starting. If it doesn't, it prints what to set and which wallets it can see, then waits 60 s before exiting (code 78), so the restart policy doesn't spin.
  - Fatal errors in the validator and miner now exit with code 1.
- **Changes existing flow?** No.

### G.8 `make env-check`
- `scripts/env_check.sh` compares `.env` with `.env.example`: missing keys, extra keys, keys set twice, and a different order. It never prints values. `.env` can now be kept as a line-by-line copy of `.env.example` with your own values.
