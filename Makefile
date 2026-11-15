# Velair heating sync via docker compose
COMPOSE := docker compose
RUN := $(COMPOSE) run --rm velair
ARGS ?=

.PHONY: help build validate show test backup restore apply apply-yes collect fixtures

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
	$(COMPOSE) run --rm --entrypoint python velair tests/run_bad_fixtures.py
