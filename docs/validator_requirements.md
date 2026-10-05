# Validator requirements

A validator runs two things on one machine: the validator neuron (this repo) and the
[ChipForge EDA server](https://github.com/TatsuProject/chipforge_eda_server), which simulates and
synthesizes every submission. The EDA server sets the hardware needs.

## Hardware

|                 | Physical CPU cores | RAM   | Disk        |
|-----------------|--------------------|-------|-------------|
| **Recommended** | 24                 | 48 GB | 100 GB SSD  |
| **Minimum**     | 16                 | 32 GB | 100 GB SSD  |

- **Physical cores, not vCPUs.** Most cloud instances count 2 vCPUs per physical core
  (hyperthreading), so 16 physical cores is usually 32 vCPUs. AWS AMD 7th-generation instances
  (`c7a`, `m7a`) are the exception: there 1 vCPU = 1 physical core, e.g. `c7a.4xlarge` = 16 cores / 32 GB.
- **Why:** synthesis of one submission is a long single-threaded run that needs up to about 10 GB of
  RAM, and simulation spreads across the remaining cores. More cores and memory let the EDA server
  evaluate more submissions at the same time. It sizes itself to the machine automatically.
- **Measured:** on the minimum spec (16 cores, 32 GB), eight NPU submissions evaluated in parallel
  finished in about 22 minutes, inside the evaluation window of a batch.
- **x86-64 only.** The EDA toolchain images do not run on ARM (e.g. AWS Graviton, Apple Silicon).
- Don't run other heavy workloads on the same machine: a synthesis run that runs out of memory fails.

## Software and network

- Linux (Ubuntu 22.04 or 24.04 recommended), Docker with the Compose plugin, `make`, `git`.
- Outbound internet access to the challenge server and a subtensor endpoint. No inbound port is needed
  except SSH; **keep the EDA server's port 8080 closed to the internet**.

## On the subnet

- A hotkey registered on subnet 108 with a validator permit (enough stake), and a
  `VALIDATOR_SECRET_KEY` from the ChipForge team. See the main [README](../README.md#before-you-start-wallet-and-registration).
