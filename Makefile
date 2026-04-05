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

IMAGE_NAME := nemoclaw-orchestrator
IMAGE_TAG  := latest
POLICY     := policies/orchestrator.yaml

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

.PHONY: setup-gateway
setup-gateway: ## Start the OpenShell gateway if not already running
	@command -v openshell >/dev/null 2>&1 && { \
		if openshell status >/dev/null 2>&1; then \
			echo "✓ Gateway already running."; \
		else \
			echo "Starting OpenShell gateway..."; \
			openshell gateway destroy --name openshell 2>/dev/null || true; \
			openshell gateway start; \
		fi; \
	} || echo "⚠  openshell CLI not found — skipping gateway setup."

# Inference provider must be 'nvidia' type (not 'generic') so that
# openshell inference routing works via inference.local.
# Slack uses 'generic' for user-defined env var names.
.PHONY: setup-secrets
setup-secrets: .env ## Register inference and Slack providers with the gateway
	@echo "Registering providers..."
	@command -v openshell >/dev/null 2>&1 && { \
		openshell provider create \
			--name inference-hub \
			--type nvidia \
			--credential "NVIDIA_API_KEY=$$(grep INFERENCE_HUB_API_KEY .env | cut -d= -f2-)" \
		&& echo "✓ Inference provider registered." \
		|| echo "⚠  Provider may already exist (use 'openshell provider update' to change)."; \
		openshell inference set \
			--provider inference-hub \
			--model "$${INFERENCE_MODEL:-azure/anthropic/claude-opus-4-6}" \
			--no-verify \
		&& echo "✓ Inference routing configured (inference.local → inference-hub)." \
		|| echo "⚠  Inference routing may already be configured."; \
		openshell provider create \
			--name slack-credentials \
			--type generic \
			--credential "SLACK_BOT_TOKEN=$$(grep SLACK_BOT_TOKEN .env | cut -d= -f2-)" \
			--credential "SLACK_APP_TOKEN=$$(grep SLACK_APP_TOKEN .env | cut -d= -f2-)" \
		&& echo "✓ Slack provider registered." \
		|| echo "⚠  Provider may already exist."; \
	} || echo "⚠  openshell CLI not found — skipping provider registration."

# OpenShell's `--from` flag accepts a Dockerfile path or a directory:
#   - Dockerfile path  → parent directory becomes the build context
#   - Directory path   → that directory is the context; must contain a `Dockerfile`
#
# We keep Dockerfiles under docker/ for organisation (multiple images planned),
# but the build context must be the project root so COPY can reach pyproject.toml,
# README.md, src/, etc.  Passing `--from docker/Dockerfile.orchestrator` would set
# context to docker/, which lacks those files.
#
# Workaround: create a temporary symlink at the project root so `--from .` finds
# a Dockerfile while using `.` as the context.  The symlink is removed immediately
# after and is listed in .gitignore so it never gets committed.
.PHONY: setup-sandbox
setup-sandbox: ## Build image in the cluster and create the orchestrator sandbox
	@echo "Creating orchestrator sandbox..."
	@command -v openshell >/dev/null 2>&1 && { \
		openshell sandbox delete orchestrator 2>/dev/null || true; \
		ln -sf docker/Dockerfile.orchestrator Dockerfile; \
		openshell sandbox create \
			--name orchestrator \
			--from . \
			--policy $(POLICY) \
			--provider inference-hub \
			--provider slack-credentials \
			-- python -m nemoclaw_escapades.main; \
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
	PYTHONPATH=src python -m nemoclaw_escapades.main

.PHONY: run-local-sandbox
run-local-sandbox: setup-gateway setup-secrets setup-sandbox ## (Re)create and run the orchestrator in the OpenShell sandbox

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

.PHONY: build
build: ## Build the orchestrator container image
	docker build -f docker/Dockerfile.orchestrator -t $(IMAGE_NAME):$(IMAGE_TAG) .

# ---------------------------------------------------------------------------
# Development
# ---------------------------------------------------------------------------

.PHONY: test-auth
test-auth: ## Verify all .env credentials against their APIs
	@scripts/test_auth.sh

.PHONY: test
test: ## Run the test suite
	PYTHONPATH=src pytest tests/ -v

.PHONY: lint
lint: ## Run linters and type checks
	ruff check src/ tests/
	ruff format --check src/ tests/
	mypy src/

.PHONY: fmt
fmt: ## Auto-format code
	ruff format src/ tests/
	ruff check --fix src/ tests/

.PHONY: logs
logs: ## Tail orchestrator logs (sandbox or local)
	@command -v openshell >/dev/null 2>&1 && \
		openshell logs orchestrator --follow 2>/dev/null || \
		tail -f logs/*.log 2>/dev/null || echo "No log files found"

.PHONY: status
status: ## Print sandbox and provider status
	@command -v openshell >/dev/null 2>&1 && { \
		echo "=== Gateway ==="; openshell status 2>/dev/null || echo "(not running)"; echo ""; \
		echo "=== Providers ==="; openshell provider list 2>/dev/null || echo "(none)"; echo ""; \
		echo "=== Sandbox ==="; openshell sandbox get orchestrator 2>/dev/null || echo "(not created)"; \
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
		openshell sandbox delete orchestrator 2>/dev/null || true; \
		openshell provider delete inference-hub 2>/dev/null || true; \
		openshell provider delete slack-credentials 2>/dev/null || true; \
	} || true
	@docker rmi $(IMAGE_NAME):$(IMAGE_TAG) 2>/dev/null || true

.PHONY: clean-all
clean-all: clean ## clean + stop the gateway
	@command -v openshell >/dev/null 2>&1 && openshell gateway stop 2>/dev/null || true
