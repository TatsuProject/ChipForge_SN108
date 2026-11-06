# Validator requirements

A validator runs two things on one machine: the validator neuron (this repo) and the
[ChipForge EDA server](https://github.com/TatsuProject/chipforge_eda_server), which simulates and
synthesizes every submission. The EDA server sets the hardware needs.

## Hardware

|                 | CPU                        | RAM   | Disk        |
|-----------------|----------------------------|-------|-------------|
| **Recommended** | 16 cores / 32 threads      | 64 GB | 500 GB SSD  |
| **Minimum**     | 16 cores / 32 threads      | 64 GB | 300 GB SSD  |

- **16 physical cores with 2 threads each (32 vCPUs on most cloud instances).** Example: AWS
  `c7i.8xlarge` (Intel, 32 vCPUs = 16 cores / 32 threads, 64 GB). AWS AMD 7th-generation instances
  (`c7a`, `m7a`) have 1 vCPU per physical core and no second thread: pick one with 32 vCPUs there.
- **Why:** a full evaluation runs for hours, and single stages of it (e.g. timing analysis) can use more
  than 20 GB of RAM on their own. With less memory, evaluations fail as "host ran out of resources",
  count as failures of your validator, and the design goes to another validator.
- **Disk:** the evaluator's install check refuses to install with less than 100 GB free on the EDA
  server's data disk, and evaluations leave files behind. The EDA server (1.0.0 and later) deletes old
  evaluator versions automatically; plan 300 GB at least, 500 GB recommended. Your free disk is shown on
  your card on the website's validators page.
- **Time:** each design gets the whole machine for up to the evaluation window (currently 7 hours). A
  slower machine times out on designs a full-spec one finishes.
- **x86-64 only.** The EDA toolchain images do not run on ARM (e.g. AWS Graviton, Apple Silicon).
- Use the machine only for the validator and its EDA server: other heavy workloads take the memory an
  evaluation needs.

## Software and network

- Linux (Ubuntu 22.04 or 24.04 recommended), Docker with the Compose plugin, `make`, `git`.
- Outbound internet access to the challenge server and a subtensor endpoint. No inbound port is needed
  except SSH; **keep the EDA server's port 8080 closed to the internet**.

## On the subnet

- A hotkey registered on subnet 108 with a validator permit (enough stake), and a
  `VALIDATOR_SECRET_KEY` from the ChipForge team. See the main [README](../README.md#before-you-start-wallet-and-registration).
