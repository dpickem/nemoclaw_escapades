# NemoClaw Escapades

An exploration of NemoClaw + OpenShell to build an always-on agentic system —
modeled after OpenClaw and Hermes — that performs useful work around the clock.

Every development milestone is documented as a blog post.

## Documentation

- [Design Document](docs/design.md) — project goals, architecture, milestones,
  and open questions.

### System Designs

- [Training Flywheel Design](docs/training_flywheel_deep_dive.md) — turning
  daily agent interactions into SFT and RL training data; two-layer trace
  capture (per-sandbox + NMB audit log), quality filtering, DPO preference
  pairs from review loops, Nemotron fine-tuning pipeline, comparison with
  Cursor's real-time RL.
- [NemoClaw Message Bus (NMB) Design](docs/nmb_design.md) — real-time
  inter-sandbox messaging: WebSocket broker, wire protocol, client library,
  security model, multi-host deployment, failure modes.

### Deep Dives

- [Hermes Agent Deep Dive](docs/deep_dives/hermes_deep_dive.md) — architecture
  analysis, self-learning loop, memory system, skills, sub-agent delegation,
  and applicability to this project.
- [NemoClaw Deep Dive](docs/deep_dives/nemoclaw_deep_dive.md) — plugin/blueprint
  architecture, sandbox lifecycle, inference routing, network policy, deployment
  modes, and CLI reference.
- [OpenShell Deep Dive](docs/deep_dives/openshell_deep_dive.md) — core
  components (gateway, sandbox, policy engine, privacy router), defense-in-depth
  enforcement, policy schema, community sandboxes, and IDE integration.
- [OpenClaw Deep Dive](docs/deep_dives/openclaw_deep_dive.md) — gateway
  architecture, sandboxing, multi-agent routing, channels, skills, and
  applicability to this project.
- [Hermes vs OpenClaw Comparison](docs/deep_dives/hermes_vs_openclaw_comparison.md) —
  side-by-side comparison of architecture, skills, memory, sandboxing,
  self-learning, and per-milestone lift strategy.
- [Hosting & Infrastructure Deep Dive](docs/deep_dives/hosting_deep_dive.md) —
  NVIDIA Brev, DGX Spark, remote SSH, cost analysis, recommended architecture,
  and where the core agent loop runs.

## Milestones

1. **Foundation** — Slack connector + inference hub + orchestrator
2. **Knowledge Management** — SecondBrain integration
3. **Coding Agent** — Sandboxed code generation via OpenShell
4. **Self-Improvement Loop** — Persistent memory + autonomous skill refinement
5. **Review Agent** — Local collaboration before push
6. **Professional KB** — Note-taking & summarization from Slack/Teams

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
