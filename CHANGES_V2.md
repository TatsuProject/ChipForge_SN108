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
