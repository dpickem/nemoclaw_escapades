# Browser-to-sandbox HTTP prototype Makefile.
#
# Intended use:
#   make smoke        # full OpenShell proof: start sandbox, forward port 8000,
#                     # run the host probe, then clean up sandbox/forward
#   make local-smoke  # debug the same server/client flow without OpenShell
#   make help         # list lower-level probe, logs, stop, and clean targets
#
# Run from this directory or from the repo root with:
#   make -C prototypes/browser_sandbox_http smoke
#
# Override PORT, SANDBOX_NAME, or OUTBOUND_URL at invocation time when needed.

# Use bash for traps, strict mode, and multi-line shell recipes.
SHELL := /bin/bash

# Fail recipes on unset variables, non-zero commands, or failed pipeline stages.
.SHELLFLAGS := -euo pipefail -c

# Show target help when `make` is run without an explicit target.
.DEFAULT_GOAL := help

# Absolute repo root. Recipes cd here before invoking `openshell --from` so
# relative paths match the command examples in the README.
ROOT := $(abspath ../..)

# Absolute path to this prototype directory. Used for logs and host-side scripts.
THIS_DIR := $(abspath .)

# OpenShell sandbox name used by the full smoke test. Override when running
# multiple copies in parallel or preserving another sandbox with the default name.
SANDBOX_NAME ?= browser-http-probe

# Port forwarded from the host to the sandbox HTTP server. Defaults to 8000
# because the local OpenShell gateway commonly owns 8080.
PORT ?= 8000

# Host-visible URL used by the host-side probe after OpenShell creates the
# forward. Usually derived from PORT; override for custom bind addresses.
BASE_URL ?= http://127.0.0.1:$(PORT)

# External URL fetched by the sandbox server through its outbound HTTPS policy.
# Keep this aligned with policy.yaml unless the policy is widened for a test.
OUTBOUND_URL ?= https://example.com/

# Host-only debug port used by `make local-smoke`; separate from PORT so a local
# debug server does not collide with an OpenShell forward.
LOCAL_PORT ?= 18000

# Host-only debug URL used by the local smoke client.
LOCAL_BASE_URL ?= http://127.0.0.1:$(LOCAL_PORT)

# Runtime scratch directory for logs and background process IDs. Ignored by git.
STATE_DIR := $(THIS_DIR)/.state

# Captures the background `openshell sandbox create` output for debugging failed
# provisioning, forwarding, or probe attempts.
SANDBOX_LOG := $(STATE_DIR)/sandbox.log

# PID of the background host-side `openshell sandbox create` command so `stop`
# can terminate it after deleting the sandbox.
SANDBOX_PID := $(STATE_DIR)/sandbox.pid

# Captures the local debug server output from `make local-smoke`.
LOCAL_LOG := $(STATE_DIR)/local.log

# PID of the local debug server so `stop-local` can clean it up.
LOCAL_PID := $(STATE_DIR)/local.pid

# Repo-root-relative path passed to `openshell sandbox create --from`.
SOURCE_REL := prototypes/browser_sandbox_http

# Repo-root-relative policy path passed to `openshell sandbox create --policy`.
POLICY_REL := $(SOURCE_REL)/policy.yaml

# Host-side probe script. It retries `/api/health`, then checks echo and
# outbound HTTPS behavior.
HOST_CLIENT := $(THIS_DIR)/host_client.py

# Command executed inside the sandbox once OpenShell provisioning completes.
SANDBOX_SERVER := python /app/sandbox_http_server.py --host 0.0.0.0 --port $(PORT)

.PHONY: help
help: ## Show available prototype targets
	@awk 'BEGIN {FS = ":.*?## "}; /^[a-zA-Z0-9_-]+:.*?## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: smoke test
smoke test: check-gateway clean start-sandbox ## Run the full OpenShell browser-to-sandbox smoke test
	@trap '$(MAKE) --no-print-directory stop' EXIT; \
	$(MAKE) --no-print-directory probe

.PHONY: local-smoke
local-smoke: clean-local start-local ## Run the same HTTP probe without OpenShell
	@trap '$(MAKE) --no-print-directory stop-local' EXIT; \
	python3 "$(HOST_CLIENT)" --base-url "$(LOCAL_BASE_URL)" --outbound-url "$(OUTBOUND_URL)"

.PHONY: check-gateway
check-gateway: ## Verify OpenShell is installed and the gateway is reachable
	@command -v openshell >/dev/null 2>&1 || { echo "openshell CLI not found"; exit 1; }
	@openshell status >/dev/null 2>&1 || { echo "OpenShell gateway is not reachable"; exit 1; }

.PHONY: start-sandbox
start-sandbox: ## Start the sandbox HTTP server with port forwarding
	@mkdir -p "$(STATE_DIR)"
	@rm -f "$(SANDBOX_LOG)" "$(SANDBOX_PID)"
	@openshell sandbox delete "$(SANDBOX_NAME)" >/dev/null 2>&1 || true
	@echo "Starting $(SANDBOX_NAME); logs: $(SANDBOX_LOG)"
	@cd "$(ROOT)" && nohup openshell sandbox create \
		--name "$(SANDBOX_NAME)" \
		--from "$(SOURCE_REL)" \
		--policy "$(POLICY_REL)" \
		--forward "$(PORT)" \
		--no-tty \
		-- $(SANDBOX_SERVER) \
		> "$(SANDBOX_LOG)" 2>&1 & echo $$! > "$(SANDBOX_PID)"

.PHONY: probe
probe: ## Probe the forwarded sandbox service from the host
	@python3 "$(HOST_CLIENT)" --base-url "$(BASE_URL)" --outbound-url "$(OUTBOUND_URL)" || { \
		echo ""; \
		echo "Sandbox probe failed. Last sandbox log lines:"; \
		tail -n 120 "$(SANDBOX_LOG)" 2>/dev/null || true; \
		exit 1; \
	}

.PHONY: probe-no-outbound
probe-no-outbound: ## Probe only host/browser -> sandbox reachability
	@python3 "$(HOST_CLIENT)" --base-url "$(BASE_URL)" --skip-outbound

.PHONY: start-local
start-local: ## Start the HTTP server directly on the host for script debugging
	@mkdir -p "$(STATE_DIR)"
	@rm -f "$(LOCAL_LOG)" "$(LOCAL_PID)"
	@echo "Starting local server; logs: $(LOCAL_LOG)"
	@nohup python3 "$(THIS_DIR)/sandbox_http_server.py" \
		--host 127.0.0.1 \
		--port "$(LOCAL_PORT)" \
		> "$(LOCAL_LOG)" 2>&1 & echo $$! > "$(LOCAL_PID)"

.PHONY: logs
logs: ## Print sandbox and local prototype logs
	@echo "=== sandbox ==="
	@test ! -f "$(SANDBOX_LOG)" || sed -n '1,260p' "$(SANDBOX_LOG)"
	@echo "=== local ==="
	@test ! -f "$(LOCAL_LOG)" || sed -n '1,160p' "$(LOCAL_LOG)"

.PHONY: stop
stop: ## Stop sandbox, forward, and background OpenShell command
	@openshell sandbox delete "$(SANDBOX_NAME)" >/dev/null 2>&1 || true
	@if [ -f "$(SANDBOX_PID)" ]; then \
		kill "$$(cat "$(SANDBOX_PID)")" >/dev/null 2>&1 || true; \
	fi

.PHONY: stop-local
stop-local: ## Stop the local debug server
	@if [ -f "$(LOCAL_PID)" ]; then \
		kill "$$(cat "$(LOCAL_PID)")" >/dev/null 2>&1 || true; \
	fi

.PHONY: clean clean-local
clean: stop clean-local ## Stop everything and remove runtime logs
	@rm -rf "$(STATE_DIR)"

clean-local: stop-local ## Stop local server and remove local runtime files
	@rm -f "$(LOCAL_LOG)" "$(LOCAL_PID)"
