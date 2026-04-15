# NemoClaw Escapades Makefile
#
# Quick start:
#   cp .env.example .env        # fill in real values
#   make setup                  # conda env + gateway + providers + sandbox
#   make run-local-dev          # start the orchestrator outside a sandbox
#   make run-local-sandbox      # start the orchestrator inside the sandbox
#
# All local Python targets run inside the "nemoclaw" conda environment.
# `make install` creates it automatically if it doesn't exist.
#
# The sandbox runs inside OpenShell with proxy-mediated credential injection.
# See policies/orchestrator.yaml for the full network and filesystem policy.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Load .env if present (for local targets that need vars)
ifneq (,$(wildcard .env))
  include .env
  export
endif

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IMAGE_NAME := nemoclaw-orchestrator
IMAGE_TAG  := latest
DOCKERFILE := docker/Dockerfile.orchestrator

# OpenShell
GATEWAY_NAME      := openshell
GATEWAY_CONTAINER := openshell-cluster-$(GATEWAY_NAME)
SANDBOX_NAME      := orchestrator
POLICY_BASE       := policies/orchestrator.yaml
POLICY            := policies/orchestrator.resolved.yaml

# Core providers (always attached to the sandbox)
INFERENCE_PROVIDER := inference-hub
INFERENCE_TYPE     := openai
SLACK_PROVIDER     := slack-credentials
DEFAULT_MODEL      := azure/anthropic/claude-opus-4-6

# Service credential providers (attached when available)
JIRA_PROVIDER       := jira-credentials
GITLAB_PROVIDER     := gitlab-credentials
GERRIT_PROVIDER     := gerrit-credentials
CONFLUENCE_PROVIDER := confluence-credentials
SLACK_USER_PROVIDER := slack-user-credentials
GITHUB_PROVIDER     := github-credentials

SERVICE_PROVIDERS := $(JIRA_PROVIDER) $(GITLAB_PROVIDER) $(GERRIT_PROVIDER) \
                     $(CONFLUENCE_PROVIDER) $(SLACK_USER_PROVIDER) $(GITHUB_PROVIDER)
ALL_PROVIDERS     := $(INFERENCE_PROVIDER) $(SLACK_PROVIDER) $(SERVICE_PROVIDERS)

# Python — all local targets run inside this conda environment.
CONDA_ENV     := nemoclaw
PYTHON_VER    := 3.13
CONDA_RUN     := conda run --live-stream -n $(CONDA_ENV)
MAIN_MODULE   := nemoclaw_escapades.main
BROKER_MODULE := nemoclaw_escapades.nmb.broker

# Audit DB paths — sandbox uses PVC-backed /sandbox (OpenShell >= 0.0.22)
AUDIT_DB_LOCAL    := $(HOME)/.nemoclaw/audit.db
AUDIT_DB_SANDBOX  := /sandbox/audit.db
AUDIT_SYNC_INTERVAL := 60

# SSH into the running sandbox
SSH_CMD := ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
	-o LogLevel=ERROR \
	-o "ProxyCommand=openshell ssh-proxy --gateway-name $(GATEWAY_NAME) --name $(SANDBOX_NAME)" \
	sandbox@openshell-$(SANDBOX_NAME)

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Setup (run once, idempotent)
# ---------------------------------------------------------------------------

.PHONY: setup
setup: install setup-gateway setup-providers setup-sandbox ## One-time: deps + gateway + providers + sandbox

.PHONY: install
install: ## Create conda env (if needed) and install the package + dev deps
	@conda env list 2>/dev/null | grep -q "^$(CONDA_ENV) " \
		|| { echo "Creating conda env '$(CONDA_ENV)' with Python $(PYTHON_VER)..."; \
		     conda create -n $(CONDA_ENV) python=$(PYTHON_VER) -y --solver=classic; }
	$(CONDA_RUN) pip install -e ".[dev]"

# OpenShell v0.0.21: `openshell gateway start` cannot restart a stopped
# gateway without destroying it.  Try `docker start` first to preserve
# provider/routing config.
.PHONY: setup-gateway
setup-gateway: ## Start the OpenShell gateway if not already running
	@command -v openshell >/dev/null 2>&1 || { echo "✗ openshell CLI not found. Install it first."; exit 1; }
	@command -v docker >/dev/null 2>&1 || { echo "✗ docker not found. Install Docker first."; exit 1; }
	@docker info >/dev/null 2>&1 || { echo "✗ Docker is not running. Start Docker Desktop and retry."; exit 1; }
	@if openshell status >/dev/null 2>&1; then \
		echo "✓ Gateway already running."; \
	elif docker inspect $(GATEWAY_CONTAINER) >/dev/null 2>&1; then \
		echo "Gateway stopped — restarting via docker..."; \
		docker start $(GATEWAY_CONTAINER); \
		echo "Waiting for k3s to initialise (up to 60s)..."; \
		for i in $$(seq 1 12); do \
			sleep 5; \
			if openshell status >/dev/null 2>&1; then \
				echo "✓ Gateway restarted (after $$(( i * 5 ))s)."; \
				break; \
			fi; \
			printf "  ...%ds\n" "$$(( i * 5 ))"; \
		done; \
		if ! openshell status >/dev/null 2>&1; then \
			echo "✗ Gateway failed to start after 60s. Run: openshell gateway destroy --name $(GATEWAY_NAME) && openshell gateway start"; \
			exit 1; \
		fi; \
	else \
		echo "No existing gateway — creating from scratch..."; \
		openshell gateway start; \
	fi

# All provider targets are idempotent: delete-then-create ensures a clean
# state regardless of whether the provider already exists.
# Vars come from `include .env` / `export` at the top of this Makefile.
.PHONY: setup-providers
setup-providers: .env ## Register all credential providers with the gateway
	@echo "Registering providers..."
	@command -v openshell >/dev/null 2>&1 && { \
		_reg() { \
			local name="$$1" label="$$2"; shift 2; \
			openshell provider delete "$$name" >/dev/null 2>&1 || true; \
			openshell provider create --name "$$name" --type generic "$$@" \
			&& echo "✓ $$label provider registered."; \
		}; \
		_reg_openai() { \
			openshell provider delete $(INFERENCE_PROVIDER) >/dev/null 2>&1 || true; \
			openshell provider create \
				--name $(INFERENCE_PROVIDER) \
				--type $(INFERENCE_TYPE) \
				--credential "OPENAI_API_KEY=$(INFERENCE_HUB_API_KEY)" \
				--config "OPENAI_BASE_URL=$(INFERENCE_HUB_BASE_URL)" \
			&& echo "✓ Inference provider registered."; \
			openshell inference set \
				--provider $(INFERENCE_PROVIDER) \
				--model "$${INFERENCE_MODEL:-$(DEFAULT_MODEL)}" \
				--no-verify \
			&& echo "✓ Inference routing configured."; \
		}; \
		_skip() { echo "⚠  $$1 not set — skipping $$2 provider."; }; \
		\
		_reg_openai; \
		_reg $(SLACK_PROVIDER) Slack \
			--credential "SLACK_BOT_TOKEN=$(SLACK_BOT_TOKEN)" \
			--credential "SLACK_APP_TOKEN=$(SLACK_APP_TOKEN)"; \
		\
		if [ -n "$(JIRA_AUTH)" ]; then \
			_reg $(JIRA_PROVIDER) Jira --credential "JIRA_AUTH=$(JIRA_AUTH)"; \
		else _skip JIRA_AUTH Jira; fi; \
		if [ -n "$(GITLAB_TOKEN)" ]; then \
			_reg $(GITLAB_PROVIDER) GitLab --credential "GITLAB_TOKEN=$(GITLAB_TOKEN)"; \
		else _skip GITLAB_TOKEN GitLab; fi; \
		if [ -n "$(GERRIT_USERNAME)" ] && [ -n "$(GERRIT_HTTP_PASSWORD)" ]; then \
			_reg $(GERRIT_PROVIDER) Gerrit \
				--credential "GERRIT_USERNAME=$(GERRIT_USERNAME)" \
				--credential "GERRIT_HTTP_PASSWORD=$(GERRIT_HTTP_PASSWORD)"; \
		else _skip "GERRIT_USERNAME/PASSWORD" Gerrit; fi; \
		if [ -n "$(CONFLUENCE_USERNAME)" ] && [ -n "$(CONFLUENCE_API_TOKEN)" ]; then \
			_reg $(CONFLUENCE_PROVIDER) Confluence \
				--credential "CONFLUENCE_USERNAME=$(CONFLUENCE_USERNAME)" \
				--credential "CONFLUENCE_API_TOKEN=$(CONFLUENCE_API_TOKEN)"; \
		else _skip "CONFLUENCE credentials" Confluence; fi; \
		if [ -n "$(SLACK_USER_TOKEN)" ]; then \
			_reg $(SLACK_USER_PROVIDER) "Slack user" \
				--credential "SLACK_USER_TOKEN=$(SLACK_USER_TOKEN)"; \
		else _skip SLACK_USER_TOKEN "Slack user"; fi; \
		if [ -n "$(GITHUB_TOKEN)" ]; then \
			_reg $(GITHUB_PROVIDER) GitHub --credential "GITHUB_TOKEN=$(GITHUB_TOKEN)"; \
		else _skip GITHUB_TOKEN GitHub; fi; \
	} || echo "⚠  openshell CLI not found — skipping provider registration."

.PHONY: gen-policy
gen-policy: .env ## Generate resolved policy with allowed_ips from .env
	@PYTHONPATH=src $(CONDA_RUN) python scripts/gen_policy.py

# Symlink docker/Dockerfile.orchestrator → ./Dockerfile so the build
# context is the project root.  Cleaned up immediately after.
# Before destroying the old sandbox, save the audit DB if it exists.
.PHONY: setup-sandbox
setup-sandbox: gen-policy ## Build image, create sandbox, and start the app inside it
	@echo "Creating orchestrator sandbox..."
	@command -v openshell >/dev/null 2>&1 && { \
		if openshell sandbox get $(SANDBOX_NAME) >/dev/null 2>&1; then \
			echo "  Saving audit DB before sandbox recreation..."; \
			mkdir -p "$$(dirname $(AUDIT_DB_LOCAL))"; \
			openshell sandbox download $(SANDBOX_NAME) $(AUDIT_DB_SANDBOX) $(AUDIT_DB_LOCAL) 2>/dev/null \
				&& echo "  ✓ Audit DB saved to $(AUDIT_DB_LOCAL)" \
				|| echo "  (no audit DB to save)"; \
		fi; \
		openshell sandbox delete $(SANDBOX_NAME) 2>/dev/null || true; \
		ln -sf $(DOCKERFILE) Dockerfile; \
		SVC_FLAGS=""; \
		for p in $(SERVICE_PROVIDERS); do \
			if openshell provider get "$$p" >/dev/null 2>&1; then \
				SVC_FLAGS="$$SVC_FLAGS --provider $$p"; \
				echo "  Attaching provider: $$p"; \
			fi; \
		done; \
		openshell sandbox create \
			--name $(SANDBOX_NAME) \
			--from . \
			--policy $(POLICY) \
			--provider $(INFERENCE_PROVIDER) \
			--provider $(SLACK_PROVIDER) \
			$$SVC_FLAGS \
			-- python -m $(MAIN_MODULE); \
		rm -f Dockerfile; \
	} || echo "⚠  openshell CLI not found — skipping sandbox creation."

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

.PHONY: run-local-dev
run-local-dev: ## Run the orchestrator outside a sandbox (bare process, .env creds)
	PYTHONPATH=src $(CONDA_RUN) python -m $(MAIN_MODULE)

.PHONY: run-local-sandbox
run-local-sandbox: setup-gateway setup-providers setup-sandbox ## (Re)create and run the orchestrator in the sandbox

.PHONY: run-broker
run-broker: ## Run the NMB broker locally
	PYTHONPATH=src $(CONDA_RUN) python -m $(BROKER_MODULE) --audit-db $(AUDIT_DB_LOCAL)

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

.PHONY: build
build: ## Build the orchestrator container image
	docker build -f $(DOCKERFILE) -t $(IMAGE_NAME):$(IMAGE_TAG) .

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

.PHONY: test
test: ## Run unit tests (excludes integration)
	PYTHONPATH=src $(CONDA_RUN) pytest tests/ -v --ignore=tests/integration

.PHONY: test-integration
test-integration: ## Run multi-sandbox NMB integration tests
	PYTHONPATH=src $(CONDA_RUN) pytest tests/integration/ -v

.PHONY: test-all
test-all: ## Run all tests (unit + integration)
	PYTHONPATH=src $(CONDA_RUN) pytest tests/ -v

.PHONY: test-auth
test-auth: ## Verify .env credentials against their APIs (no sandbox needed)
	@PYTHONPATH=src $(CONDA_RUN) python scripts/test_auth.py

# Sandbox connectivity tests — run against a live sandbox.
.PHONY: test-jira-sandbox test-gitlab-sandbox test-gerrit-sandbox \
        test-confluence-sandbox test-slack-search-sandbox test-services-sandbox

test-jira-sandbox: ## Test Jira connectivity inside the sandbox
	@$(SSH_CMD) "python3 /app/scripts/test_jira_sandbox.py"

test-gitlab-sandbox: ## Test GitLab connectivity inside the sandbox
	@$(SSH_CMD) "python3 /app/scripts/test_gitlab_sandbox.py"

test-gerrit-sandbox: ## Test Gerrit connectivity inside the sandbox
	@$(SSH_CMD) "python3 /app/scripts/test_gerrit_sandbox.py"

test-confluence-sandbox: ## Test Confluence connectivity inside the sandbox
	@$(SSH_CMD) "python3 /app/scripts/test_confluence_sandbox.py"

test-slack-search-sandbox: ## Test Slack user-token connectivity inside the sandbox
	@$(SSH_CMD) "python3 /app/scripts/test_slack_search_sandbox.py"

test-services-sandbox: test-jira-sandbox test-gitlab-sandbox test-gerrit-sandbox test-confluence-sandbox test-slack-search-sandbox ## Run all sandbox service tests

# ---------------------------------------------------------------------------
# Lint / Format
# ---------------------------------------------------------------------------

.PHONY: lint
lint: ## Run ruff + mypy
	$(CONDA_RUN) ruff check src/ tests/
	$(CONDA_RUN) ruff format --check src/ tests/
	$(CONDA_RUN) mypy src/

.PHONY: typecheck
typecheck: ## Run mypy type checks
	$(CONDA_RUN) mypy src/

.PHONY: fmt format
fmt format: ## Auto-format code
	$(CONDA_RUN) ruff format src/ tests/
	$(CONDA_RUN) ruff check --fix src/ tests/

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

.PHONY: logs
logs: ## Tail app logs from a second terminal (primary logs stream in setup-sandbox)
	@command -v openshell >/dev/null 2>&1 && \
		openshell sandbox exec -n $(SANDBOX_NAME) --no-tty -- tail -f /app/logs/nemoclaw.log \
		|| echo "Could not tail sandbox logs. Is the sandbox running? Try: make status"

.PHONY: logs-proxy
logs-proxy: ## Show OpenShell proxy-level logs (network routing, L7 decisions)
	@command -v openshell >/dev/null 2>&1 && \
		openshell logs $(SANDBOX_NAME) || \
		echo "openshell not installed or sandbox not running"

.PHONY: status
status: ## Print sandbox and provider status
	@command -v openshell >/dev/null 2>&1 && { \
		echo "=== Gateway ==="; openshell status 2>/dev/null || echo "(not running)"; echo ""; \
		echo "=== Providers ==="; openshell provider list 2>/dev/null || echo "(none)"; echo ""; \
		echo "=== Sandbox ==="; openshell sandbox get $(SANDBOX_NAME) 2>/dev/null || echo "(not created)"; \
	} || echo "openshell not installed"

# ---------------------------------------------------------------------------
# Audit DB
# ---------------------------------------------------------------------------

.PHONY: audit-checkpoint
audit-checkpoint: ## Force a WAL checkpoint inside the sandbox (folds WAL into main DB)
	@$(SSH_CMD) "python3 -c \"import sqlite3; c=sqlite3.connect('$(AUDIT_DB_SANDBOX)'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()\"" \
		&& echo "✓ WAL checkpoint completed" \
		|| echo "⚠  Checkpoint failed (sandbox not running?)"

.PHONY: audit-download
audit-download: audit-checkpoint ## Checkpoint WAL then download the audit DB from the sandbox
	@mkdir -p "$$(dirname $(AUDIT_DB_LOCAL))"
	@rm -rf "$(AUDIT_DB_LOCAL)"
	@openshell sandbox download $(SANDBOX_NAME) $(AUDIT_DB_SANDBOX) $(AUDIT_DB_LOCAL) \
		&& echo "✓ Downloaded to $(AUDIT_DB_LOCAL)"
	@if [ -d "$(AUDIT_DB_LOCAL)" ]; then \
		mv "$(AUDIT_DB_LOCAL)/audit.db" "$(AUDIT_DB_LOCAL).tmp" 2>/dev/null \
			|| mv "$(AUDIT_DB_LOCAL)/"* "$(AUDIT_DB_LOCAL).tmp" 2>/dev/null; \
		rm -rf "$(AUDIT_DB_LOCAL)"; \
		mv "$(AUDIT_DB_LOCAL).tmp" "$(AUDIT_DB_LOCAL)"; \
		echo "  (fixed: openshell created directory instead of file)"; \
	fi

.PHONY: audit-stats
audit-stats: ## Print audit DB summary (row counts, last entries)
	@[ -f "$(AUDIT_DB_LOCAL)" ] || { echo "No audit DB at $(AUDIT_DB_LOCAL). Run: make audit-download"; exit 1; }
	@echo "=== Tool calls ===" && \
		sqlite3 "$(AUDIT_DB_LOCAL)" "SELECT COUNT(*) AS total, SUM(success) AS ok, COUNT(*)-SUM(success) AS err FROM tool_calls" && \
		echo "" && echo "=== Last 5 tool calls ===" && \
		sqlite3 -header -column "$(AUDIT_DB_LOCAL)" \
			"SELECT datetime(timestamp,'unixepoch','localtime') AS time, service, command, duration_ms, CASE success WHEN 1 THEN 'ok' ELSE 'ERR' END AS status FROM tool_calls ORDER BY timestamp DESC LIMIT 5"

.PHONY: audit-query
audit-query: ## Run a SQL query against the local audit DB (SQL="SELECT ...")
	@[ -f "$(AUDIT_DB_LOCAL)" ] || { echo "No audit DB at $(AUDIT_DB_LOCAL). Run: make audit-download"; exit 1; }
	@sqlite3 -header -column "$(AUDIT_DB_LOCAL)" "$(SQL)"

.PHONY: audit-export
audit-export: ## Export tool calls to JSONL (writes to audit_tool_calls.jsonl)
	@[ -f "$(AUDIT_DB_LOCAL)" ] || { echo "No audit DB at $(AUDIT_DB_LOCAL). Run: make audit-download"; exit 1; }
	@PYTHONPATH=src $(CONDA_RUN) python -c "\
	import asyncio; from nemoclaw_escapades.audit.db import AuditDB; \
	async def _e(): \
	    db = AuditDB('$(AUDIT_DB_LOCAL)'); await db.open(); \
	    n = await db.export_tool_calls_jsonl('audit_tool_calls.jsonl'); await db.close(); \
	    print(f'Exported {n} rows to audit_tool_calls.jsonl'); \
	asyncio.run(_e())"

.PHONY: audit-sync
audit-sync: ## Background loop: checkpoint + download audit DB every $(AUDIT_SYNC_INTERVAL)s
	@mkdir -p "$$(dirname $(AUDIT_DB_LOCAL))"
	@echo "Syncing audit DB every $(AUDIT_SYNC_INTERVAL)s  (Ctrl+C to stop)"
	@while true; do \
		$(SSH_CMD) "python3 -c \"import sqlite3; c=sqlite3.connect('$(AUDIT_DB_SANDBOX)'); c.execute('PRAGMA wal_checkpoint(TRUNCATE)'); c.close()\"" 2>/dev/null; \
		openshell sandbox download $(SANDBOX_NAME) $(AUDIT_DB_SANDBOX) $(AUDIT_DB_LOCAL) 2>/dev/null \
			&& echo "$$(date '+%H:%M:%S') ✓ synced" \
			|| echo "$$(date '+%H:%M:%S') · sandbox not ready"; \
		sleep $(AUDIT_SYNC_INTERVAL); \
	done

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

.PHONY: stop
stop: ## Stop the orchestrator sandbox
	@command -v openshell >/dev/null 2>&1 && \
		openshell sandbox delete $(SANDBOX_NAME) 2>/dev/null \
		&& echo "✓ Sandbox '$(SANDBOX_NAME)' stopped." \
		|| echo "⚠  Sandbox '$(SANDBOX_NAME)' not found or already stopped."

.PHONY: stop-all
stop-all: ## Delete ALL sandboxes in the gateway
	@command -v openshell >/dev/null 2>&1 && { \
		for sb in $$(openshell sandbox list 2>/dev/null | tail -n +2 | awk '{print $$1}'); do \
			echo "Deleting sandbox $$sb..."; \
			openshell sandbox delete "$$sb" 2>/dev/null || true; \
		done; \
		echo "✓ All sandboxes deleted."; \
	} || echo "⚠  openshell CLI not found."

.PHONY: clean
clean: ## Delete sandbox, providers, and local image (saves audit DB first)
	@command -v openshell >/dev/null 2>&1 && { \
		if openshell sandbox get $(SANDBOX_NAME) >/dev/null 2>&1; then \
			echo "Saving audit DB before cleanup..."; \
			mkdir -p "$$(dirname $(AUDIT_DB_LOCAL))"; \
			openshell sandbox download $(SANDBOX_NAME) $(AUDIT_DB_SANDBOX) $(AUDIT_DB_LOCAL) 2>/dev/null \
				&& echo "✓ Audit DB saved to $(AUDIT_DB_LOCAL)" \
				|| echo "(no audit DB to save)"; \
		fi; \
		openshell sandbox delete $(SANDBOX_NAME) 2>/dev/null || true; \
		for p in $(ALL_PROVIDERS); do \
			openshell provider delete "$$p" >/dev/null 2>&1 || true; \
		done; \
	} || true
	@docker rmi $(IMAGE_NAME):$(IMAGE_TAG) 2>/dev/null || true

.PHONY: clean-all
clean-all: clean ## clean + stop the gateway
	@command -v openshell >/dev/null 2>&1 && openshell gateway stop 2>/dev/null || true
