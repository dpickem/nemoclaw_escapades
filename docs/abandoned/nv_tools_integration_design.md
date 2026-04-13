# nv-tools Integration — Design Document

> **Status:** Abandoned
>
> **Last updated:** 2026-04-10
>
> **NOTE — This approach was abandoned.** Rather than wrapping nv-tools
> as a subprocess / MCP sidecar, we now port the relevant functionality
> directly into the `src/nemoclaw_escapades/tools/` package as native
> async tools (e.g. `tools/jira.py`). This gives us full control over
> the client interface, avoids the subprocess serialisation overhead,
> and lets the orchestrator's approval gate inspect tool calls at the
> schema level instead of parsing CLI output. The design below is kept
> for historical context only.
>
> **Archive merge note:** The duplicate copy at
> `docs/nv_tools_integration_design.md` was merged into this canonical
> archived location and removed.
>
> **Related:**
> [Orchestrator Design §5](orchestrator_design.md#5--tool-system) |
> [Orchestrator Design §7](orchestrator_design.md#7--permission--approval-system) |
> [Design Doc §7](design.md#7--capabilities-the-system-should-eventually-have) |
> [Design Doc §8.4](design.md#8--design-principles)

---

## Table of Contents

1. [Motivation](#1--motivation)
2. [nv-tools Overview](#2--nv-tools-overview)
3. [Architecture](#3--architecture)
4. [Stubbing Strategy for Open-Source Distribution](#4--stubbing-strategy-for-open-source-distribution)
5. [Docker Integration](#5--docker-integration)
6. [Credential and Authentication Model](#6--credential-and-authentication-model)
7. [Orchestrator Tool Discovery](#7--orchestrator-tool-discovery)
8. [Tool Calling Flow](#8--tool-calling-flow)
9. [Multi-Step Reasoning](#9--multi-step-reasoning)
10. [Write Approval via Slack](#10--write-approval-via-slack)
11. [Sandbox Policy Changes](#11--sandbox-policy-changes)
12. [Implementation Plan](#12--implementation-plan)
13. [Open Questions](#13--open-questions)

---

## 1  Motivation

The orchestrator is conversational-only in M1 — it receives Slack messages,
calls an LLM, and replies. It has no ability to take action on behalf of the
user. The [design doc §7](design.md#7--capabilities-the-system-should-eventually-have)
lists concrete goals that require interacting with external services:

- Check Slack, Google Docs, Jira for issues, blockers, gaps, and bugs.
- Categorize & prioritize issues automatically.
- Create design docs & prototypes overnight.
- Slack outreach — only with explicit confirmation.

These all require the orchestrator to **read from and write to** external
services — Jira, Confluence, Slack, GitLab, Gerrit, among others. Rather
than building custom API clients for each service, we integrate
**nv-tools**: a unified CLI that wraps multiple services with consistent
JSON output, a READ/WRITE safety model, and audit logging (more on nv-tools in a separate blog post).

nv-tools is the bridge between the orchestrator's LLM reasoning and the
real world. Integrating it unlocks multi-step tool calling: the LLM can
plan a sequence of nv-tools invocations, inspect intermediate results,
and chain them together to complete complex tasks.

---

## 2  nv-tools Overview

nv-tools is a Typer-based Python CLI that wraps service APIs behind a
single `nv-tools <service> <command>` interface. It is **not open-source**,
so for the purposes of this project we stub out its functionality (§4).
The repo URL for the real nv-tools is configured via `NV_TOOLS_REPO_URL`
in `.env` and is only needed for internal builds.

### Supported services

| Service | Example commands | Purpose |
|---------|-----------------|---------|
| `jira` | `get-issue`, `search`, `create-issue`, `transition` | Issue tracking |
| `confluence` | `search`, `get-page`, `create-page`, `update-page` | Wiki / docs |
| `slack` | `search`, `history`, `send`, `remind` | Messaging |
| `gitlab` | `list-mrs`, `get-mr-diffs`, `create-mr`, `merge-mr` | Code hosting |
| `gerrit` | `get-change`, `get-comments`, `review`, `submit` | Code review |
| `outlook` | `list-messages`, `get-message`, `list-events` | Email / calendar |
| `teams` | `chat-list`, `chat-read`, `chat-send`, `transcript-read` | Chat / meetings |
| `jenkins` | `list-jobs`, `list-builds`, `get-log` | CI/CD |
| `wandb` | `list-projects`, `list-runs`, `get-run` | Experiment tracking |
| `gdrive` | `search`, `get`, `metadata` | Google Drive |
| `glean` | `search`, `chat`, `read-document` | Enterprise search |
| ... | *(24+ services total)* | |

Additional services can be added over time by registering new Typer sub-apps in the CLI.

### Key properties

```
nv-tools <service> <command> [args] [--format json|text] [--write]

Output envelope (JSON):
{
  "success": true,
  "service": "jira",
  "command": "search",
  "operation_type": "READ",
  "data": { ... }
}
```

- **Consistent JSON output** — every command returns the same envelope.
- **READ/WRITE safety model** — mutating commands require `--write` or they
  exit with a warning. The `operation_type` field distinguishes READ from
  WRITE in the response.
- **Write rate limiting** — `NV_TOOLS_WRITE_RATE_LIMIT` caps writes per
  rolling minute to prevent runaway agent loops.
- **Rich help** — each service and command has Typer-generated help with
  argument descriptions, available via `nv-tools <service> --help` and
  `nv-tools <service> <command> --help`.

---

## 3  Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                    Orchestrator Agent Loop                         │
│                                                                    │
│  User (Slack)                                                      │
│       │                                                            │
│       ▼                                                            │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │  Agent Loop                                                  │  │
│  │                                                              │  │
│  │  1. Build system prompt (includes nv-tools skill summary)    │  │
│  │  2. LLM generates response                                   │  │
│  │     → may include tool_use: nv_tools_execute(...)            │  │
│  │  3. Permission check                                         │  │
│  │     → READ: auto-approve                                     │  │
│  │     → WRITE: escalate to Slack for approval                  │  │
│  │  4. Execute subprocess: nv-tools <service> <cmd>             │  │
│  │  5. Parse JSON envelope, extract data                        │  │
│  │  6. Feed result back as tool_result                          │  │
│  │  7. LLM continues reasoning (may call more tools)            │  │
│  │                                                              │  │
│  └──────────────────────────────────────────────────────────────┘  │
│       │                                                            │
│       ▼                                                            │
│  ┌────────────────────┐  ┌───────────────────────────────────────┐ │
│  │  nv-tools CLI      │  │  nv-tools Skill Definitions           │ │
│  │  (subprocess)      │  │  (progressive disclosure)             │ │
│  │                    │  │                                       │ │
│  │  /usr/local/bin/   │  │  Level 0: service catalog (~500 tok)  │ │
│  │    nv-tools        │  │  Level 1: service help (~1k tok)      │ │
│  │                    │  │  Level 2: command help (~200 tok)     │ │
│  └────────┬───────────┘  └───────────────────────────────────────┘ │
│            │                                                       │
│  ┌─────────┴────────────────────────────────────────────────────┐  │
│  │  External Services (via API)                                 │  │
│  │  Jira • Confluence • Slack • GitLab • Gerrit                 │  │
│  └──────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────┘
```

The orchestrator interacts with nv-tools through **two mechanisms**:

1. **Skill definitions** — the LLM learns what nv-tools can do via a
   progressive disclosure hierarchy (service catalog → service help →
   command help). This is token-efficient: progressive disclosure keeps
   the system prompt under ~500 tokens for the catalog regardless of
   how many services exist.

2. **Subprocess execution** — the LLM invokes nv-tools as a subprocess
   via a registered tool (`nv_tools_execute` or `nv_tools_help`). The
   orchestrator parses the JSON output and feeds it back as a tool result.

---

## 4  Stubbing Strategy for Open-Source Distribution

nv-tools wraps internal APIs that are not accessible outside the company.
The nemoclaw_escapades repo is public, so we need a stub
implementation that:

1. **Preserves the CLI interface** — same `nv-tools <service> <command>`
   invocation pattern, same `--help` output structure, same JSON envelope.
2. **Returns realistic mock data** — the LLM needs plausible responses to
   reason over during development and testing.
3. **Is self-contained** — no external API calls, no auth required.
4. **Lives in this repo** — under `stubs/nv_tools/`.

### Stub architecture

```
stubs/nv_tools/
├── pyproject.toml            # Installable as `nv-tools` (same entry point)
├── src/
│   └── nv_tools_stub/
│       ├── __init__.py
│       ├── cli.py            # Root Typer app mirroring real nv-tools
│       ├── responses.py      # Canned JSON responses per service/command
│       └── services/
│           ├── jira.py       # Stub: nv-tools jira <command>
│           ├── confluence.py
│           ├── slack.py
│           ├── gitlab.py
│           └── gerrit.py
└── README.md                 # Documents the stub and how to swap for real
```

### What stubs provide

| Aspect | Stub behavior |
|--------|---------------|
| **CLI interface** | Identical command names, arguments, and `--help` output |
| **JSON envelope** | Same `{ success, service, command, operation_type, data }` structure |
| **READ commands** | Return canned JSON with realistic field names and values |
| **WRITE commands** | Require `--write` flag (same safety gate); return success with a mock ID |
| **`--format text`** | Supported; renders a simplified text view of the mock data |
| **Auth** | Not required; stubs are stateless |
| **Error cases** | `nv-tools health` returns all services as "stub" |
| **Help text** | Mirrors real help text (argument names, descriptions, types) |

### Swap mechanism

The Dockerfile (§5) controls which version is installed. A build argument
selects between:

- `NV_TOOLS_SOURCE=stub` (default) — installs from `stubs/nv_tools/`
- `NV_TOOLS_SOURCE=gitlab` — clones and installs from the internal GitLab repo

The orchestrator code is identical in both cases — it always invokes
`nv-tools` as a subprocess and parses the JSON envelope.

---

## 5  Docker Integration

The Dockerfile needs to install nv-tools so it is available as a CLI tool
inside the sandbox. Two build paths, selected by a build argument:

### Updated Dockerfile.orchestrator

```dockerfile
# --- nv-tools installation ---
ARG NV_TOOLS_SOURCE=stub

# Option 1: Stub (default, open-source safe)
# Installs the stub package from this repo.
COPY stubs/nv_tools/ /build/nv-tools-stub/
RUN if [ "$NV_TOOLS_SOURCE" = "stub" ]; then \
      pip install --no-cache-dir /build/nv-tools-stub/ ; \
    fi

# Option 2: Real nv-tools from internal GitLab
# Requires network access and GitLab credentials at build time.
# Usage: docker build --build-arg NV_TOOLS_SOURCE=gitlab \
#                     --build-arg GITLAB_TOKEN=<token> ...
ARG NV_TOOLS_REPO_URL=""
ARG GITLAB_TOKEN=""
RUN if [ "$NV_TOOLS_SOURCE" = "gitlab" ]; then \
      pip install --no-cache-dir \
        "nv-tools @ git+https://oauth2:${GITLAB_TOKEN}@${NV_TOOLS_REPO_URL}" ; \
    fi
```

### Build commands

```bash
# Public / open-source build (default — uses stubs)
make build

# Internal build (real nv-tools from source repo — URL from .env)
make build NV_TOOLS_SOURCE=gitlab
```

### Filesystem policy addition

nv-tools is installed as a console script to `/usr/local/bin/nv-tools`.
The existing filesystem policy already grants read access to `/usr`. The
nv-tools config and token cache need a writable location:

```yaml
filesystem_policy:
  read_write:
    - /app/logs
    - /app/prompts
    - /tmp
    - /dev/null
    - /app/.config/nv-tools    # nv-tools config + token cache
```

---

## 6  Credential and Authentication Model

nv-tools services use different authentication models. Each service needs
its own credentials, and the injection mechanism depends on whether we run
inside an OpenShell sandbox or locally.

### Authentication models

| Model | Services (in scope) | How it works |
|-------|---------------------|--------------|
| **API token** | Jira, Confluence, GitLab, Gerrit, Slack | Static token in `.env`; passed as env var or HTTP header |
| **OAuth / SSO** | *(future services)* | `nv-tools auth login <service>` opens a browser for SSO; callback on `localhost:<port>` |
| **Device-code** | *(future services)* | No browser redirect; user visits a URL and enters a code from the terminal |

The five services in scope for this design all use **API tokens**. The
OAuth/SSO challenge is documented here for future services but deferred
from the initial implementation.

### 6.1  API token injection (Jira, Confluence, GitLab, Gerrit, Slack)

Each service requires its own API token (username + token for Jira/Confluence/
Gerrit, personal access token for GitLab, user OAuth token for Slack).
Three injection approaches were evaluated:

**~~Approach A: One OpenShell provider per service.~~** *(Does not work —
see below.)* Each provider holds one token. Attached to the sandbox at
creation. Matches the existing `inference-hub` and `slack-credentials`
pattern in the Makefile.

- ~~Pro: credentials never appear in the sandbox filesystem or env vars~~
- Con: five `openshell provider create` commands + five `--provider` flags
  on `sandbox create`
- **DOES NOT WORK:** OpenShell `--type generic` providers do not inject
  real environment variables.  The sandbox sees placeholder strings
  (`openshell:resolve:env:VAR_NAME`) instead of actual values.  These
  placeholders are only resolved by the L7 proxy when they appear in
  outbound HTTP headers — they are never substituted in `os.environ`.
  Since nv-tools reads all configuration from env vars via
  pydantic-settings, the provider-based approach fails for every
  service except Slack (whose token happens to be used in an HTTP
  Authorization header).  This was verified experimentally — see §6.3.

**~~Approach B: Single provider, multiple credentials.~~** *(Does not work
— same root cause as Approach A.)* One `generic` provider named
`nv-tools-credentials` holding all tokens as separate `--credential`
entries:

```bash
# This was the original plan — it does NOT work.
# Credentials arrive as openshell:resolve:env:... placeholders,
# not as real values.  See §6.3 for details.
openshell provider create \
    --name nv-tools-credentials \
    --type generic \
    --credential "JIRA_URL=$JIRA_URL" \
    --credential "JIRA_USERNAME=$JIRA_USERNAME" \
    --credential "JIRA_API_TOKEN=$JIRA_API_TOKEN" \
    ...
```

- **DOES NOT WORK:** Same L7 proxy placeholder issue as Approach A.
  The `generic` provider type is designed for applications that receive
  credentials via HTTP headers injected by the proxy (e.g. the
  `inference-hub` provider sets the `Authorization` header).  It is
  *not* a general-purpose env var injection mechanism.

**~~Approach C: Mounted `.env` file.~~** *(Not recommended.)* Bind-mount
the host's `.env` into the sandbox. nv-tools reads it directly.

- Pro: simplest; matches how nv-tools works locally
- Con: credentials visible on the sandbox filesystem (violates the
  OpenShell security model)
- Con: OpenShell does not support bind mounts in the sandbox policy

**Decision:** None of the above.  The provider-based approaches (A and B)
fail due to the L7 proxy placeholder issue, and file mounting (C)
violates the security model.  See §6.2 Strategy E for the working
solution: a host-side credential server that serves credentials over
HTTP, accessed via SSH reverse tunnel (for testing) or network policy
(for production).

### 6.2  OAuth / SSO services (future)

When additional services are added that require browser-based OAuth
(`nv-tools auth login <service>`), the sandbox presents three challenges:

1. **No browser** — the sandbox has no display or browser to open the
   SSO page.
2. **No inbound connections** — the OAuth callback server listens on
   `localhost:<port>`, but the sandbox's network policy blocks inbound
   traffic.
3. **Token lifecycle** — nv-tools caches OAuth tokens (access + refresh)
   at `~/.config/nv-tools/tokens.json` and auto-refreshes them using
   stored refresh tokens. Only the initial login requires a browser.

**Important constraint:** OpenShell providers cannot be attached to a
running sandbox — the sandbox must be recreated. However, provider
*credentials* can be updated on the gateway via
`openshell provider update`, and the new values take effect on the next
sandbox creation. Network policies are the only hot-reloadable component.

Five strategies, in order of complexity:

**~~Strategy A: Pre-authenticate on the host, inject via provider.~~**
*(Does not work — same L7 proxy placeholder issue as §6.1.)*

Following the same pattern as §6.1, OAuth tokens are pre-obtained on
the host and injected into the sandbox as an OpenShell provider — no
file mounts needed.

1. Run `nv-tools auth login <service>` on the host (one-time browser
   flow per service).
2. Extract the cached tokens from `~/.config/nv-tools/tokens.json`.
3. Register them as a provider on the gateway:

```bash
# Extract tokens from the host cache and register as a provider.
# A helper script (make setup-nv-tools-oauth) automates this.
openshell provider create \
    --name nv-tools-oauth-tokens \
    --type generic \
    --credential "SERVICE_A_ACCESS_TOKEN=..." \
    --credential "SERVICE_A_REFRESH_TOKEN=..." \
    --credential "SERVICE_B_ACCESS_TOKEN=..." \
    --credential "SERVICE_B_REFRESH_TOKEN=..."
```

4. Attach to the sandbox alongside `nv-tools-credentials`:

```bash
openshell sandbox create \
    --name orchestrator \
    ...
    --provider nv-tools-credentials \
    --provider nv-tools-oauth-tokens \
    ...
```

nv-tools inside the sandbox sees the tokens as environment variables
and uses them directly. When an access token expires, nv-tools
auto-refreshes it using the injected refresh token — no browser needed.

**Token refresh lifecycle:**

- Refresh tokens are typically long-lived (days to weeks).
- When nv-tools refreshes a token inside the sandbox, the new access
  token lives in the sandbox's memory only — it does not propagate
  back to the provider on the gateway.
- On sandbox recreation, the original tokens from the provider are
  re-injected. If the refresh token is still valid, nv-tools obtains
  a fresh access token automatically.
- If the refresh token itself has expired (rare — requires the sandbox
  to be down for an extended period), re-run `nv-tools auth login` on
  the host and update the provider:

```bash
openshell provider delete nv-tools-oauth-tokens
openshell provider create --name nv-tools-oauth-tokens ...  # with fresh tokens
```

A Makefile target (`make refresh-nv-tools-oauth`) can automate the
extract-and-update cycle.

**Why this doesn't work (two independent issues):**

1. **L7 proxy placeholder issue (verified):** The `generic` provider
   does not inject real env vars — it sets them to
   `openshell:resolve:env:VAR_NAME` placeholders that are only resolved
   in outbound HTTP headers.  nv-tools reads credentials from
   `os.environ` via pydantic-settings, so it sees the placeholder
   strings, not the real values.  This was confirmed experimentally
   by inspecting `env` output inside a sandbox with a `generic`
   provider attached (see §6.3).

2. **POST body rewrite limitation (documented):** Even if env var
   injection worked, the L7 proxy cannot rewrite POST request bodies.
   OAuth2 token refresh requires a POST with `client_id`,
   `client_secret`, and `refresh_token` in the body — the proxy cannot
   inject these dynamically.

See Strategy E for the recommended alternative.

**Strategy B: SSH tunnel + device-code flow.** For services that support device-code flow, the
sandbox prints a URL and code to the orchestrator's logs; the user
authenticates on any device. For browser-redirect services, tunnel the
callback port from the sandbox to the host:

```bash
ssh -N -L 8766:localhost:8766 sandbox-host
```

This matches the SSH tunnel pattern already documented in nv-tools'
`auth.py` for remote development.

- Pro: works without pre-authentication
- Con: requires SSH tunnel setup and sandbox network policy changes
  (inbound port allowance)

**Strategy C (long-term): Auth sidecar on the host.** When the
orchestrator migrates to the `tools.local` sidecar model
([§11, Approach B](#11--sandbox-policy-changes)), the sidecar runs on the
host with full browser and network access. OAuth flows happen on the host;
the sandbox only sees tool results via `tools.local`.

- Pro: cleanest solution — sandbox never touches auth
- Con: requires the sidecar architecture (deferred)

**Strategy D: Slack-mediated authentication.** The orchestrator uses its
existing Slack connector to guide the user through OAuth flows
interactively — no host-side CLI session required.

*Device-code flows:*

1. The orchestrator initiates the device-code flow programmatically.
2. The auth provider returns a verification URL and a user code.
3. The orchestrator sends a Slack message:
   "Please visit https://microsoft.com/devicelogin and enter
   code: `ABCD1234`"
4. The orchestrator polls in the background until the user completes
   authentication (or the code expires).
5. The resulting tokens are stored and injected into the provider
   for subsequent sandbox recreations.

This is a natural fit — device-code flows are designed for headless
environments, and Slack is the orchestrator's primary user channel.

*Browser-redirect flows:*

These require a redirect URI (`localhost:<port>/callback`) that the
SSO provider redirects the browser to after login. Since the sandbox
cannot host an inbound HTTP server, the orchestrator acts as a
coordinator:

1. The orchestrator generates the authorization URL and sends it to
   the user via Slack.
2. The user clicks the link and authenticates in their browser.
3. The SSO provider redirects to `localhost:<port>/callback?code=...`.
   Since there is no local server, the browser shows an error page —
   but the URL bar contains the authorization code.
4. The orchestrator sends a follow-up Slack message:
   "Authentication successful? Please paste the URL from your
   browser's address bar."
5. The user pastes the redirect URL into Slack.
6. The orchestrator extracts the authorization code from the URL,
   completes the token exchange, and stores the tokens.

This paste-back step is a one-time friction point per service. Once
the tokens (including long-lived refresh tokens) are obtained, all
subsequent refreshes happen automatically inside the sandbox.

*Future improvement:* If nv-tools registers a publicly reachable
callback URL (e.g., via a lightweight relay or the orchestrator's own
web UI from [Design Doc §9](design.md#9--web-ui--mission-control-dashboard)),
step 4-5 can be eliminated entirely — the callback completes
automatically and the orchestrator receives the code without user
intervention.

- Pro: no host-side CLI session needed — user authenticates from any
  device via Slack
- Pro: device-code flows are fully seamless (no paste-back)
- Pro: tokens are injected into the provider, consistent with §6.1
- Con: browser-redirect flows require a one-time URL paste-back until
  a relay endpoint is available

**Strategy E (recommended): Host-side token server.**

A lightweight HTTP server runs on the host, holds the refresh token,
and serves short-lived access tokens to the sandbox on demand. The
sandbox **never sees the refresh token** — only ~1-hour access tokens
fetched via `GET /token` before each nv-tools invocation. This pattern
was proven in the
[gogcli-skill demo](https://github.com/brevdev/nemoclaw-demos/pull/2)
for Google Workspace services and avoids the L7 proxy limitation that
affects Strategy A.

```
sandbox (nv-tools wrapper) ──GET /token──► host token server ──OAuth2 refresh──► service APIs
```

**Setup:**

1. Pre-authenticate on the host (`nv-tools auth login <service>`),
   same as Strategy A step 1.
2. Start a host-side token server that reads the cached refresh token
   and exposes two endpoints:
   - `GET /token` — returns a fresh access token (auto-refreshes when
     within 5 minutes of expiry)
   - `GET /health` — liveness check
3. Push a thin wrapper script into the sandbox that fetches a token
   from the host server before each nv-tools invocation:

```bash
#!/bin/sh
_TOKEN=$(curl -sf "http://${HOST_IP}:${TOKEN_PORT}/token") || {
    echo "error: could not reach token server" >&2
    exit 1
}
exec env SERVICE_ACCESS_TOKEN="$_TOKEN" nv-tools "$@"
```

4. Add a network policy allowing the sandbox to reach the token
   server on the host:

```yaml
nv_tools_token_server:
    name: nv-tools-token-server
    endpoints:
      - host: <host-ip>
        port: 9100
        protocol: rest
        enforcement: enforce
        rules:
          - allow: { method: GET, path: "/token" }
          - allow: { method: GET, path: "/health" }
    binaries:
      - { path: /usr/local/bin/python* }
      - { path: /usr/local/bin/nv-tools }
```

**Token server implementation:**

The token server is a single-file Python HTTP server (~100 lines)
that caches access tokens in memory and refreshes them proactively.
The reference implementation
([`gog-token-server.py`](https://github.com/brevdev/nemoclaw-demos/pull/2))
demonstrates the pattern:

```python
# Simplified — see reference for full implementation
_lock = threading.Lock()
_access_token: str = ""
_expires_at: float = 0.0

def get_access_token(client_id, client_secret, refresh_token) -> str:
    global _access_token, _expires_at
    with _lock:
        if _access_token and time.monotonic() < _expires_at - 300:
            return _access_token
        _access_token, _expires_at = _exchange(
            client_id, client_secret, refresh_token
        )
        return _access_token
```

**Token lifecycle:**

- The token server caches access tokens in memory and refreshes them
  proactively (5-minute buffer before expiry).
- No sandbox recreation is needed for token refresh — the host server
  handles it continuously.
- If the refresh token itself expires (rare), re-run
  `nv-tools auth login <service>` on the host and restart the token
  server. The sandbox is unaffected.

**Defense-in-depth: read-only network policies.**

As an additional safety layer, sandbox network policies can restrict
HTTP methods per service endpoint — e.g., allowing only GET for
read-only services. This complements the write approval gate (§10):
even if the LLM constructs a write command that somehow bypasses
approval, the network policy blocks the mutating API call at the
sandbox level.

```yaml
# Example: read-only Jira access at the network level
nv_tools_jira:
    endpoints:
      - host: jira.example.com
        port: 443
        rules:
          - allow: { method: GET, path: "/**" }
          # - allow: { method: POST, path: "/**" }   # uncomment to enable writes
          # - allow: { method: PUT, path: "/**" }
```

Write methods can be commented out by default and selectively enabled
per service as the write approval gate (§10) matures. This follows the
pattern established in the
[gogcli-skill `policy.yaml`](https://github.com/brevdev/nemoclaw-demos/pull/2).

- Pro: refresh token never enters the sandbox — strongest isolation
- Pro: no sandbox recreation needed for token refresh
- Pro: no dependency on OpenShell v2 Providers
- Pro: proven pattern with a working reference implementation
- Pro: read-only network policies provide defense-in-depth
- Con: requires a host-side process (lightweight — single Python script)
- Con: adds a network hop per invocation (~1ms on localhost)

**Decision:** Strategy E — host-side credential server.  This is the
**only working approach** for injecting nv-tools credentials into an
OpenShell sandbox, since the `generic` provider's L7 proxy placeholder
mechanism (Strategies A, B in §6.2 and Approaches A, B in §6.1) does
not produce real environment variables.

Strategy E provides the strongest credential isolation (secrets never
enter the sandbox image or filesystem), requires no sandbox recreation
for credential changes, and has a proven reference implementation
([brevdev/nemoclaw-demos#2](https://github.com/brevdev/nemoclaw-demos/pull/2)).
The implementation uses `scripts/nv-tools-credential-server.py` on the
host and an SSH reverse tunnel (`-R 9100:localhost:9100`) for test
sandboxes.

Strategy D (Slack-mediated device-code flow) remains a natural
complement for interactive OAuth authentication. Strategy C (full
sidecar) is the long-term target when the service count grows.
~~Strategy A is not viable~~ — see §6.3 for details.

### 6.3  Lessons Learned (Implementation)

The following issues were discovered during initial implementation and
are documented here for future reference.

**OpenShell `generic` providers do not inject real environment variables.**
The `--type generic` provider uses the L7 proxy to resolve credential
placeholders in HTTP headers at request time.  Inside the sandbox, the
env vars are set to `openshell:resolve:env:VAR_NAME` placeholder
strings, not actual values.  This works for credentials passed as HTTP
`Authorization` headers (e.g. Slack bot tokens), but not for
applications like nv-tools that read credentials from env vars directly
via pydantic-settings.  The provider-based approach (§6.1 Approach B)
was attempted and failed for all services except Slack.

**`openshell sandbox create` does not support `--build-arg`.**
The internal Docker build uses the legacy builder, not BuildKit.  Build
arguments cannot be passed through `openshell sandbox create --from .`.
The Dockerfile uses `ARG NV_TOOLS_SOURCE=auto` with auto-detection
(checking for `pyproject.toml` in `.nv-tools-src/`) as a workaround.

**`openshell exec` does not exist.**
Command execution inside a sandbox uses SSH via `openshell sandbox
connect` (interactive) or `ssh` with `openshell ssh-proxy` as a
`ProxyCommand`.  The `openshell forward` command maps host ports *into*
the sandbox (like SSH `-L`), not the reverse.

**Strategy E implementation: credential server + network policy.**
The implemented approach uses a host-side HTTP credential server
(`scripts/nv-tools-credential-server.py`) that reads
`~/workspace/nv_tools/.env` and serves all nv-tools credentials as JSON
via `GET /credentials`.  The sandbox reaches it via a network policy
entry for the Docker host gateway IP (auto-detected at runtime by the
Makefile and injected into the policy via `openshell policy set`).
The test script (`scripts/test_nv_tools_sandbox.sh`) fetches credentials
from the server, exports them as env vars, then runs `nv-tools health`
and per-service smoke tests.

Additional policy lessons:
- `host.docker.internal` does not work as a policy hostname — the OPA
  engine cannot resolve it.  The Makefile detects the gateway IP from
  inside the sandbox (`python3 socket.getaddrinfo(...)`) and injects it
  into the policy at runtime.
- Plain HTTP endpoints require `tls: skip` in the policy.  Without it,
  the proxy attempts TLS termination and returns 403.
- `tls: terminate` is deprecated in recent OpenShell versions — TLS
  termination is now automatic.  Explicit `tls: terminate` entries
  produce a warning but still work.

**VPN-gated services (GitLab, Gerrit) are not reachable from Docker
Desktop sandboxes.**  The OpenShell sandbox runs in a k3s pod inside
Docker Desktop's Linux VM.  The corporate VPN tunnel
(GlobalProtect/Cisco AnyConnect) runs on the macOS host but does not
extend into Docker's VM — the VM has its own network stack.  Services
behind the VPN (`gitlab-master.nvidia.com`, `git-av.nvidia.com`) return
403 from the sandbox even with correct credentials and network policies.

Validated results on Docker Desktop (macOS):
- Jira (`jirasw.nvidia.com`) — OK (not VPN-gated)
- Confluence (`nvidia.atlassian.net`) — OK (Atlassian Cloud, not VPN-gated)
- Slack (`slack.com`) — OK (public API)
- GitLab (`gitlab-master.nvidia.com`) — FAIL 403 (VPN-gated)
- Gerrit (`git-av.nvidia.com`) — FAIL 403 (VPN-gated)

Four options for reaching VPN-gated services from a sandbox:

1. **Host-side API proxy (recommended short-term).** Extend the
   credential server to also proxy API requests through the host, which
   has VPN access.  The sandbox sends `nv-tools` traffic to the proxy
   instead of directly to the service endpoints.  Same pattern as the
   credential server — one more endpoint per service on the host.

2. **VPN passthrough for Docker.** Configure the VPN client to route
   Docker subnets through the VPN tunnel.  GlobalProtect supports
   "split tunnel include" rules that can add Docker's network ranges.
   Requires VPN admin or IT configuration — may not be self-service.

3. **Remote host with VPN (recommended long-term).** Run the OpenShell
   gateway on an NVIDIA Brev instance or DGX Spark that is natively on
   the corporate network.  All services are reachable without VPN
   workarounds.  This is the intended production deployment model
   (see [Hosting Deep Dive](deep_dives/hosting_deep_dive.md)).

4. **Split testing.** Validate non-VPN services (Jira, Confluence,
   Slack) inside the sandbox; validate VPN-gated services (GitLab,
   Gerrit) with a host-side `nv-tools health` run.  Works immediately
   but doesn't exercise the full sandbox network policy path.

**Decision:** Option 3 (remote host) is the long-term target.  For
local development on Docker Desktop, the sandbox test validates
3/5 services end-to-end.  GitLab and Gerrit will be validated when
the gateway moves to a VPN-connected host.

### 6.4  Pivot: Direct REST Clients (Replacing nv-tools Subprocess)

After extensive experimentation with the nv-tools subprocess approach
(Strategy E credential server, OpenShell provider injection, SSH
tunneling), we pivoted to implementing service clients directly in
Python.  The nv-tools CLI approach had compounding problems:

1. **Credential injection doesn't work for CLI tools.**  OpenShell's
   `generic` provider resolves placeholders in HTTP headers, not in env
   vars.  nv-tools reads credentials from env vars — the two models are
   incompatible (§6.3).

2. **The credential server workaround was fragile.**  The host-side
   credential server + network policy approach worked but required:
   runtime IP detection from inside the sandbox, dynamic policy
   hot-reloading via `openshell policy set`, `tls: skip` for plain
   HTTP, and `host.docker.internal` hostname resolution quirks.

3. **`openshell sandbox create` blocks on the entrypoint.**  Creating
   a sandbox with `-- sleep infinity` blocked the Makefile recipe
   indefinitely, making automated test flows impossible.

4. **VPN-gated services are unreachable from Docker Desktop.**  GitLab
   and Gerrit require VPN access, but the Docker Desktop VM doesn't
   share the host's VPN tunnel.

**The direct REST approach resolves all of these.**  Each service gets
a Python client (async httpx) that reads a pre-computed `Authorization`
header from an env var.  The OpenShell L7 proxy resolves the
`openshell:resolve:env:*` placeholder in the outbound HTTP header —
this is exactly how the `inference` provider works for the
`Authorization: Bearer` header, just applied to `Basic` auth.

Implementation:
- `src/nemoclaw_escapades/tools/jira.py` — async `JiraClient` lifted
  from `nv_tools.clients.jira`, 8 tool handlers registered with the
  orchestrator's `ToolRegistry`
- Pre-computed `JIRA_AUTH=Basic <base64>` credential registered as an
  OpenShell `generic` provider (`make setup-jira-provider`)
- No subprocess, no credential server, no SSH tunneling

This pattern extends naturally to Confluence, GitLab, Gerrit, and Slack
as additional `tools/<service>.py` files — one async client per service,
one pre-computed auth header per provider.

---

## 7  Orchestrator Tool Discovery

The orchestrator needs to know what nv-tools can do. Following the patterns
established in [Orchestrator Design §5](orchestrator_design.md#5--tool-system),
we use **progressive disclosure** (from Hermes) to keep the system prompt
lean while making the full tool surface discoverable.

### Three-level progressive disclosure

```
Level 0: Service Catalog                          ~500 tokens
┌──────────────────────────────────────────────┐
│  nv-tools: unified CLI for services          │
│                                              │
│  Services:                                   │
│  • jira       — Issue tracking (CRUD)        │
│  • confluence — Wiki pages (CRUD)            │
│  • slack      — Search, history, send        │
│  • gitlab     — MRs, pipelines, repos        │
│  • gerrit     — Code review (CLs)            │
│                                              │
│  Use nv_tools_help("<service>") to see       │
│  available commands for a service.           │
│  Use nv_tools_help("<service> <command>")    │
│  to see argument details.                    │
└──────────────────────────────────────────────┘

Level 1: Service Help                             ~500–1500 tokens
(returned by nv_tools_help("jira"))
┌──────────────────────────────────────────────┐
│  nv-tools jira — Jira issue management       │
│                                              │
│  READ commands:                              │
│  • get-issue <key>                           │
│  • search <jql> [--limit N]                  │
│  • get-transitions <key>                     │
│  • get-boards [--project P]                  │
│  • get-sprints <board_id>                    │
│  • get-sprint-issues <sprint_id>             │
│  • me                                        │
│                                              │
│  WRITE commands (require --write):           │
│  • create-issue --project P --summary S      │
│  • update-issue <key> --field val            │
│  • add-comment <key> --body B                │
│  • transition <key> --status S               │
│  • link-issues <key1> <key2> --type T        │
│  • delete-issue <key>                        │
└──────────────────────────────────────────────┘

Level 2: Command Help                             ~100–300 tokens
(returned by nv_tools_help("jira create-issue"))
┌──────────────────────────────────────────────┐
│  nv-tools jira create-issue                  │
│  (WRITE operation — requires --write)        │
│                                              │
│  Arguments:                                  │
│    --project TEXT  Project key [required]    │
│    --summary TEXT  Issue summary [required]  │
│    --type TEXT     Issue type (default Task) │
│    --description TEXT  Description body      │
│    --assignee TEXT  Assignee username        │
│    --priority TEXT  Priority name            │
│    --labels TEXT   Comma-separated labels    │
│    --write / -w   Confirm write operation    │
│    --format TEXT   json or text              │
└──────────────────────────────────────────────┘
```

### Registered tools

Two tools are registered in the orchestrator's tool registry:

```python
@register(
    name="nv_tools_help",
    description=(
        "Query nv-tools help. Call with no args for the service catalog. "
        "Call with a service name for its commands. "
        "Call with 'service command' for argument details."
    ),
    toolset="nv_tools",
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Empty for catalog, 'service' for commands, 'service command' for args"
            }
        }
    },
    required_permission=PermissionMode.ReadOnly,
    is_concurrency_safe=True,
    is_read_only=True,
)
async def nv_tools_help(query: str = "") -> str:
    """Run `nv-tools [query] --help` and return the output."""
    ...


@register(
    name="nv_tools_execute",
    description="Execute an nv-tools command. Returns JSON with the result.",
    toolset="nv_tools",
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Full command string, e.g. 'jira search \"project = MYPROJ\"'"
            }
        },
        "required": ["command"]
    },
    required_permission=PermissionMode.FullAccess,
    is_concurrency_safe=True,
    is_read_only=False,
)
async def nv_tools_execute(command: str) -> str:
    """Execute `nv-tools <command> --format json` as a subprocess."""
    ...
```

### System prompt injection

The Level 0 service catalog is injected into the system prompt as a skill
summary, keeping it under ~500 tokens. The LLM uses `nv_tools_help` to
drill deeper on demand.

```
## Available Tools: nv-tools

nv-tools is a CLI for external services. You can query help for any
service to learn its commands, then execute commands to read or modify
data.

Services: jira, confluence, slack, gitlab, gerrit

To discover commands:  nv_tools_help("jira")
To get argument info:  nv_tools_help("jira create-issue")
To execute a command:  nv_tools_execute("jira search 'project = MYPROJ AND status = Open'")

IMPORTANT:
- WRITE commands (create, update, delete, transition, send) require
  approval before execution. The system will request approval via Slack
  before running any WRITE operation.
- Always check the `success` field in the JSON response.
- Use `--limit` on search/list commands to avoid large outputs.
```

---

## 8  Tool Calling Flow

### Execution pipeline

```
LLM generates tool_use: nv_tools_execute(command="jira search 'project=MYPROJ'")
    │
    ▼
┌──────────────────────────────────────────────────────────────┐
│  1. Parse command string                                     │
│     service = "jira", subcommand = "search", args = [...]    │
│                                                              │
│  2. Classify operation type                                  │
│     Heuristic: does the command contain --write or -w?       │
│     If yes → WRITE. If no → READ.                            │
│     Backup: check known WRITE commands per service.          │
│                                                              │
│  3. Permission gate                                          │
│     READ  → auto-approve (fast path)                         │
│     WRITE → escalate to Slack approval (§10)                 │
│                                                              │
│  4. Execute subprocess                                       │
│     cmd = ["nv-tools"] + shlex.split(command)                │
│           + ["--format", "json"]                             │
│     result = await create_subprocess_exec(*cmd, ...)         │
│     stdout, stderr = await proc.communicate()                │
│     timeout: 60s (configurable per service)                  │
│                                                              │
│  5. Parse response                                           │
│     envelope = json.loads(stdout)                            │
│     if not envelope["success"]:                              │
│         return format_error(envelope["error"])               │
│     return format_result(envelope["data"])                   │
│                                                              │
│  6. Audit log (ToolCallDB)                                   │
│     Log the full invocation: command, operation type,        │
│     duration, success/failure, approval decision,            │
│     response payload. See §8.2 for the schema.               │
│                                                              │
│  7. Return as tool_result to the agent loop                  │
│     The LLM sees the data and can reason over it,            │
│     potentially calling more tools.                          │
└──────────────────────────────────────────────────────────────┘
```

### Tool call audit database (ToolCallDB)

The orchestrator owns the audit trail for all nv-tools invocations,
following the same pattern as the NMB broker's `AuditDB`
(see [NMB Design §4](nmb_design.md#4--message-broker)). Every tool call —
READ or WRITE, success or failure — is persisted to a local SQLite
database with full request and response payloads.

**Why the orchestrator, not nv-tools?**

- **Single source of truth** — the orchestrator sees the full picture:
  which LLM turn triggered the call, the approval decision, the thread
  context, and the downstream reasoning. nv-tools only sees its own
  invocation in isolation.
- **Training flywheel** — tool call traces feed into SFT/DPO data
  generation (see [Training Flywheel Design](training_flywheel_deep_dive.md)).
  The orchestrator can join tool call logs with conversation transcripts
  to produce complete agent traces.
- **Stub-compatible** — the audit works identically with the stub and
  real nv-tools since logging happens at the orchestrator layer.

**SQLAlchemy ORM model** (matching `nmb.audit.models`):

```python
class ToolCallRow(Base):
    """ORM model for the ``tool_calls`` audit table."""

    __tablename__ = "tool_calls"

    # Identity
    id: Mapped[str] = mapped_column(String, primary_key=True)
    timestamp: Mapped[float] = mapped_column(Float, nullable=False)
    session_id: Mapped[str | None] = mapped_column(String)
    thread_ts: Mapped[str | None] = mapped_column(String)

    # Command
    service: Mapped[str] = mapped_column(String, nullable=False)
    command: Mapped[str] = mapped_column(String, nullable=False)
    args: Mapped[str] = mapped_column(String, nullable=False)
    operation_type: Mapped[str] = mapped_column(String, nullable=False)

    # Approval (WRITE only)
    approval_status: Mapped[str | None] = mapped_column(String)
    approved_by: Mapped[str | None] = mapped_column(String)
    approval_time_ms: Mapped[float | None] = mapped_column(Float)

    # Execution
    exit_code: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[float] = mapped_column(Float, nullable=False)
    success: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(String)

    # Payloads
    response_payload: Mapped[str | None] = mapped_column(String)
    payload_size: Mapped[int] = mapped_column(Integer, nullable=False)
```

The DDL (including indexes and the FTS5 virtual table) lives in an Alembic
migration, same as the NMB audit schema:

```python
# alembic/versions/001_tool_calls_schema.py

def upgrade() -> None:
    op.execute("""
        CREATE TABLE tool_calls (
            id               TEXT PRIMARY KEY,
            timestamp        REAL NOT NULL,
            session_id       TEXT,
            thread_ts        TEXT,
            service          TEXT NOT NULL,
            command          TEXT NOT NULL,
            args             TEXT NOT NULL,
            operation_type   TEXT NOT NULL,
            approval_status  TEXT,
            approved_by      TEXT,
            approval_time_ms REAL,
            exit_code        INTEGER,
            duration_ms      REAL NOT NULL,
            success          INTEGER NOT NULL,
            error_code       TEXT,
            error_message    TEXT,
            response_payload TEXT,
            payload_size     INTEGER NOT NULL
        )
    """)

    op.execute("CREATE INDEX idx_tc_timestamp ON tool_calls(timestamp)")
    op.execute("CREATE INDEX idx_tc_session ON tool_calls(session_id)")
    op.execute("CREATE INDEX idx_tc_service ON tool_calls(service)")
    op.execute("CREATE INDEX idx_tc_operation ON tool_calls(operation_type)")

    op.execute("""
        CREATE VIRTUAL TABLE tool_calls_fts USING fts5(
            args, response_payload,
            content=tool_calls, content_rowid=rowid
        )
    """)
```

**ToolCallDB class** (reuses the `AuditDB` patterns: async SQLAlchemy +
aiosqlite engine, WAL mode, Alembic migrations on `open()`, configurable
payload persistence):

```python
class ToolCallDB:
    """Audit database for nv-tools invocations.

    Follows the same design as nmb.audit.AuditDB — async SQLite with
    Alembic-managed schema, WAL mode, and optional payload persistence.
    """

    def __init__(self, db_path: str, *, persist_payloads: bool = True) -> None: ...

    async def open(self) -> None: ...
    async def close(self) -> None: ...

    async def log_call(
        self,
        *,
        session_id: str | None,
        thread_ts: str | None,
        service: str,
        command: str,
        args: str,
        operation_type: str,
        approval_status: str | None,
        approved_by: str | None,
        approval_time_ms: float | None,
        exit_code: int | None,
        duration_ms: float,
        success: bool,
        error_code: str | None,
        error_message: str | None,
        response_payload: str,
    ) -> None:
        """Log a single nv-tools invocation."""
        ...

    async def query(self, sql: str, params: dict | None = None) -> list[dict]: ...
    async def export_jsonl(self, path: str, since: float | None = None) -> int: ...
```

### Output truncation

nv-tools responses can be large (e.g., `jira search` returning 50 issues).
The executor applies truncation before feeding results back to the LLM:

| Strategy | Threshold | Action |
|----------|-----------|--------|
| Character limit | >8000 chars | Truncate with `"... (truncated, {n} chars omitted)"` |
| Item limit | >20 items in a list | Keep first 20, append `"... ({m} more items)"` |
| Field pruning | — | Strip verbose fields (`description`, `body`) from list results; keep them for single-item gets |

The LLM can request more detail by calling specific get commands
(e.g., `jira get-issue PROJ-1234` after seeing it in a search result).

---

## 9  Multi-Step Reasoning

The primary value of nv-tools integration is enabling the orchestrator to
complete tasks that require **multiple tool calls with intermediate
reasoning**. This is the core capability that turns the orchestrator from
a chatbot into an agent.

### Example: Jira triage workflow

```
User (Slack): "Check my open Jira tickets and summarize which ones are blocked"

LLM reasoning step 1:
  I need to find the user's open tickets. Let me search Jira.
  → tool_use: nv_tools_execute("jira search 'assignee = currentUser() AND status != Done' --limit 20")

Tool result: [PROJ-101, PROJ-234, PROJ-567, ...]

LLM reasoning step 2:
  I have 8 open tickets. I need to check which ones have blockers.
  Let me get details for each.
  → tool_use: nv_tools_execute("jira get-issue PROJ-101")
  → tool_use: nv_tools_execute("jira get-issue PROJ-234")
  → tool_use: nv_tools_execute("jira get-issue PROJ-567")
  (concurrent — these are read-only and concurrency-safe)

Tool results: [details for each issue including linked issues, status, blockers]

LLM reasoning step 3:
  I can see PROJ-234 is blocked by PROJ-100 (still in review), and
  PROJ-567 has an unresolved dependency on the infra team.
  Let me compose a summary for the user.

Final response (Slack):
  "You have 8 open tickets. 2 are blocked:
   • PROJ-234 — blocked by PROJ-100 (in review by @alice)
   • PROJ-567 — waiting on infra team for API access
   The other 6 are progressing normally."
```

### Example: Cross-service research workflow

```
User (Slack): "Prepare me for my 1:1 with Alice tomorrow"

LLM reasoning step 1:
  I need to find Alice's recent activity across Jira and Gerrit.
  → tool_use: nv_tools_execute("jira search 'assignee = alice AND updated >= -7d' --limit 10")
  → tool_use: nv_tools_execute("gerrit list-changes --owner alice --since 7d --limit 5")

LLM reasoning step 2:
  Alice has 3 active Jira tickets and 2 open CLs.
  Let me check if any of her CLs need my review.
  → tool_use: nv_tools_execute("gerrit list-changes --reviewer me --owner alice")

LLM reasoning step 3:
  I have CL 12345 from Alice that I haven't reviewed yet.
  Let me get the diff summary.
  → tool_use: nv_tools_execute("gerrit get-change 12345")

Final response:
  "Here's a briefing for your 1:1 with Alice:
   ..."
```

### Example: Write operation with approval

```
User (Slack): "Create a Jira ticket for the auth service refactor"

LLM reasoning step 1:
  I need to create a ticket. This is a WRITE operation.
  → tool_use: nv_tools_execute("jira create-issue --project PROJ --summary 'Refactor auth service' --type Task --write")

Permission gate:
  WRITE detected → escalate to Slack
  → Slack message: "🔒 Approval required: nv-tools jira create-issue
     Project: PROJ
     Summary: Refactor auth service
     Type: Task
     [Approve] [Reject]"

User clicks [Approve]

Execution proceeds → returns { success: true, data: { key: "PROJ-890" } }

Final response:
  "Created PROJ-890: Refactor auth service"
```

### Chaining patterns

| Pattern | Description | Example |
|---------|-------------|---------|
| **Search → Get** | List results, then fetch details | `jira search` → `jira get-issue` for each |
| **Read → Write** | Inspect state, then modify | `gerrit get-change` → `gerrit review --label Code-Review=+2 --write` |
| **Cross-service join** | Combine data from multiple services | `jira search` + `gerrit list-changes` + `confluence search` |
| **Iterative refinement** | Narrow a search based on results | `slack search "auth bug"` → `slack history <channel>` → `jira get-issue <key>` |
| **Verify after write** | Confirm a mutation succeeded | `jira create-issue --write` → `jira get-issue <new-key>` |

---

## 10  Write Approval via Slack

Following [Design Doc §8.4](design.md#8--design-principles) ("Safety by
default — write operations require explicit confirmation") and the
[Orchestrator Design §7](orchestrator_design.md#7--permission--approval-system)
tiered approval system, all nv-tools WRITE operations require Slack
approval before execution.

### Classification

```
nv-tools command received
        │
        ▼
┌─────────────────────────────────────────┐
│  Stage 1: Static classification         │
│                                         │
│  Is --write or -w in the command?       │
│  Is the command in the known WRITE      │
│  set for this service?                  │
│    jira: create-issue, update-issue,    │
│          add-comment, transition,       │
│          link-issues, delete-issue      │
│    slack: send, remind                  │
│    gerrit: review, submit, abandon      │
│    confluence: create-page,             │
│                update-page, delete-page │
│    gitlab: create-mr, merge-mr,         │
│            approve-mr                   │
└───────────────────┬─────────────────────┘
                    │
               ┌────┴────┐
               │         │
             READ      WRITE
               │         │
          auto-approve   │
               │    ┌────┴──────────────────────┐
               │    │  Stage 2: Risk assessment  │
               │    │                            │
               │    │  LOW risk:                 │
               │    │  • jira add-comment        │
               │    │  • slack send (to self)    │
               │    │                            │
               │    │  HIGH risk:                │
               │    │  • gerrit submit           │
               │    │  • jira delete-issue       │
               │    │  • gitlab merge-mr         │
               │    │  • slack send (to others)  │
               │    └────────────┬───────────────┘
               │                 │
               │            ┌────┴────┐
               │            │         │
               │           LOW      HIGH
               │            │         │
               ▼            ▼         ▼
            execute  ┌──────────────────────────────┐
                     │  Slack approval message      │
                     │                              │
                     │  nv-tools WRITE request      │
                     │                              │
                     │  Service: jira               │
                     │  Command: create-issue       │
                     │  Args:                       │
                     │    --project PROJ            │
                     │    --summary "Refactor..."   │
                     │    --type Task               │
                     │                              │
                     │  Risk: LOW / HIGH            │
                     │                              │
                     │  [✅ Approve] [❌ Reject]     │
                     └──────────────────────────────┘
```

### Approval flow

1. **Agent loop pauses** — the tool execution is suspended, waiting for
   approval. Other concurrent tool calls (READ) can continue.
2. **Slack message** — the approval gate sends a Block Kit message to the
   user's DM or the thread where the request originated.
3. **User responds** — clicking Approve or Reject triggers a Slack
   interaction callback.
4. **Resume or deny** — on Approve, the executor runs the command with
   `--write` and returns the result. On Reject, the executor returns a
   denial message that the LLM can reason about.
5. **Timeout** — if the user doesn't respond within 10 minutes (configurable),
   the operation is denied with a timeout message.

### Audit

Approval decisions are logged as part of the `ToolCallDB` record for
every WRITE invocation (see [§8.2](#tool-call-audit-database-toolcalldb)).
Each record captures the approval status, who approved, and how long
the decision took — alongside the full command, response, and execution
metadata. This gives a single audit trail for all tool calls rather than
separate logs for approvals and executions.

---

## 11  Sandbox Policy Changes

The orchestrator sandbox needs network access to the services that nv-tools
calls. Two approaches:

### Approach A: Direct API access (current sandbox)

If nv-tools runs inside the orchestrator sandbox, the sandbox policy must
allow outbound HTTPS to each service endpoint:

```yaml
network_policies:
  # ... existing slack_api, slack_websocket, inference entries ...

  nv_tools_jira:
    name: nv-tools-jira
    endpoints:
      - host: jira.example.com
        port: 443
        protocol: rest
        tls: terminate
        enforcement: enforce
        rules:
          - allow: { method: GET, path: "/**" }
          - allow: { method: POST, path: "/**" }
          - allow: { method: PUT, path: "/**" }
    binaries:
      - { path: /usr/local/bin/python* }
      - { path: /usr/local/bin/nv-tools }

  nv_tools_confluence:
    name: nv-tools-confluence
    endpoints:
      - host: confluence.example.com
        port: 443
        protocol: rest
        tls: terminate
        enforcement: enforce
    binaries:
      - { path: /usr/local/bin/python* }
      - { path: /usr/local/bin/nv-tools }

  nv_tools_gitlab:
    name: nv-tools-gitlab
    endpoints:
      - host: gitlab.example.com
        port: 443
        protocol: rest
        tls: terminate
        enforcement: enforce
    binaries:
      - { path: /usr/local/bin/python* }
      - { path: /usr/local/bin/nv-tools }

  nv_tools_gerrit:
    name: nv-tools-gerrit
    endpoints:
      - host: gerrit.example.com
        port: 443
        protocol: rest
        tls: terminate
        enforcement: enforce
    binaries:
      - { path: /usr/local/bin/python* }
      - { path: /usr/local/bin/nv-tools }
```

### Approach B: nv-tools as a sidecar service

For cleaner separation, nv-tools runs as a separate lightweight service
(outside the sandbox) and the orchestrator calls it via a local HTTP API,
similar to the `inference.local` pattern:

```
Sandbox → tools.local:8877 → nv-tools HTTP wrapper → external APIs
```

This keeps the sandbox policy minimal (only `tools.local` access) and
centralizes API credentials outside the sandbox. This is the preferred
approach for production but adds deployment complexity. Defer to a later
milestone.

**Recommendation:** Start with Approach A (direct access inside sandbox)
for simplicity. Migrate to Approach B when the service count grows or
credential management becomes a concern.

---

## 12  Implementation Plan

### Phase 1 — Stub package

Create `stubs/nv_tools/` with:

- Typer CLI mirroring the real nv-tools interface for the core services
  (jira, confluence, slack, gitlab, gerrit)
- Canned JSON responses for common commands
- `--help` output matching the real tool
- `--write` safety gate
- Installable as `nv-tools` console script

### Phase 2 — ToolCallDB

Implement the orchestrator-side audit database:

- `ToolCallRow` SQLAlchemy ORM model (matching `nmb.audit.models` pattern)
- Alembic migration with the `tool_calls` table, indexes, and FTS5
- `ToolCallDB` class following the `nmb.audit.AuditDB` pattern
  (async SQLAlchemy + aiosqlite, WAL mode, configurable payload persistence)
- `log_call()`, `query()`, `export_jsonl()` methods
- Tests mirroring `test_nmb_audit.py`

### Phase 3 — Orchestrator tool registration

- Register `nv_tools_help` and `nv_tools_execute` in the tool registry
- Add the `nv_tools` toolset to the `orchestrator` platform preset
- Inject the Level 0 service catalog into the system prompt
- Implement subprocess execution with timeout and output parsing
- Wire `ToolCallDB.log_call()` into the execution pipeline

### Phase 4 — Write approval gate

- Implement the Slack-based approval flow (Block Kit message, interaction
  callback, timeout)
- Integrate with the existing `ApprovalGate` from the orchestrator
- Add the static WRITE command classifier
- Approval decisions recorded in `ToolCallDB` (no separate approval log)

### Phase 5 — Docker + policy updates

- Update `Dockerfile.orchestrator` with the `NV_TOOLS_SOURCE` build arg
- Add nv-tools service endpoints to `policies/orchestrator.yaml`
- Add Makefile targets for building with real vs. stub nv-tools
- Update `.env.example` with nv-tools configuration variables
- Implement host-side token server for OAuth services (§6.2, Strategy E):
  - Token server script (`nv-tools-token-server.py`) with `/token` and
    `/health` endpoints
  - Sandbox wrapper script that fetches tokens on demand
  - Token server network policy template
  - Read-only network policy defaults (GET only) for each service
  - `make setup-nv-tools-oauth` target for bootstrap and re-deploy

### Phase 6 — Multi-step reasoning tests

- End-to-end tests with the stub: Jira triage, cross-service research,
  write-with-approval
- Verify output truncation works correctly
- Verify concurrent READ tool calls execute in parallel
- Verify WRITE operations correctly pause for approval
- Verify `ToolCallDB` records are complete and queryable

### Deferred

- Approach B sidecar deployment (§11)
- nv-tools write rate limiting integration
- Advanced risk classification (LLM-based Stage 2 from
  [Orchestrator Design §7](orchestrator_design.md#7--permission--approval-system))

---

## 13  Open Questions

| # | Question | Notes |
|---|----------|-------|
| 1 | How many services should the stub cover initially? | Recommendation: start with jira, confluence, slack, gitlab, gerrit. Add more as use-cases demand. |
| 2 | Should the stub return randomized or fixed canned data? | Fixed is more predictable for tests; randomized is more realistic for LLM reasoning. Consider: fixed by default, randomized via `--randomize` flag. |
| 3 | ~~How should nv-tools credentials be injected into the sandbox?~~ | **Resolved in §6.** Host-side credential server (Strategy E) — serves all credentials via `GET /credentials` over HTTP. For testing, an SSH reverse tunnel (`-R 9100:localhost:9100`) bridges the host server into the sandbox. The `generic` provider approach was attempted and failed (see §6.3 Lessons Learned). |
| 4 | Should the LLM be allowed to construct `--write` commands, or should the orchestrator always strip `--write` and re-add it only after approval? | Stripping is safer — prevents the LLM from accidentally bypassing the gate. |
| 5 | What is the right subprocess timeout per service? | Jira/Confluence: 30s. GitLab (large diffs): 60s. Slack (search can be slow): 45s. Make configurable. |
| 6 | Should we also expose nv-tools to sub-agents (coding, review), or only to the orchestrator? | Start with orchestrator only. Sub-agents that need service access (e.g., review agent posting to Gerrit) can request it via NMB → orchestrator. |

---

### Sources

- nv-tools — source CLI (repo URL configured via `NV_TOOLS_REPO_URL` in `.env`)
- [Orchestrator Design](orchestrator_design.md) — agent loop, tool system, permission model
- [Design Doc](design.md) — project goals, design principles
- [Claude Code Deep Dive](deep_dives/claude_code_deep_dive.md) — tool system patterns, progressive disclosure
- [Hermes Deep Dive](deep_dives/hermes_deep_dive.md) — skill system, progressive disclosure
- [OpenShell OAuth2 discussion](https://nvidia.slack.com/archives/C0AE9P50JVA/p1775739593096759) — Slack thread on OAuth2 credential patterns in sandboxes, L7 proxy limitation, v2 Providers roadmap
- [gogcli-skill PR](https://github.com/brevdev/nemoclaw-demos/pull/2) — reference implementation of host-side token server pattern for Google Workspace in NemoClaw
