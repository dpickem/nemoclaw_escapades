# NemoClaw Escapades Makefile
#
# Quick start:
#   cp .env.example .env        # fill in real values
#   make setup                  # gateway + providers + sandbox (one-time)
#   make run-local-dev          # start the orchestrator outside a sandbox
#   make run-local-sandbox      # start the orchestrator inside the sandbox
#
# The sandbox runs inside OpenShell with proxy-mediated credential injection.
# See policies/orchestrator.yaml for how Slack and inference credentials
# flow through the proxy.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Load .env if present (for local targets that need vars)
ifneq (,$(wildcard .env))
  include .env
  export
endif

# ---------------------------------------------------------------------------
# Constants — change these to reconfigure names, paths, and defaults
# ---------------------------------------------------------------------------

# Docker / image
IMAGE_NAME := nemoclaw-orchestrator
IMAGE_TAG  := latest
DOCKERFILE := docker/Dockerfile.orchestrator

# OpenShell — gateway, sandbox, providers
GATEWAY_NAME       := openshell
GATEWAY_CONTAINER  := openshell-cluster-$(GATEWAY_NAME)
SANDBOX_NAME       := orchestrator
POLICY             := policies/orchestrator.yaml
INFERENCE_PROVIDER := inference-hub
INFERENCE_TYPE     := openai
SLACK_PROVIDER     := slack-credentials
DEFAULT_MODEL      := azure/anthropic/claude-opus-4-6

# Jira integration
JIRA_PROVIDER := jira-credentials

# Python entry points
MAIN_MODULE   := nemoclaw_escapades.main
BROKER_MODULE := nemoclaw_escapades.nmb.broker
AUDIT_DB      := .audit.db

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

.PHONY: help
help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Setup (run once, idempotent)
# ---------------------------------------------------------------------------

.PHONY: setup
setup: install setup-gateway setup-secrets setup-sandbox ## One-time: deps + gateway + providers + sandbox

.PHONY: install
install: ## Install the package and dev dependencies
	pip install -e ".[dev]"

# OpenShell v0.0.21 limitation: `openshell gateway start` cannot restart a
# stopped gateway — it only offers "Destroy and recreate?", which re-downloads
# the image and loses all provider/routing config.  The fastest recovery path
# is `docker start <container>` which preserves everything.  We try that first,
# and only fall back to a fresh `openshell gateway start` when no container
# exists at all.  See Lesson #18 in the M1 blog post.
.PHONY: setup-gateway
setup-gateway: ## Start the OpenShell gateway if not already running
	@command -v openshell >/dev/null 2>&1 && { \
		if openshell status >/dev/null 2>&1; then \
			echo "✓ Gateway already running."; \
		elif docker inspect $(GATEWAY_CONTAINER) >/dev/null 2>&1; then \
			echo "Gateway container exists but is stopped — restarting via docker..."; \
			docker start $(GATEWAY_CONTAINER); \
			echo "Waiting for k3s to initialise..."; \
			sleep 10; \
			if openshell status >/dev/null 2>&1; then \
				echo "✓ Gateway restarted (providers and routing preserved)."; \
			else \
				echo "⚠  Gateway not ready yet — try 'openshell status' in a few seconds."; \
			fi; \
		else \
			echo "No existing gateway — creating from scratch..."; \
			openshell gateway start; \
		fi; \
	} || echo "⚠  openshell CLI not found — skipping gateway setup."

# Inference provider must be 'openai' type (not 'nvidia' or 'generic') so that
# openshell inference routing works via inference.local.  The 'openai' type
# with an explicit base URL override points to the endpoint from .env.
# Slack uses 'generic' for user-defined env var names.
# All provider targets are idempotent: delete-then-create ensures a
# clean state regardless of whether the provider already exists.
.PHONY: setup-secrets
setup-secrets: .env ## Register inference and Slack providers with the gateway
	@echo "Registering providers..."
	@command -v openshell >/dev/null 2>&1 && { \
		openshell provider delete $(INFERENCE_PROVIDER) >/dev/null 2>&1 || true; \
		openshell provider create \
			--name $(INFERENCE_PROVIDER) \
			--type $(INFERENCE_TYPE) \
			--credential "OPENAI_API_KEY=$$(grep INFERENCE_HUB_API_KEY .env | cut -d= -f2-)" \
			--config "OPENAI_BASE_URL=$${INFERENCE_HUB_BASE_URL}" \
		&& echo "✓ Inference provider registered."; \
		openshell inference set \
			--provider $(INFERENCE_PROVIDER) \
			--model "$${INFERENCE_MODEL:-$(DEFAULT_MODEL)}" \
			--no-verify \
		&& echo "✓ Inference routing configured (inference.local → $(INFERENCE_PROVIDER))."; \
		openshell provider delete $(SLACK_PROVIDER) >/dev/null 2>&1 || true; \
		openshell provider create \
			--name $(SLACK_PROVIDER) \
			--type generic \
			--credential "SLACK_BOT_TOKEN=$$(grep SLACK_BOT_TOKEN .env | cut -d= -f2-)" \
			--credential "SLACK_APP_TOKEN=$$(grep SLACK_APP_TOKEN .env | cut -d= -f2-)" \
		&& echo "✓ Slack provider registered."; \
	} || echo "⚠  openshell CLI not found — skipping provider registration."

.PHONY: setup-jira-provider
setup-jira-provider: .env ## Register Jira credentials provider with the gateway
	@command -v openshell >/dev/null 2>&1 && { \
		AUTH=$$(grep '^JIRA_AUTH=' .env | cut -d= -f2-); \
		if [ -z "$$AUTH" ]; then \
			echo "⚠  JIRA_AUTH not set in .env — skipping."; \
		else \
			openshell provider delete $(JIRA_PROVIDER) >/dev/null 2>&1 || true; \
			openshell provider create \
				--name $(JIRA_PROVIDER) \
				--type generic \
				--credential "JIRA_AUTH=$$AUTH" \
			&& echo "✓ Jira provider registered."; \
		fi; \
	} || echo "⚠  openshell CLI not found."

# OpenShell's `--from` flag accepts a Dockerfile path or a directory.
# We symlink docker/Dockerfile.orchestrator to ./Dockerfile so the build
# context is the project root.  The symlink is cleaned up immediately.
.PHONY: setup-sandbox
setup-sandbox: ## Build image, create sandbox, and start the app inside it
	@echo "Creating orchestrator sandbox..."
	@command -v openshell >/dev/null 2>&1 && { \
		openshell sandbox delete $(SANDBOX_NAME) 2>/dev/null || true; \
		ln -sf $(DOCKERFILE) Dockerfile; \
		JIRA_FLAG=""; \
		if openshell provider get $(JIRA_PROVIDER) >/dev/null 2>&1; then \
			JIRA_FLAG="--provider $(JIRA_PROVIDER)"; \
			echo "  Attaching Jira credentials provider."; \
		fi; \
		openshell sandbox create \
			--name $(SANDBOX_NAME) \
			--from . \
			--policy $(POLICY) \
			--provider $(INFERENCE_PROVIDER) \
			--provider $(SLACK_PROVIDER) \
			$$JIRA_FLAG \
			-- python -m $(MAIN_MODULE); \
		rm -f Dockerfile; \
	} || echo "⚠  openshell CLI not found — skipping sandbox creation."

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

# Credentials come from the `include .env` / `export` block at the top of
# this Makefile.  Make parses each VAR=value line as a Make variable, and
# `export` pushes all Make variables into the environment of child processes.
# The Python app reads them via os.environ — it never touches .env directly.
.PHONY: run-local-dev
run-local-dev: ## Run the orchestrator outside a sandbox (bare process, .env creds)
	PYTHONPATH=src python -m $(MAIN_MODULE)

.PHONY: run-local-sandbox
run-local-sandbox: setup-gateway setup-secrets setup-sandbox ## (Re)create and run the orchestrator in the OpenShell sandbox

.PHONY: run-broker
run-broker: ## Run the NMB broker locally
	PYTHONPATH=src python -m $(BROKER_MODULE) \
		--audit-db $(AUDIT_DB)

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

.PHONY: build
build: ## Build the orchestrator container image
	docker build -f $(DOCKERFILE) -t $(IMAGE_NAME):$(IMAGE_TAG) .

# ---------------------------------------------------------------------------
# Development
# ---------------------------------------------------------------------------

.PHONY: test-auth
test-auth: ## Verify all .env credentials against their APIs
	@scripts/test_auth.sh

.PHONY: test-jira-sandbox
test-jira-sandbox: ## Test Jira connectivity from inside the sandbox (sandbox must be running)
	@ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR \
		-o "ProxyCommand=openshell ssh-proxy --gateway-name $(GATEWAY_NAME) --name $(SANDBOX_NAME)" \
		sandbox@openshell-$(SANDBOX_NAME) "python3 /app/scripts/test_jira_sandbox.py"

.PHONY: test
test: ## Run the unit test suite (excludes integration tests)
	PYTHONPATH=src pytest tests/ -v --ignore=tests/integration

.PHONY: test-integration
test-integration: ## Run multi-sandbox NMB integration tests
	PYTHONPATH=src pytest tests/integration/ -v

.PHONY: test-all
test-all: ## Run all tests (unit + integration)
	PYTHONPATH=src pytest tests/ -v

.PHONY: lint
lint: ## Run linters and type checks
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

.PHONY: logs
logs: ## Tail orchestrator logs (sandbox or local)
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
		openshell provider delete $(INFERENCE_PROVIDER) >/dev/null 2>&1 || true; \
		openshell provider delete $(SLACK_PROVIDER) >/dev/null 2>&1 || true; \
		openshell provider delete $(JIRA_PROVIDER) >/dev/null 2>&1 || true; \
	} || true
	@docker rmi $(IMAGE_NAME):$(IMAGE_TAG) 2>/dev/null || true

.PHONY: clean-all
clean-all: clean ## clean + stop the gateway
	@command -v openshell >/dev/null 2>&1 && openshell gateway stop 2>/dev/null || true
