# Velair heating sync via docker compose
# Root has no compose plugin; use the standalone binary.
# Bind-mount this tree so the container always runs local source.
ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
COMPOSE := /usr/local/bin/docker-compose -f $(ROOT)/compose.yaml --project-directory $(ROOT)
COMPOSE_HA := $(COMPOSE) -f $(ROOT)/compose.ha.yaml --profile ha
RUN := $(COMPOSE) run --rm --volume $(ROOT):/app velair
PYTEST := $(COMPOSE) run --rm --volume $(ROOT):/app --entrypoint python velair -m pytest
ARGS ?=

.PHONY: help build validate show test backup restore apply apply-yes collect fixtures check ha-up ha-down test-ha

help: ## list targets
	@grep -E '^[a-zA-Z0-9_-]+:.*?##' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

build: ## build image
	$(COMPOSE) build

validate: ## lint schedule offline
	$(RUN) validate $(ARGS)

show: ## show resolved schedule (ARGS=--entity climate.hc1 --day weekday)
	$(RUN) show $(ARGS)

test: ## check HA connection
	$(RUN) test $(ARGS)

backup: ## backup live schedules
	$(RUN) backup $(ARGS)

restore: ## restore backup (ARGS=backups/<file>.yaml)
	$(RUN) restore $(ARGS)

apply: ## dry-run apply
	$(RUN) apply --dry-run $(ARGS)

apply-yes: ## apply schedules to HA
	$(RUN) apply --yes $(ARGS)

collect: ## pull stats into reviews (ARGS=--since YYYY-MM-DD)
	$(RUN) collect $(ARGS)

fixtures: ## run bad-fixture linter checks
	$(COMPOSE) run --rm --volume $(ROOT):/app --entrypoint python velair tests/run_bad_fixtures.py

check: ## offline pytest + fake HA blackbox
	$(PYTEST) -q $(ARGS)

ha-up: ## start optional local HA on 127.0.0.1:8123
	@mkdir -p $(ROOT)/tests/ha/config
	@test -f $(ROOT)/tests/ha/config/configuration.yaml || \
		cp $(ROOT)/tests/ha/configuration.yaml $(ROOT)/tests/ha/config/configuration.yaml
	$(COMPOSE_HA) up -d homeassistant

ha-down: ## stop optional local HA
	$(COMPOSE_HA) down

test-ha: ## smoke test against local HA only (tests/ha.env)
	$(COMPOSE) run --rm --volume $(ROOT):/app --entrypoint python velair tests/run_ha_smoke.py
