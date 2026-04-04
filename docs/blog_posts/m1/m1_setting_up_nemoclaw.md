# M1 — Building Our Own Agent: Local Orchestrator + NVIDIA Inference Hub (DRAFT)

![M1 architecture loop: local orchestrator to NVIDIA inference hub](./m1_slack_inference_orchestrator_loop.svg)

![M1 deliverables overview](./m1_foundation_deliverables_overview.svg)

---

## Table of Contents

- [Step-by-Step Setup Guide](#step-by-step-setup-guide)
  - [Prerequisites](#prerequisites)
  - [Step 1: Create a Slack App](#step-1-create-a-slack-app)
  - [Step 2: Get an NVIDIA Inference Hub API Key](#step-2-get-an-nvidia-inference-hub-api-key)
  - [Step 3: Clone and Configure](#step-3-clone-and-configure)
  - [Step 4: Install Dependencies](#step-4-install-dependencies)
  - [Step 5: Verify Credentials](#step-5-verify-credentials)
  - [Step 6: Run Locally](#step-6-run-locally)
  - [Step 7: Run All Tests](#step-7-run-all-tests)
- [Deploying to an OpenShell Sandbox](#deploying-to-an-openshell-sandbox)
  - [Step 8: Start the OpenShell Gateway](#step-8-start-the-openshell-gateway)
  - [Step 9: Register the Inference Provider](#step-9-register-the-inference-provider)
  - [Step 10: Configure Inference Routing](#step-10-configure-inference-routing)
  - [Step 11: Register the Slack Provider](#step-11-register-the-slack-provider)
  - [Step 12: Understand the Sandbox Network Policy](#step-12-understand-the-sandbox-network-policy)
  - [Step 13: Build and Deploy the Sandbox](#step-13-build-and-deploy-the-sandbox)
  - [Step 14: Verify the Deployment](#step-14-verify-the-deployment)
  - [Step 15: Debugging a Failed Deployment](#step-15-debugging-a-failed-deployment)
  - [Stopping and Cleaning Up](#stopping-and-cleaning-up)
  - [How Credentials Flow in the Sandbox](#how-credentials-flow-in-the-sandbox)
- [Why This Milestone Comes First](#why-this-milestone-comes-first)
- [What We Are Intending to Build](#what-we-are-intending-to-build)
- [What M1 Teaches](#what-m1-teaches)
- [Deployment Model](#deployment-model)
- [Architecture Flow](#architecture-flow)
- [Connector Implementation Walkthrough](#connector-implementation-walkthrough)
  - [The connector contract](#the-connector-contract)
  - [Event listening and filtering](#event-listening-and-filtering)
  - [Event normalization](#event-normalization)
  - [The thinking indicator pattern](#the-thinking-indicator-pattern)
  - [Error propagation and rate limiting](#error-propagation-and-rate-limiting)
  - [Rendering: RichResponse → Block Kit](#rendering-richresponse--block-kit)
- [Orchestrator Implementation Walkthrough](#orchestrator-implementation-walkthrough)
  - [Wiring: how the pieces connect](#wiring-how-the-pieces-connect)
  - [The handle() method: the full agent loop](#the-handle-method-the-full-agent-loop)
  - [Context assembly: _build_prompt](#context-assembly-_build_prompt)
  - [Inference dispatch and transcript repair](#inference-dispatch-and-transcript-repair)
  - [The inference backend contract](#the-inference-backend-contract)
  - [The approval gate](#the-approval-gate)
  - [Response construction](#response-construction)
  - [Observability](#observability)
- [Learning Objectives](#learning-objectives)
- [Deliverables](#deliverables)
- [Acceptance Criteria](#acceptance-criteria)
- [Why This Sets Up the Rest of the Series](#why-this-sets-up-the-rest-of-the-series)
- [OpenShell Sandbox Deployment — Lessons Learned](#openshell-sandbox-deployment--lessons-learned)
- [Sources and References](#sources-and-references)
- [Appendix: Original TODO Tracker](#appendix-original-todo-tracker)

---

## Step-by-Step Setup Guide

This guide takes you from a fresh clone to a fully working Slack bot — first
running locally, then deployed inside an OpenShell sandbox. Every command is
listed. Every credential is explained.

### Prerequisites

| Requirement | Version | Check |
|---|---|---|
| Python | 3.11+ | `python3 --version` |
| Docker Desktop | Running | `docker info` |
| OpenShell CLI | 0.0.21+ | `openshell --version` |
| A Slack workspace | You have admin access or can create apps | — |
| An NVIDIA account | For Inference Hub API keys | [build.nvidia.com](https://build.nvidia.com) |

### Step 1: Create a Slack App

You need two tokens: a **bot token** (`xoxb-...`) for API calls and an
**app-level token** (`xapp-...`) for Socket Mode.

1. Go to [api.slack.com/apps](https://api.slack.com/apps) and click
   **Create New App** → **From scratch**.
2. Name it (e.g. `dbot`), pick your workspace, and create it.

#### Enable Socket Mode

Socket Mode lets the bot receive events over a WebSocket instead of requiring
a public HTTP endpoint. This is critical for local development and for running
inside an OpenShell sandbox (which only allows outbound connections).

1. In the left sidebar, click **Socket Mode**.
2. Toggle **Enable Socket Mode** on.
3. When prompted, create an app-level token. Name it `socket-mode` and give it
   the `connections:write` scope.
4. Copy the token (`xapp-...`). This is your `SLACK_APP_TOKEN`.

#### Configure Bot Scopes

1. Go to **OAuth & Permissions** in the sidebar.
2. Under **Bot Token Scopes**, add:
   - `app_mentions:read` — see @mentions
   - `chat:write` — send messages
   - `im:history` — read DM history
   - `im:read` — see DM metadata
   - `im:write` — open DMs
   - `channels:history` — read channel history (for threaded replies)

#### Subscribe to Events

1. Go to **Event Subscriptions** in the sidebar.
2. Toggle **Enable Events** on.
3. Under **Subscribe to bot events**, add:
   - `message.im` — DM messages
   - `message.channels` — channel messages (optional, for @mention responses)
   - `app_mention` — @bot mentions

#### Install to Workspace

1. Go to **Install App** in the sidebar.
2. Click **Install to Workspace** and authorize.
3. Copy the **Bot User OAuth Token** (`xoxb-...`). This is your
   `SLACK_BOT_TOKEN`.

### Step 2: Get an NVIDIA Inference Hub API Key

1. Go to [build.nvidia.com](https://build.nvidia.com) and sign in.
2. Pick a model endpoint (e.g. search for "claude" or any available model).
3. Click **Get API Key** or navigate to your API keys page.
4. Create a key and copy it. This is your `INFERENCE_HUB_API_KEY`.
5. Note the **model name** — the exact string the API expects (e.g.
   `azure/anthropic/claude-opus-4-6`). Hyphens vs dots matter.

### Step 3: Clone and Configure

```bash
git clone https://github.com/dpickem/nemoclaw_escapades.git
cd nemoclaw_escapades
cp .env.example .env
```

Edit `.env` and fill in the three required values:

```
SLACK_BOT_TOKEN=xoxb-your-token-here
SLACK_APP_TOKEN=xapp-your-token-here
INFERENCE_HUB_API_KEY=your-nvidia-api-key-here
```

### Step 4: Install Dependencies

```bash
make install
```

This runs `pip install -e ".[dev]"`, installing the project and all
development dependencies (pytest, ruff, mypy, etc.).

### Step 5: Verify Credentials

```bash
make test-auth
```

This script tests each credential against its API:

- `SLACK_BOT_TOKEN` → calls `auth.test` on the Slack API
- `SLACK_APP_TOKEN` → calls `apps.connections.open`
- `INFERENCE_HUB_API_KEY` → calls `/v1/models` and
  `/v1/chat/completions` on the inference API

All four checks should show ✓. If inference fails, check the model name.

### Step 6: Run Locally

```bash
make run-local
```

You should see:

```
{"level": "INFO", "component": "main", "message": "Starting NemoClaw M1 agent loop"}
{"level": "INFO", "component": "slack_connector", "message": "Slack bot authenticated"}
{"level": "INFO", "component": "slack_bolt.AsyncApp", "message": "⚡️ Bolt app is running!"}
```

Open Slack, find your bot in DMs, and send a message. You should see:

1. A "Thinking..." indicator appears immediately.
2. After a few seconds, it's replaced with the model's response.

Press `Ctrl+C` to stop the bot.

### Step 7: Run All Tests

```bash
make test
```

All tests should pass. This validates the connector, orchestrator, inference
backend, transcript repair, and approval gate without needing real credentials.

---

## Deploying to an OpenShell Sandbox

The local setup (`make run-local`) is great for development, but for
always-on deployment we use OpenShell. This section walks through the full
sandbox deployment.

### Step 8: Start the OpenShell Gateway

```bash
openshell gateway start
```

This downloads and starts a **k3s cluster inside Docker Desktop**. The gateway
is a lightweight Kubernetes environment running entirely on your laptop. It
manages sandbox lifecycle, network policies, and the credential proxy.

First run downloads the gateway image (~200 MB) and initializes the cluster.
Expect 1-2 minutes. Subsequent starts reuse the existing cluster and take
seconds.

Verify the gateway is healthy:

```bash
openshell status
```

Expected output:

```
Server Status
  Gateway: openshell
  Server: https://127.0.0.1:8080
  Status: Connected
  Version: 0.0.21
```

If you see `connection refused`, Docker Desktop may not be running or the
gateway container stopped. Run `openshell gateway start` again.

#### Understanding the gateway

The gateway is the central control plane. It:

- Runs k3s (lightweight Kubernetes) inside a Docker container
- Manages sandbox pods (create, delete, monitor)
- Stores provider credentials (encrypted, never exposed to sandboxes)
- Runs the HTTPS proxy that mediates all sandbox network traffic
- Hosts the `inference.local` inference routing proxy

Everything below depends on the gateway running. If it dies, all sandboxes
lose connectivity.

### Step 9: Register the Inference Provider

OpenShell has a **provider** abstraction for managing credentials. You register
a provider once, and it can be attached to any number of sandboxes.

```bash
openshell provider create \
    --name inference-hub \
    --type openai \
    --credential "OPENAI_API_KEY=$(grep INFERENCE_HUB_API_KEY .env | cut -d= -f2-)" \
    --config "OPENAI_BASE_URL=https://inference-api.nvidia.com/v1"
```

Let's break this down:

- **`--name inference-hub`**: A logical name we'll reference later.
- **`--type openai`**: The provider type. We use `openai` because the NVIDIA
  Inference Hub exposes an OpenAI-compatible API (`/v1/chat/completions`).
  Other types: `nvidia` (defaults to `integrate.api.nvidia.com`), `anthropic`,
  `claude`, `generic`.
- **`--credential "OPENAI_API_KEY=..."`**: The API key. The name `OPENAI_API_KEY`
  is required by the `openai` provider type — it's the env var name OpenShell
  uses internally for routing. The actual value comes from your `.env` file.
- **`--config "OPENAI_BASE_URL=..."`**: The upstream endpoint. Without this,
  the `openai` type defaults to `api.openai.com`. We override it to point to
  NVIDIA's endpoint.

**Why not `--type nvidia`?** The `nvidia` type defaults to
`integrate.api.nvidia.com`, which is a different API from
`inference-api.nvidia.com`. Using `openai` gives us explicit control over the
base URL.

**Why not `--type generic`?** The `generic` type works for credential injection
via placeholders, but it cannot be used with `openshell inference set` (the
inference routing command). Only `openai`, `nvidia`, and `anthropic` types
support inference routing.

Verify the provider:

```bash
openshell provider get inference-hub
```

Expected:

```
Provider:
  Name: inference-hub
  Type: openai
  Credential keys: OPENAI_API_KEY
  Config keys: OPENAI_BASE_URL
```

### Step 10: Configure Inference Routing

Registering a provider only stores the credential. You must also tell the
gateway **how to route inference requests** from `inference.local` to the
real upstream:

```bash
openshell inference set \
    --provider inference-hub \
    --model "azure/anthropic/claude-opus-4-6"
```

This configures the `inference.local` proxy endpoint inside sandboxes:

- When the app sends `POST https://inference.local/v1/chat/completions`,
  the proxy looks up the `inference-hub` provider.
- The proxy adds `Authorization: Bearer <real-api-key>` to the request.
- The proxy overrides the model field in the request body with
  `azure/anthropic/claude-opus-4-6`.
- The proxy forwards to `https://inference-api.nvidia.com/v1/chat/completions`.

**The `--model` flag forces the model name.** This is important: the proxy
overrides whatever model the app requests. If this name doesn't exactly match
what the upstream API expects, you'll get 401 errors. The model name is
`claude-opus-4-6` (hyphenated), NOT `claude-opus-4.6` (dotted).

Verify routing:

```bash
openshell inference get
```

Expected:

```
Gateway inference:
  Route: inference.local
  Provider: inference-hub
  Model: azure/anthropic/claude-opus-4-6
  Version: 1
  Timeout: 60s (default)
```

If you omit `--no-verify`, the command will test the endpoint and report
whether it's reachable.

### Step 11: Register the Slack Provider

Slack credentials use the `generic` provider type because OpenShell doesn't
have a built-in Slack type. The `generic` type lets us define arbitrary env
var names:

```bash
openshell provider create \
    --name slack-credentials \
    --type generic \
    --credential "SLACK_BOT_TOKEN=$(grep SLACK_BOT_TOKEN .env | cut -d= -f2-)" \
    --credential "SLACK_APP_TOKEN=$(grep SLACK_APP_TOKEN .env | cut -d= -f2-)"
```

Inside the sandbox, these become env vars with placeholder values:

```
SLACK_BOT_TOKEN=openshell:resolve:env:SLACK_BOT_TOKEN
SLACK_APP_TOKEN=openshell:resolve:env:SLACK_APP_TOKEN
```

The HTTPS proxy resolves these placeholders to the real tokens when the Slack
SDK makes HTTP requests with `Authorization: Bearer <placeholder>`.

**Shortcut:** `make setup-secrets` runs all three commands (inference provider,
inference routing, Slack provider) in one step.

### Step 12: Understand the Sandbox Network Policy

Before deploying, it's worth understanding what the sandbox is and isn't
allowed to do. The policy lives in `policies/orchestrator.yaml`.

The network policy has three entries:

**`slack_api`** — For Slack HTTP API calls (`auth.test`, `chat.postMessage`,
`apps.connections.open`):

```yaml
endpoints:
  - host: slack.com
    port: 443
    protocol: rest
    enforcement: enforce
    tls: terminate
    rules:
      - allow: { method: GET, path: "/**" }
      - allow: { method: POST, path: "/**" }
```

`protocol: rest` + `tls: terminate` means the proxy intercepts HTTPS traffic,
terminates TLS, inspects HTTP headers (resolving credential placeholders), and
enforces method/path rules.

**`slack_websocket`** — For the Socket Mode WebSocket connection:

```yaml
endpoints:
  - host: "*.slack.com"
    port: 443
    access: full
```

`access: full` creates a CONNECT tunnel — opaque TCP passthrough with no header
inspection. This is necessary because Socket Mode is a long-lived WebSocket.
The proxy's HTTP idle timeout (~2 min) would kill it if we used `protocol: rest`.
The WebSocket doesn't need credential resolution anyway — authentication
happens in the initial `apps.connections.open` HTTP call, which returns a
session ticket embedded in the WebSocket URL.

**`inference`** — For model inference via the built-in proxy:

```yaml
endpoints:
  - host: inference.local
    port: 443
    protocol: rest
    enforcement: enforce
    tls: terminate
    rules:
      - allow: { method: GET, path: "/**" }
      - allow: { method: POST, path: "/**" }
```

Note this is `inference.local`, NOT `inference-api.nvidia.com`. The app sends
requests to `inference.local`; the inference routing proxy handles forwarding
to the real upstream. Direct access to external inference endpoints is blocked
by the proxy (Python's HTTP libraries use CONNECT tunneling, which the proxy
rejects for `protocol: rest` endpoints — see Lesson #8 below).

### Step 13: Build and Deploy the Sandbox

```bash
make setup-sandbox
```

This is the main deployment command. Under the hood it:

1. **Deletes** any existing `orchestrator` sandbox.
2. **Creates a symlink** `Dockerfile -> docker/Dockerfile.orchestrator` at the
   project root. This is needed because `openshell sandbox create --from .`
   uses the current directory as both the Dockerfile location and build
   context. Our Dockerfile lives in `docker/` but needs access to
   `pyproject.toml`, `src/`, etc. at the root. (See Lesson #3 below.)
3. **Builds the Docker image** inside the k3s cluster. This is NOT a local
   Docker build — it happens inside the gateway's containerd. The image
   includes Python, our app code, `iproute2`, and the `sandbox` user.
4. **Pushes the image** into the cluster's image store (~50 MB).
5. **Creates the sandbox** with:
   - The built image
   - The network policy from `policies/orchestrator.yaml`
   - Both providers attached (`inference-hub` and `slack-credentials`)
   - The command `python -m nemoclaw_escapades.main`
6. **Streams the app's stdout/stderr** to your terminal.
7. **Cleans up the symlink**.

The first deploy is slow (~30-60s) because the cluster pulls base images.
Subsequent deploys reuse cached layers and take ~10-15s.

Expected output:

```
Creating orchestrator sandbox...
Building image openshell/sandbox-from:... from .../Dockerfile
  ...
  Successfully built ...
  Pushing image ... into gateway "openshell"
  Image ... is available in the gateway.

Created sandbox: orchestrator
{"level": "INFO", "component": "main", "message": "Starting NemoClaw M1 agent loop"}
{"level": "INFO", "component": "slack_connector", "message": "Slack bot authenticated"}
{"level": "INFO", "component": "slack_bolt.AsyncApp", "message": "⚡️ Bolt app is running!"}
```

Send a message to the bot in Slack. It should respond.

### Step 14: Verify the Deployment

```bash
make status
```

Check that:

- **Gateway**: Connected, version 0.0.21+
- **Providers**: `inference-hub` (openai, 1 credential, 1 config) and
  `slack-credentials` (generic, 2 credentials)
- **Sandbox**: `orchestrator`, phase `Ready`
- **Policy**: Shows `slack_api`, `slack_websocket`, and `inference` entries

### Step 15: Debugging a Failed Deployment

If the sandbox fails, here's the debugging ladder:

**Check pod status:**

```bash
openshell doctor exec -- kubectl get pods -n openshell
```

- `CrashLoopBackOff` → the container keeps crashing. Get logs:
  ```bash
  openshell doctor exec -- kubectl logs <pod-name> -n openshell --all-containers=true
  ```
- `ImagePullBackOff` → the image isn't in the cluster. Rebuild with
  `make setup-sandbox`.
- `Provisioning` (stuck) → usually waiting for image pull. Check with
  `openshell sandbox list`.

**Common crash-loop causes:**

| Error | Cause | Fix |
|---|---|---|
| `sandbox user 'sandbox' not found` | Image missing `sandbox` user | Add `groupadd`/`useradd` to Dockerfile |
| `Network namespace creation failed [...] iproute2` | Image missing `iproute2` | Add `apt-get install iproute2` to Dockerfile |
| `ModuleNotFoundError: No module named 'aiohttp'` | Missing Python dependency | Add `aiohttp` to `pyproject.toml` |
| `ProxyError: 403 Forbidden` | Network policy blocks the endpoint | Check `policies/orchestrator.yaml` |
| `Authentication failed (401)` | Wrong API key or model name | Verify with `make test-auth` |

**Check the applied policy:**

```bash
openshell sandbox get orchestrator
```

This shows the policy as the sandbox actually sees it — not just what's in the
YAML file on disk.

**Hot-reload the policy** (for dynamic fields like `network_policies`):

```bash
openshell policy set orchestrator --policy policies/orchestrator.yaml
```

This takes effect immediately without rebuilding the image or recreating the
sandbox.

### Stopping and Cleaning Up

| Command | What it does |
|---|---|
| `Ctrl+C` in the terminal | Stops the current `make setup-sandbox` session |
| `make stop-all` | Deletes ALL sandboxes in the gateway |
| `make clean` | Deletes the orchestrator sandbox, providers, and local Docker image |
| `make clean-all` | Everything in `clean` plus stops the gateway |

### How Credentials Flow in the Sandbox

Understanding this flow is critical for debugging:

![Credential flow in the OpenShell sandbox](./m1_credential_flow.svg)

The two credential paths work differently:

- **Slack credentials** use placeholder resolution. The app reads
  `SLACK_BOT_TOKEN=openshell:resolve:env:SLACK_BOT_TOKEN` from its
  environment. When the Slack SDK sends an HTTP request with
  `Authorization: Bearer openshell:resolve:...`, the HTTPS proxy intercepts
  it, resolves the placeholder to the real `xoxb-...` token, and forwards
  the request to Slack.

- **Inference credentials** use the built-in `inference.local` proxy. The
  app sends requests to `https://inference.local/v1/chat/completions` with
  no `Authorization` header at all. The proxy looks up the provider config,
  injects the real API key, overrides the model name, and forwards to the
  NVIDIA endpoint.

---

## Why This Milestone Comes First

If the series introduction explains why this project exists, M1 explains why
the first implementation milestone is intentionally small. We are starting with
the smallest useful loop that still teaches something real.

So M1 is deliberately narrow. We are not starting with a coding agent, a review
agent, a memory system, or a self-improvement loop. We are starting with the
minimum viable runtime that lets us see where the core pieces of an agent system
actually live:

- the connector that receives and returns messages,
- the orchestrator that owns the agent loop,
- the inference backend that talks to a model,
- and the deployment/runtime choices that determine where each part runs.

One of the easiest ways to get confused by modern agent projects is to blur the
agent loop, the surrounding infrastructure, and the tools it may eventually
call. M1 exists to make those boundaries visible.

## What We Are Intending to Build

M1 establishes the first production-oriented runtime loop for
`nemoclaw_escapades`. A Slack message enters the connector, the orchestrator
builds context and routes inference, and the response returns to Slack with
basic observability around failures and retries.

Even though the project references NemoClaw in its broader research context,
this milestone is **not** about deploying vanilla NemoClaw. The objective is to
build and run our own orchestrator-based stack with clean, reusable seams.

In concrete terms, "builds context" in M1 means only the essentials: normalize
the incoming Slack event, shape the prompt, call the model through a reusable
backend interface, and return a response with enough logging to debug failures.
That is intentionally modest. The point of M1 is not sophistication. The point
is legibility.

This is the foundation milestone. If this loop is not reliable, inspectable,
and easy to explain, then later work on sandboxed coding agents, review loops,
memory, and self-improvement will be built on sand.

## What M1 Teaches

This milestone is doing more than wiring Slack to a model endpoint. It is meant
to clarify the shape of a minimal agent system.

| Boundary | Responsibility in M1 | Why isolate it now |
|---|---|---|
| Connector | Translate Slack events into internal request/response objects | Prevent channel-specific logic from leaking into the core loop |
| Orchestrator | Own context assembly, routing, retries, and response shaping | Make the "main brain" explicit from day one |
| Inference backend | Provide one interface for model calls | Keep provider choice swappable instead of hard-coded |
| Observability | Surface failures, retries, and runtime state | Make the system debuggable before it becomes more complex |

If M1 works, we will have a clean answer to an important question for the whole
series: what is the minimum useful agent architecture before delegation even
enters the picture?

## Deployment Model

M1 also establishes the first deployment split for the system: the orchestrator
and Slack connector run locally, while model inference is hosted remotely
through NVIDIA Inference Hub.

That split is a concrete engineering choice, not just a convenience:

- Running the control loop locally keeps the architecture easy to inspect,
  iterate on, and debug.
- Using hosted inference avoids premature model-serving work while still
  forcing a real backend abstraction.
- Keeping the boundary explicit gives us a cleaner path toward later always-on
  deployment on managed infrastructure rather than trapping the project in a
  laptop-only demo.

| Runtime Element | Where It Runs | Notes |
|---|---|---|
| Orchestrator | Local machine | Main agent loop, routing, and response shaping |
| Connector (Slack) | Local machine | Ingress/egress channel adapter |
| Model inference | NVIDIA Inference Hub | Orchestrator calls hosted model endpoints remotely |
| Logs/observability | Local machine | First-pass operational visibility for M1 |

NVIDIA Inference Hub is a good fit for this stage because it lets the project
focus on orchestration instead of model serving. The architectural goal is not
loyalty to one inference provider. The goal is to keep inference behind a
generic backend contract so the orchestrator remains portable.

## Architecture Flow

```mermaid
sequenceDiagram
    participant User as Slack User
    participant Conn as Slack Connector
    participant Orch as Orchestrator
    participant Hub as NVIDIA Inference Hub

    User->>Conn: Send message
    Conn->>Orch: Normalized event payload
    Orch->>Hub: Prompt + context
    Hub-->>Orch: Model completion
    Orch-->>Conn: Final response payload
    Conn-->>User: Reply in channel
```

The loop is simple on purpose:

1. A Slack user sends a message.
2. The connector converts that platform event into a normalized payload.
3. The orchestrator builds the prompt context and decides how to call the model.
4. The inference backend sends that request to NVIDIA Inference Hub.
5. The orchestrator shapes the result into a final response and returns it
   through the connector.

That is the first "real" agent loop for this project. It is only one turn, but
it gives us a working baseline for control flow, abstraction boundaries, and
deployment.

## Connector Implementation Walkthrough

![Connector event pipeline](./m1_connector_pipeline.svg)

The Slack connector (`src/nemoclaw_escapades/connectors/slack.py`) is the
boundary between Slack's event model and the orchestrator's platform-neutral
types. It handles three responsibilities: normalizing inbound events, managing
the response lifecycle, and rendering platform-neutral blocks into Slack's
Block Kit format. No orchestration logic lives here. No inference logic either.
Just the translation layer.

### The connector contract

Every connector extends `ConnectorBase` (`connectors/base.py`):

```python
MessageHandler = Callable[[NormalizedRequest], Awaitable[RichResponse]]

class ConnectorBase(ABC):
    def __init__(self, handler: MessageHandler) -> None:
        self._handler = handler

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...
```

The `handler` callback is the orchestrator's `handle` method — the connector
calls `await self._handler(request)` and gets back a `RichResponse`. It never
knows what happens between those two points. This is the central isolation
guarantee: adding a new platform (Discord, Telegram, a web UI) means writing
one new `ConnectorBase` subclass. Nothing else changes.

### Event listening and filtering

The Slack connector registers three Bolt listeners:

```python
@self._app.event("message")
async def on_message(event, client):
    await self._on_event(event, client)

@self._app.event("app_mention")
async def on_mention(event, client):
    await self._on_event(event, client)

@self._app.action("")
async def on_action(ack, body, client):
    await ack()
    request = self._normalize_action(body)
    await self._handle_with_thinking(client, request)
```

All three funnel into the same pipeline: filter → normalise → thinking →
orchestrate → reply. The thin closures only differ in how they extract a
`NormalizedRequest` from the Slack payload.

Before any event is processed, it passes through `_should_ignore`:

```python
def _should_ignore(self, event):
    if event.get("subtype") is not None:
        return True
    if event.get("bot_id"):
        return True
    if self._bot_user_id and event.get("user") == self._bot_user_id:
        return True
    return False
```

The `subtype` check is the most important line in this method. When the bot
posts a "Thinking…" placeholder and later replaces it with `chat_update`,
Slack emits a `message_changed` event. If the connector processes that event,
it triggers another inference call, which posts another update, which emits
another event — an infinite spam loop. The fix: only process events where
`subtype is None`. Real user messages have no subtype. Everything else —
`bot_message`, `message_changed`, `message_deleted` — is dropped. The
`bot_id` and user ID checks are defense-in-depth for cases where a subtype
might be missing but the message still originates from the bot.

### Event normalization

Normalization strips away all Slack-specific structure and produces a
platform-neutral `NormalizedRequest`:

```python
@staticmethod
def _normalize(event):
    return NormalizedRequest(
        text=event.get("text", ""),
        user_id=event.get("user", ""),
        channel_id=event.get("channel", ""),
        thread_ts=event.get("thread_ts") or event.get("ts"),
        timestamp=time.time(),
        source="slack",
        raw_event=event,
    )
```

The `thread_ts` logic deserves attention. If the message is in a thread,
`thread_ts` is the parent message's timestamp — Slack's way of identifying
a thread. If it's a top-level message, there is no `thread_ts`, so we fall
back to `ts` (the message's own timestamp). This matters because the
orchestrator uses `thread_ts` as the key for conversation history: all
messages in a thread share the same key, giving the model full thread context.

`NormalizedRequest` itself is a plain dataclass with no Slack imports:

```python
@dataclass
class NormalizedRequest:
    text: str
    user_id: str
    channel_id: str
    timestamp: float
    source: str
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    thread_ts: str | None = None
    action: ActionPayload | None = None
    raw_event: dict[str, object] = field(default_factory=dict)
```

The `request_id` is auto-generated and carried through every log line in the
pipeline — orchestrator, backend, transcript repair — for end-to-end tracing.

Action payloads (button clicks, dropdown selections) follow a parallel path
through `_normalize_action`, which extracts the first action from Slack's
`actions` array and wraps it in an `ActionPayload`:

```python
@staticmethod
def _normalize_action(body):
    actions = body.get("actions", [{}])
    action_data = actions[0] if actions else {}
    channel = body.get("channel", {})
    user = body.get("user", {})
    message = body.get("message", {})

    return NormalizedRequest(
        text=action_data.get("value", ""),
        user_id=user.get("id", ""),
        channel_id=channel.get("id", "") if isinstance(channel, dict) else str(channel),
        thread_ts=message.get("thread_ts") or message.get("ts"),
        timestamp=time.time(),
        source="slack",
        action=ActionPayload(
            action_id=action_data.get("action_id", ""),
            value=action_data.get("value", ""),
            metadata=action_data,
        ),
        raw_event=body,
    )
```

### The thinking indicator pattern

Most chat bots post their response only after inference completes, leaving the
user staring at nothing for several seconds. The connector inverts this with a
thinking indicator — an immediate visible response that gets replaced in-place:

```python
async def _handle_with_thinking(self, client, request):
    # 1. Post placeholder immediately
    thinking_ts = await self._post_thinking(client, channel, thread_ts)

    # 2. Call the orchestrator (inference happens here — the slow part)
    response = await self._handler(request)

    # 3. Render and replace the placeholder
    blocks = self.render(response)
    if thinking_ts:
        await self._update_message(client, channel, thinking_ts, text, blocks)
    else:
        await self._post_message(client, channel, thread_ts, text, blocks)
```

The flow is:

1. Post ":hourglass_flowing_sand: Thinking…" via `chat_postMessage`.  Capture
   its `ts` (Slack's message ID).
2. Call the orchestrator, which builds context, calls the model, repairs the
   transcript, and returns a `RichResponse`.
3. Replace the thinking message in-place via `chat_update` using the saved `ts`.

If the thinking message fails to post (permissions, rate limits), the connector
falls back to posting a new message after inference returns. If the `chat_update`
fails, it falls back again to a new message. The user always gets a response.

### Error propagation and rate limiting

![Error propagation chain](./m1_error_propagation.svg)

Errors flow from bottom to top through the full stack:

1. The **inference backend** raises `InferenceError` with a categorized
   `ErrorCategory` (auth, rate limit, timeout, model error, unknown).
2. The **orchestrator** catches it and returns a `RichResponse` containing a
   user-friendly error message — not a traceback.
3. The **connector** receives that `RichResponse` and checks whether it looks
   like an error:

```python
is_error = response.blocks and all(
    isinstance(b, TextBlock)
    and any(
        phrase in (b.text or "")
        for phrase in ("try again", "admin know", "rate-limited")
    )
    for b in response.blocks
)
```

4. If it is an error, the connector checks its **per-channel rate limiter**:
   at most 3 error messages per 60 seconds.  If the limit is exceeded, the
   thinking indicator is silently deleted instead of being replaced with the
   error text.

```python
def _is_error_rate_limited(self, channel):
    now = time.monotonic()
    timestamps = self._error_timestamps[channel]
    timestamps[:] = [t for t in timestamps if now - t < self._ERROR_WINDOW_S]
    timestamps.append(now)
    return len(timestamps) > self._ERROR_MAX_PER_WINDOW
```

This prevents the worst-case failure mode: a persistently broken backend
generating one error message for every inbound user message in a busy channel.

### Rendering: RichResponse → Block Kit

The orchestrator returns `RichResponse` objects containing platform-neutral
blocks. The connector's `render()` method translates each block into Slack
Block Kit JSON.

The mapping is:

| Platform-neutral block | Slack Block Kit output |
|---|---|
| `TextBlock` (markdown) | `section` with `mrkdwn` text |
| `TextBlock` (plain) | `section` with `plain_text` |
| `ActionBlock` | `actions` with `button` elements |
| `ConfirmBlock` | `section` with a `confirm` dialog accessory |
| `FormBlock` | `header` + field `section`s with `static_select` + submit button |

The most common path is `TextBlock` → `section`:

```python
if isinstance(block, TextBlock):
    if block.style == "markdown":
        return _section(_to_slack_markdown(block.text))
    return _plain_section(block.text)
```

`_to_slack_markdown` handles the impedance mismatch between standard Markdown
(which LLMs produce) and Slack's mrkdwn dialect:

- `# Heading` → `*Heading*` (bold)
- `**bold**` → `*bold*` (Slack uses single asterisks)
- `[label](url)` → `<url|label>` (Slack link syntax)

`FormBlock` is the most complex case. Slack messages cannot contain `input`
blocks (those are modal-only), so the connector adapts: `select` fields become
`static_select` dropdowns in a section accessory, and text fields become
labelled sections prompting the user to reply in-thread. A submit button is
appended when `submit_action_id` is set.

The key architectural point: none of this rendering logic exists in the
orchestrator. If a Discord connector were added, it would implement its own
`render()` targeting Discord embeds. The orchestrator's output is always the
same `RichResponse` regardless of destination.

---

## Orchestrator Implementation Walkthrough

![Orchestrator pipeline](./m1_orchestrator_pipeline.svg)

The orchestrator (`src/nemoclaw_escapades/orchestrator/orchestrator.py`) is the
centre of the M1 agent loop. It owns the full request lifecycle: receive a
platform-neutral request, build prompt context, call the inference backend,
apply defensive output handling, check the approval gate, and return a
platform-neutral response.

The orchestrator imports no platform SDK — no `slack_sdk`, no `slack_bolt`. It
communicates with connectors through `NormalizedRequest` / `RichResponse` and
with backends through `InferenceRequest` / `InferenceResponse`. This isolation
is not accidental. It is the reason we can test the orchestrator without Slack
credentials, swap inference providers without touching the control loop, and
add new connectors without modifying the core.

### Wiring: how the pieces connect

The entry point (`main.py`) shows how the three components are assembled:

```python
backend = InferenceHubBackend(config.inference)
orchestrator = Orchestrator(backend, config.orchestrator)
connector = SlackConnector(
    handler=orchestrator.handle,
    bot_token=config.slack.bot_token,
    app_token=config.slack.app_token,
)
```

The orchestrator's `handle` method matches the `MessageHandler` callback
signature. It is passed to the connector as a plain function reference — the
connector calls it without knowing (or needing to know) anything about the
orchestrator's internal structure.

### The handle() method: the full agent loop

`handle()` is the orchestrator's only public method. Every inbound request
passes through it:

```python
async def handle(self, request: NormalizedRequest) -> RichResponse:
    thread_key = request.thread_ts or request.request_id

    try:
        # 1. Build prompt context
        messages = self._build_prompt(thread_key, request.text)

        # 2. Call inference with transcript repair
        content = await self._inference_with_repair(messages, request.request_id)

        # 3. Check approval gate
        approval = await self._approval.check(
            "respond", {"content": content, "request_id": request.request_id}
        )
        if not approval.approved:
            content = "I generated a response but it was not approved. ..."

        # 4. Record in thread history
        self._thread_history[thread_key].append(
            {"role": "assistant", "content": content}
        )

        # 5. Shape and return
        return self._shape_response(request, content)

    except InferenceError as exc:
        return self._error_response(request, exc.category)
    except Exception:
        return self._error_response(request, ErrorCategory.UNKNOWN)
```

Five steps, every one visible and testable in isolation. The outer
`try`/`except` guarantees the method never raises — it always returns a
`RichResponse`, even on failure. This is important because the connector is
waiting for a response to replace the thinking indicator. An unhandled
exception would leave a dangling "Thinking…" message in the channel forever.

### Context assembly: _build_prompt

The prompt sent to the model is always: **system prompt + full thread history +
latest user message**. `_build_prompt` constructs this list:

```python
def _build_prompt(self, thread_key, user_text):
    # Append the new user message to history
    self._thread_history[thread_key].append(
        {"role": "user", "content": user_text}
    )

    # Enforce max history length
    history = self._thread_history[thread_key]
    if len(history) > self._config.max_thread_history:
        self._thread_history[thread_key] = history[-max_len:]
        history = self._thread_history[thread_key]

    # System prompt always comes first
    return [{"role": "system", "content": self._system_prompt}] + list(history)
```

Thread history is keyed by `thread_ts` — the same key the connector derives
during normalization. All messages in a Slack thread share the same key, so
the model sees the full conversation context.

History is capped at a configurable maximum (default 50 messages). When
exceeded, the oldest messages are dropped from the front. This prevents
unbounded memory growth in long-running conversations. The cap is a simple
sliding window — more sophisticated approaches (summarization, semantic
compression) are deferred to M4 (memory orchestration).

The system prompt is loaded once at startup from a file (defaulting to
`prompts/system_prompt.md`), with a built-in fallback:

```python
def load_system_prompt(path):
    p = Path(path)
    if p.is_file():
        return p.read_text().strip()
    return (
        "You are NemoClaw, a helpful AI assistant. "
        "Be concise and direct in your responses. "
        "You do not yet have tools or persistent memory."
    )
```

History is in-memory only. It survives across messages within a process
lifetime but is lost on restart. Persistent conversation storage is a M5
concern.

### Inference dispatch and transcript repair

After building the prompt, the orchestrator calls the inference backend. But
it doesn't just fire one request and return the result. It wraps the call in a
transcript-repair loop that handles empty replies, truncated output, and
content-filter blocks:

```python
async def _inference_with_repair(self, messages, request_id):
    accumulated_content = ""

    for attempt in range(1 + MAX_CONTINUATION_RETRIES):
        if attempt > 0:
            # Append partial output + continuation prompt for retry
            messages = messages + [
                {"role": "assistant", "content": accumulated_content},
                {"role": "user", "content": CONTINUATION_PROMPT},
            ]

        inference_request = InferenceRequest(
            messages=messages,
            model=self._config.model,
            temperature=self._config.temperature,
            max_tokens=self._config.max_tokens,
        )
        result = await self._backend.complete(inference_request)

        repair = repair_response(result, request_id)

        if repair.was_repaired and not repair.needs_continuation:
            return repair.content  # fallback message, done

        accumulated_content += repair.content

        if not repair.needs_continuation:
            return accumulated_content  # clean response, done

    return accumulated_content  # partial, but best we have
```

`repair_response` (`orchestrator/transcript_repair.py`) inspects the raw model
output and returns a `RepairResult`:

| Condition | Action | Continuation? |
|---|---|---|
| Empty or whitespace-only content | Replace with fallback: "I wasn't able to generate a response. Could you rephrase?" | No |
| `finish_reason="length"` (truncated) | Keep the partial content, flag for continuation | Yes |
| `finish_reason="content_filter"` | Replace with: "My response was filtered. Could you rephrase your request?" | No |
| Normal response | Pass through unchanged | No |

When continuation is needed, the orchestrator appends the partial output as an
assistant message followed by a continuation prompt:

```
"Resume directly, no apology, no recap. Pick up mid-thought.
Break remaining work into smaller pieces."
```

This mirrors Claude Code's continuation strategy — no "I apologize for the
interruption" padding, just a clean resume. Up to 2 continuation retries are
attempted. Content from successive attempts is concatenated. If all retries
are exhausted, the partial content is returned as-is.

### The inference backend contract

The orchestrator calls `self._backend.complete(request)` — but what happens
inside that call is significant. The `InferenceHubBackend`
(`backends/inference_hub.py`) wraps `httpx.AsyncClient` with tenacity-based
retry logic:

```python
async for attempt in AsyncRetrying(
    retry=retry_if_exception_type(_RetryableError),
    wait=self._wait_for_retry,
    stop=stop_after_attempt(self._config.max_retries),
    before_sleep=self._log_retry,
    reraise=True,
):
    with attempt:
        return await self._send_request(payload, request.model)
```

A single HTTP attempt (`_send_request`) can result in:

| HTTP Status | Classification | Retry? |
|---|---|---|
| 200 | Success — parse and return | — |
| 401, 403 | `AUTH_ERROR` — bad key or expired token | Never |
| 429 | `RATE_LIMIT` — honour `Retry-After` header | Yes |
| 5xx | `MODEL_ERROR` — upstream failure | Yes |
| Timeout | `TIMEOUT` — no response in time | Yes |

Retryable failures raise an internal `_RetryableError` that tenacity catches.
Non-retryable failures (auth) raise `InferenceError` immediately, which
tenacity does not catch — it propagates straight to the orchestrator.

The wait strategy is adaptive: if the response includes a `Retry-After`
header, that value is used verbatim. Otherwise, exponential backoff with jitter
kicks in (initial 2 s, max 30 s, jitter ±2 s). Every retry is logged with the
attempt number and error category.

The backend also handles the local-vs-sandbox credential split. In the sandbox,
the `inference.local` proxy injects the real API key — the app must not send
one. Locally, the app sends the key from `.env` directly:

```python
headers = {"Content-Type": "application/json"}
if config.api_key:
    headers["Authorization"] = f"Bearer {config.api_key}"
```

### The approval gate

After inference succeeds, the response passes through an approval gate:

```python
approval = await self._approval.check(
    "respond", {"content": content, "request_id": request.request_id}
)
```

In M1 this is `AutoApproval` — a stub that approves everything. There are no
tools in M1, so there are no side effects to gate. But the interface is
scaffolded now so M2 can plug in a tiered classifier (fast-path pattern
matching for safe reads, LLM classifier for ambiguous operations, Slack
escalation for dangerous writes) without restructuring the loop.

### Response construction

The final step wraps the plain-text content in a platform-neutral
`RichResponse`:

```python
@staticmethod
def _shape_response(request, content):
    return RichResponse(
        channel_id=request.channel_id,
        thread_ts=request.thread_ts,
        blocks=[TextBlock(text=content)],
    )
```

In M1 this is always a single `TextBlock`. The block-list structure exists
because M2+ responses will include `ActionBlock` (approve/reject buttons),
`ConfirmBlock` (dangerous-action confirmations), and `FormBlock` (structured
input). The connector renders whatever blocks it receives — it doesn't need
to know whether the response is a simple text reply or a multi-block
interactive form.

Error responses follow the same pattern but with category-specific messages:

```python
@staticmethod
def _error_response(request, category):
    messages = {
        ErrorCategory.AUTH_ERROR: "I'm having a configuration issue ...",
        ErrorCategory.RATE_LIMIT: "I'm being rate-limited right now ...",
        ErrorCategory.TIMEOUT: "The model didn't respond in time ...",
        ErrorCategory.MODEL_ERROR: "Something went wrong with the model ...",
        ErrorCategory.UNKNOWN: "Something unexpected happened ...",
    }
    return RichResponse(
        channel_id=request.channel_id,
        thread_ts=request.thread_ts,
        blocks=[TextBlock(text=messages.get(category, messages[ErrorCategory.UNKNOWN]))],
    )
```

Each `ErrorCategory` maps to a non-technical message. The user never sees a
Python traceback. The full exception is logged server-side with structured JSON
(including `error_category`, `request_id`, and `latency_ms`) for debugging.

### Observability

Every step in the pipeline emits structured JSON logs. A single request
generates a trace like:

```json
{"component": "slack_connector", "message": "Request received", "request_id": "a3f8b2c1d4e5", "user_id": "U...", "channel_id": "C..."}
{"component": "orchestrator", "message": "Prompt built", "request_id": "a3f8b2c1d4e5", "history_length": 4}
{"component": "inference_hub", "message": "Inference call starting", "model": "azure/anthropic/claude-opus-4-6"}
{"component": "inference_hub", "message": "Inference call completed", "latency_ms": 2847.3, "prompt_tokens": 312, "completion_tokens": 89}
{"component": "orchestrator", "message": "Request completed", "request_id": "a3f8b2c1d4e5", "latency_ms": 2891.1}
{"component": "slack_connector", "message": "Response sent (updated thinking message)"}
```

The `request_id` ties every log line together. Token counts and latencies
are recorded for cost and performance tracking. Error categories are logged
so failure modes can be aggregated. All of this runs through a single
`JSONFormatter` that writes to stdout (and optionally a file), making it easy
to pipe into any log aggregation system.

---

## Learning Objectives

M1 should teach the mechanics of channel abstraction, inference abstraction, and
orchestrator control flow under real operational constraints.

| Objective | Evidence We Expect by End of M1 |
|---|---|
| Channel abstraction | Slack logic remains in connector, not orchestration core |
| Inference abstraction | Backend routes through inference hub without provider lock-in |
| Runtime reliability | One-turn request/response works repeatedly with retries/logging |
| Operability | Failure modes are visible and diagnosable from logs |

## Deliverables

The deliverables below are intentionally practical. Each one should leave
behind a reusable piece of the stack rather than a one-off demo.

| Deliverable | Description | Done When |
|---|---|---|
| Repo + docs baseline | Core project structure and architecture notes | New contributor can understand system boundaries quickly |
| Slack connector | Reusable connector interface with Slack implementation | Messages can be received and replied to reliably |
| Inference backend integration | Reusable backend interface routed through inference hub | Orchestrator can call model endpoints through one contract |
| Local runtime deployment | Orchestrator and connector run locally against hosted inference | End-to-end flow runs from local process to NVIDIA Inference Hub |
| Minimal orchestrator loop | End-to-end one-turn processing pipeline | Slack -> model -> Slack works with realistic prompts |
| Architecture diagrams | Visual map of runtime responsibilities | Diagram matches actual code path and is reviewable |

## Acceptance Criteria

The acceptance criteria are deliberately narrow. M1 is successful if it proves
the baseline loop and preserves clean seams for the milestones that follow.

| Check | Validation Method |
|---|---|
| End-to-end loop works | Send a Slack message and receive model response |
| Pluggable boundaries exist | Confirm no Slack/provider-specific logic in core orchestrator |
| Deployment behavior is explicit | Verify orchestrator runs locally while model calls are remote via NVIDIA Inference Hub |
| Error handling exists | Simulate auth/network/model failures and inspect logs |
| M2 readiness | Documentation is clear enough for coding-agent milestone handoff |

## Why This Sets Up the Rest of the Series

M1 is the point where the architecture stops being an idea and becomes a
running system.

Once this baseline exists, the next milestones can add complexity for good
reasons instead of for their own sake:

- M2 adds sandboxed execution through a coding agent.
- M3 adds collaboration through a review agent.
- M4 adds memory orchestration (working, user, and knowledge lanes).
- M5 adds professional knowledge capture from Slack and Teams into the knowledge tier.
- M6 adds a self-improvement loop.

But every one of those later steps assumes the same foundation: a clear
connector, a clear orchestrator, a clear inference boundary, and a deployment
model we can reason about. That is what M1 is really buying us.

## OpenShell Sandbox Deployment — Lessons Learned

This section documents every issue we hit deploying the M1 orchestrator inside
an OpenShell sandbox, in the order we encountered them. These are hard-won
debugging nuggets that will save you hours.

### 1. The `openshell` CLI changes between versions — check subcommands

OpenShell 0.0.6 used `provider add`, `credential set`, `sandbox remove`, and
`sandbox stop`. By 0.0.21, these were `provider create`, `sandbox delete`, and
there is no `credential` subcommand at all. Always run `openshell <command>
--help` before scripting against the CLI. The Makefile we shipped was written
against a hallucinated API and every command was wrong.

**Fix:** Run `--help` on every subcommand. Don't guess.

### 2. Dockerfiles must include `README.md` for hatchling builds

Our `pyproject.toml` declares `readme = "README.md"`. Hatchling (the build
backend) validates this at metadata generation time. If the Dockerfile only
copies `pyproject.toml` and `src/` into the builder stage, the build fails
with `OSError: Readme file does not exist: README.md`.

**Fix:** `COPY pyproject.toml README.md ./` in the builder stage.

### 3. OpenShell's `--from` flag and the build context trap

When `openshell sandbox create --from <path>` receives a Dockerfile path, it
uses the Dockerfile's **parent directory** as the build context. If your
Dockerfile lives in `docker/Dockerfile.orchestrator`, the context is `docker/`
— which doesn't contain `pyproject.toml`, `src/`, or `README.md`.

When given a directory path, OpenShell uses that directory as context and looks
for a `Dockerfile` inside it.

**Fix:** We create a temporary symlink `Dockerfile -> docker/Dockerfile.orchestrator`
at the project root, pass `--from .`, and remove the symlink after. The symlink
is in `.gitignore`. This is documented in the Makefile with a full explanation.

### 4. The `python:3.11-slim` image doesn't meet OpenShell sandbox requirements

OpenShell 0.0.21+ requires every sandbox image to include:

- A `sandbox` user and group (the supervisor drops privileges to this user)
- `iproute2` (the supervisor uses it to create network namespaces for proxy
  isolation)

The stock `python:3.11-slim` has neither. The supervisor crashes with clear
error messages:

- `sandbox user 'sandbox' not found in image`
- `Network namespace creation failed [...] Ensure CAP_NET_ADMIN and
  CAP_SYS_ADMIN are available and iproute2 is installed`

**Fix:** Add both to the Dockerfile:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r sandbox && useradd -r -g sandbox -d /app -s /bin/bash sandbox
```

### 5. Do NOT set `USER` or `ENTRYPOINT` in the Dockerfile

OpenShell replaces the image's entrypoint with its own supervisor binary
(`/opt/openshell/bin/openshell-sandbox`). The supervisor must start as root to
apply Landlock policies and set up network namespaces before dropping
privileges to the `sandbox` user (specified in the policy's `process` section).

If the Dockerfile sets `USER sandbox`, the supervisor can't apply policies and
crash-loops. If it sets `ENTRYPOINT`, it's ignored anyway.

The actual application command is passed via `-- <cmd>` on
`openshell sandbox create`.

### 6. Credential injection uses opaque placeholders, not real values

OpenShell never exposes real credentials inside the sandbox. Environment
variables contain placeholder tokens like
`openshell:resolve:env:SLACK_BOT_TOKEN`. The proxy resolves these placeholders
in HTTP request headers when traffic passes through it.

We confirmed this with a test script (`scripts/test_credential_injection.sh`)
that creates a provider, spins up an ephemeral sandbox, and dumps `env | sort`.
All three credentials showed placeholder values, not the real tokens from
`.env`.

### 7. The proxy routes ALL outbound traffic — HTTPS_PROXY is set automatically

Inside the sandbox, OpenShell sets `HTTPS_PROXY=http://10.200.0.1:3128` and
installs its own CA certificate (`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`,
`CURL_CA_BUNDLE`). All outbound HTTPS traffic goes through this proxy, which
enforces network policies, resolves credential placeholders, and terminates
TLS.

### 8. Python's CONNECT tunneling vs OpenShell's REST proxy mode

This was the hardest bug to diagnose. The OpenShell proxy supports two modes
for endpoints:

- **`protocol: rest` + `tls: terminate`**: The proxy expects the client to send
  a regular HTTP request. The proxy terminates TLS itself and can inspect/modify
  headers (resolving credential placeholders).
- **`access: full`**: The proxy creates a CONNECT tunnel (opaque TCP
  passthrough). No header inspection.

**The problem:** Python's HTTP libraries (`httpx`, `urllib.request`, `curl`)
send `CONNECT host:443` requests through an HTTPS proxy. This is standard HTTP
proxy behavior. But OpenShell's proxy rejects CONNECT requests for endpoints
configured with `protocol: rest`. It returns **403 Forbidden**.

Node.js-based tools (Claude Code, OpenClaw) send regular HTTP requests through
the proxy instead of CONNECT, which is why the
[reference NemoClaw policy](https://github.com/NVIDIA/NemoClaw/blob/main/nemoclaw-blueprint/policies/openclaw-sandbox.yaml)
lists `inference-api.nvidia.com` with `protocol: rest` and it works — for Node.

**The workaround for Python:** Use `inference.local` instead of hitting the
inference API directly. `inference.local` is OpenShell's built-in inference
proxy endpoint. The app sends requests to `https://inference.local/v1/...` and
the proxy handles authentication, routing, and forwarding to the real upstream.
No CONNECT tunnel is needed because `inference.local` resolves locally inside
the sandbox's network namespace.

For Slack, the split is:
- `slack.com` with `protocol: rest` + `tls: terminate` for HTTP API calls
  (auth.test, chat.postMessage, apps.connections.open)
- `*.slack.com` with `access: full` for the long-lived Socket Mode WebSocket
  (the `access: full` CONNECT tunnel avoids the proxy's HTTP idle timeout
  killing the connection — same pattern as Discord in the reference policy)

### 9. Inference routing must be configured separately

Having an inference provider registered (`openshell provider create`) is not
enough. You must also configure **inference routing** so the gateway knows how
to forward requests from `inference.local` to the real upstream:

```bash
openshell inference set --provider inference-hub --model "azure/anthropic/claude-opus-4-6"
```

Without this, `inference.local` returns **503 Unknown** (no route configured).

### 10. The `nvidia` provider type defaults to `integrate.api.nvidia.com`

If you create a provider with `--type nvidia`, the inference routing defaults to
`https://integrate.api.nvidia.com/v1`. Our endpoint is
`https://inference-api.nvidia.com/v1` — a different API.

**Fix:** Use `--type openai` with an explicit base URL:

```bash
openshell provider create \
    --name inference-hub \
    --type openai \
    --credential "OPENAI_API_KEY=<key>" \
    --config "OPENAI_BASE_URL=https://inference-api.nvidia.com/v1"
```

The `openai` type works because the NVIDIA Inference Hub exposes an
OpenAI-compatible API.

### 11. The inference proxy injects the API key — the app must NOT send one

When using `inference.local`, the proxy adds `Authorization: Bearer <real-key>`
to the request before forwarding upstream. If the app also sends an
`Authorization` header (even an empty `Bearer `), it conflicts.

With an empty API key, `httpx` rejects the header entirely:
`Illegal header value b'Bearer '`.

**Fix:** Only add the `Authorization` header when the app has a real key (local
development). In the sandbox, omit it and let the proxy inject it:

```python
headers = {"Content-Type": "application/json"}
if config.api_key:
    headers["Authorization"] = f"Bearer {config.api_key}"
```

### 12. The `openshell inference set --model` **forces** the model name

The `--model` parameter on `openshell inference set` overrides whatever model
the app requests. If you configure `claude-opus-4.6` (dotted) but the API
expects `claude-opus-4-6` (hyphenated), every request fails with **401** even
though the key is valid.

**Fix:** The model name in the routing config must exactly match what the
upstream API accepts. Verify with `make test-auth` or a direct curl.

### 13. Bot message spam loops from `message_changed` events

When the bot posts a "Thinking..." placeholder and then updates it with
`chat_update`, Slack generates a `message_changed` event. Our original
`_should_ignore` filter only caught `subtype=bot_message`, not
`message_changed`. Each error response update triggered a new processing cycle,
creating an infinite spam loop.

**Fix:** Filter ALL events with any `subtype` — only process events where
`subtype` is `None` (real user messages):

```python
if event.get("subtype") is not None:
    return True
```

### 14. Error response rate limiting prevents the worst-case spam

Even with proper message filtering, a persistently failing backend can still
generate error messages for every inbound user message in a busy channel. We
added a per-channel rate limiter: at most 3 error messages per 60 seconds.
After that, error responses are silently suppressed (the thinking indicator is
deleted).

### 15. Network policy field reference for Python-based sandboxes

Here is the minimum viable policy structure for a Python app in OpenShell,
based on everything we learned:

```yaml
network_policies:
  # REST API endpoint — protocol: rest + tls: terminate lets the proxy
  # intercept, inspect headers, and resolve credential placeholders.
  slack_api:
    endpoints:
      - host: slack.com
        port: 443
        protocol: rest
        enforcement: enforce
        tls: terminate
        rules:
          - allow: { method: GET, path: "/**" }
          - allow: { method: POST, path: "/**" }
    binaries:
      - { path: /usr/local/bin/python* }

  # WebSocket endpoint — access: full creates a CONNECT tunnel.
  # Avoids the proxy's HTTP idle timeout killing the connection.
  slack_websocket:
    endpoints:
      - host: "*.slack.com"
        port: 443
        access: full
    binaries:
      - { path: /usr/local/bin/python* }

  # Inference — use inference.local, NOT the direct API endpoint.
  # Python sends CONNECT through the proxy, which inference.local
  # sidesteps entirely.
  inference:
    endpoints:
      - host: inference.local
        port: 443
        protocol: rest
        enforcement: enforce
        tls: terminate
        rules:
          - allow: { method: GET, path: "/**" }
          - allow: { method: POST, path: "/**" }
    binaries:
      - { path: /usr/local/bin/python* }
```

### 16. Sandbox provisioning is slow the first time

The first `openshell sandbox create` pulls images into the k3s cluster's
containerd store (separate from Docker Desktop's cache). The OpenShell base
image is ~1 GB and custom images need to be built and pushed. Expect 1-3
minutes for the first sandbox. Subsequent creates reuse cached layers and
take seconds.

### 17. Use `openshell doctor exec` to debug sandbox issues

When the sandbox is stuck or crash-looping:

```bash
openshell doctor exec -- kubectl get pods -n openshell
openshell doctor exec -- kubectl describe pod <name> -n openshell
openshell doctor exec -- kubectl logs <name> -n openshell --all-containers=true
```

This is how we discovered the `sandbox user not found` and `iproute2 missing`
errors — the `openshell logs` command only showed the sidecar, not the crash
reason.

### 18. Test scripts must work on macOS bash 3.2

macOS ships bash 3.2 which doesn't support `declare -A` (associative arrays).
Shell scripts targeting macOS must avoid bash 4+ features or use
`#!/usr/bin/env zsh`.

## Sources and References

- [`docs/design.md`](../../design.md)
- [`docs/deep_dives/hermes_deep_dive.md`](../../deep_dives/hermes_deep_dive.md)
- [`docs/deep_dives/openclaw_deep_dive.md`](../../deep_dives/openclaw_deep_dive.md)
- [`docs/deep_dives/openshell_deep_dive.md`](../../deep_dives/openshell_deep_dive.md)
- [`docs/deep_dives/nemoclaw_deep_dive.md`](../../deep_dives/nemoclaw_deep_dive.md)
- [OpenShell Policy Schema Reference](https://docs.nvidia.com/openshell/latest/reference/policy-schema.html)
- [OpenShell Provider & Credential Docs](https://docs.nvidia.com/openshell/latest/sandboxes/manage-providers.html)
- [NemoClaw Reference Policy](https://github.com/NVIDIA/NemoClaw/blob/main/nemoclaw-blueprint/policies/openclaw-sandbox.yaml)

## Appendix: Original TODO Tracker

These items were identified during the initial outline of this post. The table
below tracks their status and where in the post they are addressed.

| # | Item | Status | Covered In |
|---|---|---|---|
| 1 | **Slack permissions walkthrough** — OAuth scopes, bot vs user token, event subscriptions, Socket Mode vs HTTP endpoint, workspace installation flow, minimum permission set | ✅ Done | [Step 1: Create a Slack App](#step-1-create-a-slack-app) |
| 2 | **NVIDIA Inference Hub setup walkthrough** — Account creation, API keys, endpoint/model selection, backend configuration | ✅ Done | [Step 2: Get an NVIDIA Inference Hub API Key](#step-2-get-an-nvidia-inference-hub-api-key) |
| 3 | **Local development environment setup** — Prerequisites, dependencies, env vars, clone-to-first-run, smoke test | ✅ Done | [Prerequisites](#prerequisites), [Steps 3–7](#step-3-clone-and-configure) |
| 4 | **Connector implementation walkthrough** — Event normalization, response formatting, error propagation | ✅ Done | [Connector Implementation Walkthrough](#connector-implementation-walkthrough) |
| 5 | **Orchestrator implementation walkthrough** — Context assembly, prompt shaping, inference dispatch, retry logic, response construction | ✅ Done | [Orchestrator Implementation Walkthrough](#orchestrator-implementation-walkthrough) |
| 6 | **Inference backend interface walkthrough** — Generic backend contract, Inference Hub adapter, provider swappability | ✅ Done | [Steps 9–10](#step-9-register-the-inference-provider), [Lessons 9–12](#9-inference-routing-must-be-configured-separately) |
| 7 | **Observability and logging setup** — What gets logged, where logs live, how to read them, failure mode simulation | ✅ Done | [Step 6: Run Locally](#step-6-run-locally), [Step 15: Debugging](#step-15-debugging-a-failed-deployment), [Lesson 14](#14-error-response-rate-limiting-prevents-the-worst-case-spam) |
| 8 | **End-to-end demo / screenshots** — Annotated screenshots or recorded walkthrough of a message flowing through the system | ✅ Done | [Step 6: Run Locally](#step-6-run-locally), [Step 14: Verify the Deployment](#step-14-verify-the-deployment) |
