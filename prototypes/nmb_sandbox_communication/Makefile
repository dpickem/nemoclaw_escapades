# NMB sandbox-to-sandbox communication prototype.
#
# Purpose:
#   Prove the dashboard design's riskiest NMB path with real OpenShell
#   sandboxes: one sandbox runs an NMB broker, a second sandbox connects to it
#   under an NMB-only egress policy, and the two exchange a minimal
#   task.assign -> task.complete workflow.
#
# Workflow:
#   1. `start-broker-sandbox` creates the broker sandbox, starts the broker on
#      0.0.0.0:9876, and exposes it with an operator-created OpenShell forward.
#   2. `probe-host-broker` verifies the host can open a WebSocket through that
#      forward before a second sandbox is involved.
#   3. `probe-client-sandbox` starts a short-lived client sandbox and diagnoses
#      DNS, proxy, TCP, and WebSocket reachability to the forwarded broker.
#   4. `run-client-sandbox` starts a fresh client sandbox that publishes
#      client.ready, receives task.assign, and sends task.complete.
#   5. `download-audit` and `audit-summary` prove the broker sandbox's audit DB
#      recorded the routed NMB messages.
#
# Run:
#   make -C prototypes/nmb_sandbox_communication smoke
#
# Notes:
#   `--forward` is intentional. In this OpenShell setup, sandbox service DNS and
#   messages.local did not reach the process inside OpenShell's nested runtime
#   network namespace. The working route mirrors the browser_sandbox_http
#   prototype: operator-created inbound forward plus explicit sandbox egress to
#   host.docker.internal through the OpenShell proxy.

# Use bash with fail-fast flags so multi-command recipes stop on errors,
# undefined variables, and failed pipeline stages.
SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -c

# Show help when `make` is run without an explicit target.
.DEFAULT_GOAL := help

# Absolute repo root. Commands that build images run from here so Docker sees
# the full repository context (`pyproject.toml`, `src/`, and this prototype).
ROOT := $(abspath ../..)

# Absolute path to this prototype directory. Runtime logs and downloaded audit
# artifacts are kept relative to this directory.
THIS_DIR := $(abspath .)

# OpenShell sandbox name for the broker side of the smoke test.
BROKER_SANDBOX ?= nmb-broker-smoke

# OpenShell sandbox name for the short-lived client/probe side.
CLIENT_SANDBOX ?= nmb-client-smoke

# NMB WebSocket port. The broker binds this inside its sandbox and OpenShell
# forwards the same port on the host.
NMB_PORT ?= 9876

# Local bind address for the broker sandbox forward. 0.0.0.0 is required so a
# second sandbox can reach the forwarded host listener via host.docker.internal.
BROKER_FORWARD_BIND ?= 0.0.0.0:$(NMB_PORT)

# Hostname the client sandbox uses for the forwarded broker endpoint.
BROKER_HOST ?= host.docker.internal

# Broker WebSocket URL used inside the client sandbox.
BROKER_URL ?= ws://$(BROKER_HOST):$(NMB_PORT)

# Broker WebSocket URL used by the host-side preflight probe.
HOST_BROKER_URL ?= ws://127.0.0.1:$(NMB_PORT)

# Stable task identifier expected in task.assign and task.complete payloads.
TASK_ID ?= nmb-smoke-001

# Timeout in seconds for broker/client smoke waits. Kept generous because
# OpenShell image pulls and sandbox startup can be slow on first run.
SMOKE_TIMEOUT ?= 600

# Repository-relative directory containing this prototype.
SOURCE_REL := prototypes/nmb_sandbox_communication

# Repository-relative Dockerfile path. The Makefile symlinks this to
# `$(ROOT)/Dockerfile` because `openshell sandbox create --from .` expects a
# Dockerfile at the build context root.
DOCKERFILE_REL := $(SOURCE_REL)/Dockerfile

# Temporary root-level Dockerfile symlink created for OpenShell image builds.
TEMP_DOCKERFILE := $(ROOT)/Dockerfile

# Repository-relative broker sandbox policy path.
BROKER_POLICY_REL := $(SOURCE_REL)/policies/nmb-broker.yaml

# Repository-relative client sandbox policy path.
CLIENT_POLICY_REL := $(SOURCE_REL)/policies/nmb-client.yaml

# Host-side path to the Python smoke driver.
SMOKE_SCRIPT := $(SOURCE_REL)/nmb_smoke.py

# In-container path to the copied Python smoke driver.
SANDBOX_SMOKE_SCRIPT := /app/$(SMOKE_SCRIPT)

# Local runtime artifact directory. It is ignored by git.
STATE_DIR := $(THIS_DIR)/.state

# Broker sandbox creation and broker-process log.
BROKER_LOG := $(STATE_DIR)/broker.log

# Client sandbox creation/probe log.
CLIENT_LOG := $(STATE_DIR)/client.log

# PID of the background host-side `openshell sandbox create` command for the
# broker sandbox.
BROKER_PID := $(STATE_DIR)/broker.pid

# Local copy of the broker sandbox audit database downloaded after a smoke run.
AUDIT_DB := $(STATE_DIR)/nmb-smoke-audit.db

.PHONY: help
help: ## Show available prototype targets
	@awk 'BEGIN {FS = ":.*?## "}; /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

.PHONY: smoke
smoke: check-gateway ## Run the two-sandbox NMB smoke test
	@trap '$(MAKE) --no-print-directory stop' EXIT; \
	$(MAKE) --no-print-directory clean; \
	$(MAKE) --no-print-directory start-broker-sandbox; \
	$(MAKE) --no-print-directory wait-broker-ready; \
	$(MAKE) --no-print-directory probe-host-broker; \
	$(MAKE) --no-print-directory probe-client-sandbox; \
	$(MAKE) --no-print-directory run-client-sandbox; \
	$(MAKE) --no-print-directory wait-smoke-success; \
	$(MAKE) --no-print-directory download-audit; \
	$(MAKE) --no-print-directory audit-summary; \
	echo "✓ NMB two-sandbox smoke test completed."

.PHONY: debug-client
debug-client: check-gateway ## Run only broker + client-side reachability diagnostics
	@trap '$(MAKE) --no-print-directory stop' EXIT; \
	$(MAKE) --no-print-directory clean; \
	$(MAKE) --no-print-directory start-broker-sandbox; \
	$(MAKE) --no-print-directory wait-broker-ready; \
	$(MAKE) --no-print-directory probe-host-broker; \
	$(MAKE) --no-print-directory probe-client-sandbox

.PHONY: check-gateway
check-gateway: ## Verify the OpenShell gateway is already reachable
	@command -v openshell >/dev/null 2>&1 || { echo "openshell CLI not found"; exit 1; }
	@openshell status >/dev/null 2>&1 || { echo "OpenShell gateway is not reachable"; exit 1; }

.PHONY: start-broker-sandbox
start-broker-sandbox: ## Launch broker sandbox with the broker policy
	@mkdir -p "$(STATE_DIR)"
	@rm -f "$(BROKER_LOG)" "$(BROKER_PID)"
	@openshell sandbox delete "$(BROKER_SANDBOX)" >/dev/null 2>&1 || true
	@cd "$(ROOT)" && ln -sf "$(DOCKERFILE_REL)" Dockerfile
	@echo "Starting $(BROKER_SANDBOX); logs: $(BROKER_LOG)"
	@cd "$(ROOT)" && nohup bash -c 'openshell sandbox create \
		--name "$(BROKER_SANDBOX)" \
		--from . \
		--policy "$(BROKER_POLICY_REL)" \
		--forward "$(BROKER_FORWARD_BIND)" \
		--no-tty \
		-- python "$(SANDBOX_SMOKE_SCRIPT)" broker \
			--port "$(NMB_PORT)" \
			--task-id "$(TASK_ID)" \
			--timeout "$(SMOKE_TIMEOUT)" \
		> "$(BROKER_LOG)" 2>&1' >/dev/null 2>&1 & echo $$! > "$(BROKER_PID)"

.PHONY: wait-broker-ready
wait-broker-ready: ## Wait until broker sandbox reports readiness
	@echo "Waiting for broker readiness..."
	@for _ in $$(seq 1 300); do \
		if [ -f "$(BROKER_LOG)" ] && grep -q "NMB_SMOKE_BROKER_READY" "$(BROKER_LOG)"; then \
			echo "✓ Broker is ready."; \
			exit 0; \
		fi; \
		if [ -f "$(BROKER_LOG)" ] && grep -q "Traceback\|ERROR\|Cannot connect" "$(BROKER_LOG)"; then \
			echo "Broker failed before readiness:"; \
			sed -n '1,220p' "$(BROKER_LOG)"; \
			exit 1; \
		fi; \
		sleep 1; \
	done; \
	echo "Timed out waiting for broker readiness. Last broker log lines:"; \
	tail -n 80 "$(BROKER_LOG)" 2>/dev/null || true; \
	exit 1

.PHONY: probe-host-broker
probe-host-broker: ## Verify host can reach the forwarded broker WebSocket
	@echo "Probing forwarded broker from host at $(HOST_BROKER_URL)..."
	@cd "$(ROOT)" && PYTHONPATH=src python3 "$(SMOKE_SCRIPT)" probe \
		--broker-url "$(HOST_BROKER_URL)" \
		--task-id "$(TASK_ID)" \
		--timeout "$(SMOKE_TIMEOUT)"

.PHONY: run-client-sandbox
run-client-sandbox: ## Launch client sandbox with NMB-only egress policy
	@mkdir -p "$(STATE_DIR)"
	@rm -f "$(CLIENT_LOG)"
	@openshell sandbox delete "$(CLIENT_SANDBOX)" >/dev/null 2>&1 || true
	@echo "Starting $(CLIENT_SANDBOX); logs: $(CLIENT_LOG)"
	@image="$$(rg -o 'openshell/sandbox-from:[0-9]+' "$(BROKER_LOG)" | tail -n 1)"; \
	[ -n "$$image" ] || { echo "Could not find broker image tag in $(BROKER_LOG)"; exit 1; }; \
	echo "Reusing broker image $$image"; \
	cd "$(ROOT)" && openshell sandbox create \
		--name "$(CLIENT_SANDBOX)" \
		--from "$$image" \
		--policy "$(CLIENT_POLICY_REL)" \
		--no-keep \
		--no-tty \
		-- python "$(SANDBOX_SMOKE_SCRIPT)" client \
			--broker-url "$(BROKER_URL)" \
			--task-id "$(TASK_ID)" \
			--timeout "$(SMOKE_TIMEOUT)" \
		2>&1 | tee "$(CLIENT_LOG)"

.PHONY: probe-client-sandbox
probe-client-sandbox: ## Diagnose broker reachability from the client sandbox
	@mkdir -p "$(STATE_DIR)"
	@rm -f "$(CLIENT_LOG)"
	@openshell sandbox delete "$(CLIENT_SANDBOX)" >/dev/null 2>&1 || true
	@echo "Probing client sandbox egress to $(BROKER_URL); logs: $(CLIENT_LOG)"
	@image="$$(rg -o 'openshell/sandbox-from:[0-9]+' "$(BROKER_LOG)" | tail -n 1)"; \
	[ -n "$$image" ] || { echo "Could not find broker image tag in $(BROKER_LOG)"; exit 1; }; \
	echo "Reusing broker image $$image"; \
	cd "$(ROOT)" && openshell sandbox create \
		--name "$(CLIENT_SANDBOX)" \
		--from "$$image" \
		--policy "$(CLIENT_POLICY_REL)" \
		--no-keep \
		--no-tty \
		-- python "$(SANDBOX_SMOKE_SCRIPT)" client-probe \
			--broker-url "$(BROKER_URL)" \
			--task-id "$(TASK_ID)" \
			--timeout "$(SMOKE_TIMEOUT)" \
		2>&1 | tee "$(CLIENT_LOG)"

.PHONY: wait-smoke-success
wait-smoke-success: ## Wait for the broker side success marker
	@echo "Waiting for broker-side success marker..."
	@for _ in $$(seq 1 120); do \
		if [ -f "$(BROKER_LOG)" ] && grep -q "NMB_SMOKE_SUCCESS" "$(BROKER_LOG)"; then \
			echo "✓ Broker observed task.complete."; \
			exit 0; \
		fi; \
		if [ -f "$(BROKER_LOG)" ] && grep -q "Traceback\|ERROR" "$(BROKER_LOG)"; then \
			echo "Broker failed during smoke test:"; \
			sed -n '1,260p' "$(BROKER_LOG)"; \
			exit 1; \
		fi; \
		sleep 1; \
	done; \
	echo "Timed out waiting for success. Broker log:"; \
	tail -n 120 "$(BROKER_LOG)" 2>/dev/null || true; \
	exit 1

.PHONY: download-audit
download-audit: ## Download the broker sandbox audit DB
	@rm -f "$(AUDIT_DB)"
	@openshell sandbox download "$(BROKER_SANDBOX)" /sandbox/nmb-smoke-audit.db "$(AUDIT_DB)" >/dev/null
	@if [ -d "$(AUDIT_DB)" ]; then \
		mv "$(AUDIT_DB)/nmb-smoke-audit.db" "$(AUDIT_DB).tmp" 2>/dev/null \
			|| mv "$(AUDIT_DB)"/* "$(AUDIT_DB).tmp"; \
		rm -rf "$(AUDIT_DB)"; \
		mv "$(AUDIT_DB).tmp" "$(AUDIT_DB)"; \
	fi
	@echo "✓ Downloaded audit DB to $(AUDIT_DB)"

.PHONY: audit-summary
audit-summary: ## Print a compact audit DB summary
	@[ -f "$(AUDIT_DB)" ] || { echo "No audit DB at $(AUDIT_DB)"; exit 1; }
	@echo "Audit messages: $$(sqlite3 "$(AUDIT_DB)" "SELECT COUNT(*) FROM messages")"
	@sqlite3 -separator " | " "$(AUDIT_DB)" "SELECT '  ' || op, type, from_sandbox, COALESCE(to_sandbox, channel, '') FROM messages ORDER BY timestamp"

.PHONY: logs
logs: ## Show broker and client smoke logs
	@echo "=== broker ==="
	@test ! -f "$(BROKER_LOG)" || sed -n '1,260p' "$(BROKER_LOG)"
	@echo "=== client ==="
	@test ! -f "$(CLIENT_LOG)" || sed -n '1,220p' "$(CLIENT_LOG)"

.PHONY: stop
stop: ## Delete smoke-test sandboxes and stop port forwarding
	@openshell forward stop "$(NMB_PORT)" >/dev/null 2>&1 || true
	@openshell sandbox delete "$(CLIENT_SANDBOX)" >/dev/null 2>&1 || true
	@openshell sandbox delete "$(BROKER_SANDBOX)" >/dev/null 2>&1 || true
	@if [ -L "$(TEMP_DOCKERFILE)" ] && [ "$$(readlink "$(TEMP_DOCKERFILE)")" = "$(DOCKERFILE_REL)" ]; then \
		rm -f "$(TEMP_DOCKERFILE)"; \
	fi

.PHONY: clean
clean: stop ## Remove smoke-test runtime artifacts
	@rm -rf "$(STATE_DIR)"

