# NemoClaw Escapades

An exploration of NemoClaw + OpenShell to build an always-on agentic system —
modeled after OpenClaw and Hermes — that performs useful work around the clock.

Every development milestone is documented as a blog post.

## Quick Start

```bash
cp .env.example .env        # fill in real values
make setup                  # gateway + providers + sandbox (one-time)
make run-local-dev          # start the orchestrator outside a sandbox
```

## Running Tests

```bash
make test                   # unit + component tests (fast)
make test-integration       # multi-sandbox NMB integration tests
make test-all               # everything
make typecheck              # mypy strict
make lint                   # ruff check + format
```

## Documentation

### Architecture & Design

- [Design Document](docs/design.md) — project goals, architecture, milestones,
  and open questions
- [Milestone 1 Design](docs/design_m1.md) — foundation loop: Slack + inference
  hub + orchestrator
- [Orchestrator Design](docs/orchestrator_design.md) — coordinator mode, task
  store, permission model, sub-agent lifecycle

### System Designs

- [NemoClaw Message Bus (NMB)](docs/nmb_design.md) — real-time inter-sandbox
  messaging: WebSocket broker, wire protocol (send/request/reply/pub-sub/stream),
  client library, audit DB with FTS5, security model, multi-host deployment,
  failure modes
- [NMB Integration Tests](docs/nmb_integration_tests_design.md) — multi-sandbox
  test harness: PolicyBroker with per-sandbox egress/ingress/channel/op
  enforcement, IntegrationHarness lifecycle manager, 41 tests across 7 files
- [nv-tools Integration](docs/nv_tools_integration_design.md) — Jira,
  Confluence, Slack, GitLab, Gerrit access from inside OpenShell sandboxes;
  stub for offline testing, write-approval gate, host-side token server for
  OAuth credential isolation
- [Training Flywheel](docs/training_flywheel_deep_dive.md) — turning daily
  agent interactions into SFT and RL training data; two-layer trace capture
  (per-sandbox + NMB audit log), quality filtering, DPO preference pairs from
  review loops, Nemotron fine-tuning pipeline

### Deep Dives

- [Hermes Agent](docs/deep_dives/hermes_deep_dive.md) — architecture analysis,
  self-learning loop, memory system, skills, sub-agent delegation
- [NemoClaw](docs/deep_dives/nemoclaw_deep_dive.md) — plugin/blueprint
  architecture, sandbox lifecycle, inference routing, network policy, deployment
  modes
- [OpenShell](docs/deep_dives/openshell_deep_dive.md) — core components
  (gateway, sandbox, policy engine, privacy router), defense-in-depth
  enforcement, policy schema, community sandboxes
- [OpenClaw](docs/deep_dives/openclaw_deep_dive.md) — gateway architecture,
  sandboxing, multi-agent routing, channels, skills
- [Claude Code](docs/deep_dives/claude_code_deep_dive.md) — tool system,
  coordinator mode, session forking, sub-agents, permission model analysis
- [Hermes vs OpenClaw vs Claude Code](docs/deep_dives/hermes_vs_openclaw_vs_claude_code_comparison.md) —
  side-by-side comparison of architecture, skills, memory, sandboxing,
  self-learning, per-milestone lift strategy
- [Hosting & Infrastructure](docs/deep_dives/hosting_deep_dive.md) — NVIDIA
  Brev, DGX Spark, remote SSH, cost analysis, recommended architecture

### Blog Posts

- [Series Introduction](docs/blog_posts/series_introduction/series_introduction.md) —
  why build agents from scratch, project goals, what to expect
- [M1 — Setting Up NemoClaw](docs/blog_posts/m1/m1_setting_up_nemoclaw.md) —
  local orchestrator + NVIDIA Inference Hub + Slack connector, lessons learned

## Milestones

| # | Milestone | Status | Key Deliverables |
|---|-----------|--------|------------------|
| 1 | **Foundation** | Done | Slack connector, inference hub backend, orchestrator loop, multi-turn history, transcript repair |
| 2 | **Message Bus** | Done | NMB broker + async/sync clients, audit DB with Alembic migrations + FTS5, multi-sandbox integration tests with PolicyBroker harness |
| 3 | **Knowledge Management** | Planned | SecondBrain integration |
| 4 | **Coding Agent** | Planned | Sandboxed code generation via OpenShell |
| 5 | **Self-Improvement Loop** | Planned | Persistent memory + autonomous skill refinement |
| 6 | **Review Agent** | Planned | Local collaboration before push |

## Package Layout

```
src/nemoclaw_escapades/
├── main.py                      # Orchestrator entry point
├── config.py                    # Environment-based configuration
├── orchestrator.py              # Multi-turn agent loop
├── nmb/                         # NemoClaw Message Bus
│   ├── broker.py                # Asyncio WebSocket message router
│   ├── client.py                # Async MessageBus client
│   ├── models.py                # Wire protocol types (Pydantic)
│   ├── sync.py                  # Synchronous wrapper
│   ├── audit/                   # SQLite audit DB (Alembic-managed)
│   └── testing/                 # Integration test infrastructure
│       ├── policy.py            # PolicyBroker, SandboxPolicy
│       └── harness.py           # IntegrationHarness, SandboxHandle
├── connectors/                  # Slack connector
└── backends/                    # Inference hub backend
```

## Related Projects

| Project | Description |
|---------|-------------|
| [NemoClaw](https://github.com/NVIDIA/NemoClaw) | NVIDIA's open-source stack for running OpenClaw with enterprise security |
| [OpenShell](https://github.com/NVIDIA/OpenShell) | NVIDIA's secure runtime for autonomous AI agents (sandbox, policy, inference routing) |
| [OpenClaw](https://github.com/openclaw/openclaw) | Personal AI assistant — reference architecture for multi-channel agentic systems |
| [Hermes Agent](https://github.com/nousresearch/hermes-agent) | Self-improving AI agent by Nous Research — learning loop, skills, memory |
| [SecondBrain](https://github.com/dpickem/project_second_brain) | Personal knowledge management & learning system (own project) |
| [NVIDIA Brev](https://brev.nvidia.com/) | GPU-accelerated cloud platform — recommended hosting for always-on agents |

## License

See [LICENSE](LICENSE).
