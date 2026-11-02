.PHONY: help install up down run transform lookups test test-unit lint format psql clean \
        schedule schedule-now schedule-status unschedule

# Optional local settings (e.g. POSTGRES_PORT=5433 when 5432 is already taken)
-include .env
POSTGRES_PORT ?= 5432

help:            ## Show available commands
	@grep -hE '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-16s %s\n", $$1, $$2}'

install:         ## Install the package + dev tools in the current virtualenv
	pip install -e ".[dev]"

up:              ## Start PostgreSQL and Adminer
	docker compose up -d postgres adminer

down:            ## Stop containers (keeps the database volume)
	docker compose down

run: up          ## Run the pipeline once, in Docker
	docker compose run --rm pipeline run

transform: up    ## Rebuild the analytics tables only
	docker compose run --rm pipeline transform

lookups:         ## List fuel type and district ids available in the API
	docker compose run --rm pipeline lookups

test: up         ## Run all tests (uses the separate pipeline_test database)
	TEST_DATABASE_URL=postgresql://pipeline:pipeline@localhost:$(POSTGRES_PORT)/pipeline_test pytest

test-unit:       ## Run unit tests only (no database needed)
	pytest -m "not integration"

lint:            ## Static checks
	ruff check . && ruff format --check .

format:          ## Auto-format the code
	ruff check --fix . && ruff format .

psql:            ## Open a SQL shell on the warehouse
	docker compose exec postgres psql -U pipeline -d analytics

clean:           ## Remove containers, the database volume and landed files
	docker compose down -v && rm -rf data/raw data/quarantine

# ---------------------------------------------------------------- automatic runs (macOS launchd)
LABEL := pt.vitorhsc.fuel-prices-pipeline
PLIST := $(HOME)/Library/LaunchAgents/$(LABEL).plist
GUI   := gui/$(shell id -u)

schedule:        ## Run the pipeline every day at 10:00 and 22:00 (macOS)
	@mkdir -p logs "$(HOME)/Library/LaunchAgents"
	@chmod +x scheduling/run_scheduled.sh
	@sed -e 's|__PROJECT_DIR__|$(CURDIR)|g' -e 's|__LABEL__|$(LABEL)|g' \
		scheduling/launchd.plist.template > "$(PLIST)"
	@launchctl bootout $(GUI)/$(LABEL) 2>/dev/null || true
	@launchctl bootstrap $(GUI) "$(PLIST)"
	@echo "Scheduled: every day at 10:00 and 22:00. Log: logs/scheduled.log"

schedule-now:    ## Start the scheduled job right now (to test it)
	launchctl kickstart $(GUI)/$(LABEL)
	@echo "Started. Follow it with: tail -f logs/scheduled.log"

schedule-status: ## Is the job loaded? How many runs? Last exit code?
	@launchctl print $(GUI)/$(LABEL) 2>/dev/null | grep -E 'state =|runs =|last exit code' \
		|| echo "Not scheduled. Run: make schedule"

unschedule:      ## Stop the automatic runs
	@launchctl bootout $(GUI)/$(LABEL) 2>/dev/null || true
	@rm -f "$(PLIST)"
	@echo "Automatic runs removed."
