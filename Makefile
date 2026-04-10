# NemoClaw Escapades Makefile
#
# Quick start:
#   cp .env.example .env        # fill in real values
#   make setup                  # gateway + providers + sandbox (one-time)
#   make run-local-dev          # start the orchestrator outside a sandbox
#   make run-local-sandbox      # start the orchestrator inside the sandbox
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
POLICY            := policies/orchestrator.yaml

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

# Python
MAIN_MODULE   := nemoclaw_escapades.main
BROKER_MODULE := nemoclaw_escapades.nmb.broker
AUDIT_DB      := .audit.db

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
install: ## Install the package and dev dependencies
	pip install -e ".[dev]"

# OpenShell v0.0.21: `openshell gateway start` cannot restart a stopped
# gateway without destroying it.  Try `docker start` first to preserve
# provider/routing config.
.PHONY: setup-gateway
setup-gateway: ## Start the OpenShell gateway if not already running
	@command -v openshell >/dev/null 2>&1 && { \
		if openshell status >/dev/null 2>&1; then \
			echo "✓ Gateway already running."; \
		elif docker inspect $(GATEWAY_CONTAINER) >/dev/null 2>&1; then \
			echo "Gateway stopped — restarting via docker..."; \
			docker start $(GATEWAY_CONTAINER); \
			echo "Waiting for k3s to initialise..."; \
			sleep 10; \
			if openshell status >/dev/null 2>&1; then \
				echo "✓ Gateway restarted."; \
			else \
				echo "⚠  Gateway not ready yet — try 'openshell status' in a few seconds."; \
			fi; \
		else \
			echo "No existing gateway — creating from scratch..."; \
			openshell gateway start; \
		fi; \
	} || echo "⚠  openshell CLI not found — skipping gateway setup."

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
		[ -n "$(JIRA_AUTH)" ] \
			&& _reg $(JIRA_PROVIDER) Jira --credential "JIRA_AUTH=$(JIRA_AUTH)" \
			|| _skip JIRA_AUTH Jira; \
		[ -n "$(GITLAB_TOKEN)" ] \
			&& _reg $(GITLAB_PROVIDER) GitLab --credential "GITLAB_TOKEN=$(GITLAB_TOKEN)" \
			|| _skip GITLAB_TOKEN GitLab; \
		[ -n "$(GERRIT_USERNAME)" ] && [ -n "$(GERRIT_HTTP_PASSWORD)" ] \
			&& _reg $(GERRIT_PROVIDER) Gerrit \
				--credential "GERRIT_USERNAME=$(GERRIT_USERNAME)" \
				--credential "GERRIT_HTTP_PASSWORD=$(GERRIT_HTTP_PASSWORD)" \
			|| _skip "GERRIT_USERNAME/PASSWORD" Gerrit; \
		[ -n "$(CONFLUENCE_USERNAME)" ] && [ -n "$(CONFLUENCE_API_TOKEN)" ] \
			&& _reg $(CONFLUENCE_PROVIDER) Confluence \
				--credential "CONFLUENCE_USERNAME=$(CONFLUENCE_USERNAME)" \
				--credential "CONFLUENCE_API_TOKEN=$(CONFLUENCE_API_TOKEN)" \
			|| _skip "CONFLUENCE credentials" Confluence; \
		[ -n "$(SLACK_USER_TOKEN)" ] \
			&& _reg $(SLACK_USER_PROVIDER) "Slack user" \
				--credential "SLACK_USER_TOKEN=$(SLACK_USER_TOKEN)" \
			|| _skip SLACK_USER_TOKEN "Slack user"; \
		[ -n "$(GITHUB_TOKEN)" ] \
			&& _reg $(GITHUB_PROVIDER) GitHub --credential "GITHUB_TOKEN=$(GITHUB_TOKEN)" \
			|| _skip GITHUB_TOKEN GitHub; \
	} || echo "⚠  openshell CLI not found — skipping provider registration."

# Symlink docker/Dockerfile.orchestrator → ./Dockerfile so the build
# context is the project root.  Cleaned up immediately after.
.PHONY: setup-sandbox
setup-sandbox: ## Build image, create sandbox, and start the app inside it
	@echo "Creating orchestrator sandbox..."
	@command -v openshell >/dev/null 2>&1 && { \
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
	PYTHONPATH=src python -m $(MAIN_MODULE)

.PHONY: run-local-sandbox
run-local-sandbox: setup-gateway setup-providers setup-sandbox ## (Re)create and run the orchestrator in the sandbox

.PHONY: run-broker
run-broker: ## Run the NMB broker locally
	PYTHONPATH=src python -m $(BROKER_MODULE) --audit-db $(AUDIT_DB)

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
	PYTHONPATH=src pytest tests/ -v --ignore=tests/integration

.PHONY: test-integration
test-integration: ## Run multi-sandbox NMB integration tests
	PYTHONPATH=src pytest tests/integration/ -v

.PHONY: test-all
test-all: ## Run all tests (unit + integration)
	PYTHONPATH=src pytest tests/ -v

.PHONY: test-auth
test-auth: ## Verify .env credentials against their APIs (no sandbox needed)
	@PYTHONPATH=src python scripts/test_auth.py

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
	ruff check src/ tests/
	ruff format --check src/ tests/
	mypy src/

.PHONY: typecheck
typecheck: ## Run mypy type checks
	mypy src/

.PHONY: fmt
fmt: ## Auto-format code
	ruff format src/ tests/
	ruff check --fix src/ tests/

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

.PHONY: logs
logs: ## Tail orchestrator logs
	@command -v openshell >/dev/null 2>&1 && \
		openshell logs $(SANDBOX_NAME) --follow 2>/dev/null || \
		tail -f logs/*.log 2>/dev/null || echo "No log files found"

.PHONY: status
status: ## Print sandbox and provider status
	@command -v openshell >/dev/null 2>&1 && { \
		echo "=== Gateway ==="; openshell status 2>/dev/null || echo "(not running)"; echo ""; \
		echo "=== Providers ==="; openshell provider list 2>/dev/null || echo "(none)"; echo ""; \
		echo "=== Sandbox ==="; openshell sandbox get $(SANDBOX_NAME) 2>/dev/null || echo "(not created)"; \
	} || echo "openshell not installed"

# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

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
clean: ## Delete sandbox, providers, and local image
	@command -v openshell >/dev/null 2>&1 && { \
		openshell sandbox delete $(SANDBOX_NAME) 2>/dev/null || true; \
		for p in $(ALL_PROVIDERS); do \
			openshell provider delete "$$p" >/dev/null 2>&1 || true; \
		done; \
	} || true
	@docker rmi $(IMAGE_NAME):$(IMAGE_TAG) 2>/dev/null || true

.PHONY: clean-all
clean-all: clean ## clean + stop the gateway
	@command -v openshell >/dev/null 2>&1 && openshell gateway stop 2>/dev/null || true
