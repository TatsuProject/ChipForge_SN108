# ChipForge SN108 operator commands. One role per machine: ROLE=validator (default) or ROLE=miner.
#   make up                       build if needed and start in the background
#   make logs / status / restart / down
#   make backup-state             snapshot ./data into ./backups
#   make migrate-state            copy state files from a pre-Docker checkout into ./data
#   make submit FILE=design.zip   miner: submit a solution with the miner CLI
#   make test                     run the test-suite in a throwaway container

ROLE      ?= validator
ENV_DATA_DIR := $(shell sed -n 's/^DATA_DIR=\([^ #]*\).*/\1/p' .env 2>/dev/null | tail -n1)
DATA_DIR  ?= $(if $(ENV_DATA_DIR),$(ENV_DATA_DIR),./data)
COMPOSE   := HOST_UID=$(shell id -u) HOST_GID=$(shell id -g) DATA_DIR=$(DATA_DIR) docker compose --profile $(ROLE)
SERVICE   := $(ROLE)
STAMP     := $(shell date -u +%Y%m%dT%H%M%SZ)

.PHONY: help check-env build up down restart logs status backup-state migrate-state submit cli test

help:
	@sed -n '1,9p' Makefile | sed 's/^# \{0,1\}//'

check-env:
	@test -f .env || { echo ".env missing: cp .env.example .env and fill it in"; exit 1; }
	@mkdir -p $(DATA_DIR)

build: check-env
	$(COMPOSE) build

up: check-env
	$(COMPOSE) up -d --build
	@$(MAKE) --no-print-directory status

down:
	$(COMPOSE) down

restart: check-env
	$(COMPOSE) restart $(SERVICE)

logs:
	$(COMPOSE) logs -f --tail=200 $(SERVICE)

status:
	@$(COMPOSE) ps $(SERVICE)
	@docker exec chipforge-$(ROLE) python -m chipforge.heartbeat $(ROLE) 2>/dev/null || echo "$(ROLE): not running or heartbeat stale"

backup-state:
	@mkdir -p backups
	tar -czf backups/state-$(ROLE)-$(STAMP).tar.gz --exclude='./downloaded_active_challenge' -C $(DATA_DIR) .
	@echo "backups/state-$(ROLE)-$(STAMP).tar.gz"

# Before Docker the validator kept its state in the repo root. Copy (never move or
# overwrite) those files into DATA_DIR so the container continues where it left off.
migrate-state:
	@mkdir -p $(DATA_DIR)
	@for f in validator_state.json emission_state.json banned_coldkeys.json validator_data logs; do \
	  if [ -e "$$f" ] && [ ! -e "$(DATA_DIR)/$$f" ]; then cp -a "$$f" "$(DATA_DIR)/" && echo "copied $$f"; \
	  elif [ -e "$$f" ]; then echo "skip $$f (already in $(DATA_DIR))"; fi; \
	done

submit: check-env
	@test -n "$(FILE)" || { echo "usage: make submit FILE=path/to/solution.zip"; exit 1; }
	HOST_UID=$(shell id -u) HOST_GID=$(shell id -g) DATA_DIR=$(DATA_DIR) docker compose --profile miner run --rm --no-deps \
	  -v "$(abspath $(FILE)):/tmp/solution.zip:ro" --entrypoint python miner \
	  /app/python_scripts/miner_cli.py submit /tmp/solution.zip --check_status

# Any miner CLI command, e.g. make cli ARGS="status"
cli: check-env
	HOST_UID=$(shell id -u) HOST_GID=$(shell id -g) DATA_DIR=$(DATA_DIR) docker compose --profile miner run --rm --no-deps \
	  --entrypoint python miner /app/python_scripts/miner_cli.py $(ARGS)

test:
	docker build -q -t chipforge-sn108-test -f docker/Dockerfile.test . >/dev/null
	docker run --rm -u $(shell id -u):$(shell id -g) -e HOME=/tmp -e PYTHONDONTWRITEBYTECODE=1 \
	  -e BT_WALLET_PATH=/nonexistent -v "$(CURDIR)":/app -w /app chipforge-sn108-test \
	  python -m pytest -q -p no:cacheprovider tests
