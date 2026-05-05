---
name: windows-multi-agent-orchestrator
description: Build or extend a Windows 10/11 multi-agent control plane that routes work across OpenClaw, Hermes Agent, Claude Code, Codex, Ollama, and similar local or CLI-based agent platforms. Use when designing adapters, capability routing, quick or deep health probes, cost-aware local-vs-remote delegation, or GitHub-packaged orchestration skills. Not for macOS.
---

# Windows Multi-Agent Orchestrator

Use this skill when the task is to make one leader agent coordinate other agent platforms, models, and runtimes on Windows 10 or Windows 11.

The leader should decide, split work, route subtasks, reduce token burn, and validate outputs. It should not blindly do all work itself.

## Outcome

Produce one or more of these:

- worker manifests
- routing policy manifests
- adapter code
- quick and deep ping strategy
- routing rules
- failure and reroute policy
- Windows 10 or 11 runtime guidance
- GitHub-ready skill packaging for orchestration

## Workflow

1. Inventory the available platforms.
   Record framework name, backend, target model or agent, write capability, transport, health command, quick ping path, deep ping path, and invocation path.
2. Normalize each platform into a worker contract.
   Use the minimum fields listed below.
3. Write the routing policy before wiring the runtime.
   Use `assets/routing-policy.template.json` to define planner, builder, reviewer, fallback, and budget-aware delegation lanes.
4. Route by role, not by brand.
   Planning and architecture go to the best planner. Coding and testing go to write-capable builders. Review goes to strong read-only judges. Cheap repetitive work goes to local models first.
5. Pick the transport family.
   Use `local_llm` for local model APIs such as Ollama. Use `agent_cli` for CLI-based agents such as OpenClaw, Hermes-over-OpenClaw, Claude Code, Codex, Aider, Goose, or similar wrappers.
   Use `extension_bridge` for IDE-extension-backed agents or editor panes that need a local bridge. Use `service_mesh` for HTTP, daemon, or queue-backed agent services.
6. Separate architecture families early.
   CLI agents fit `agent_cli`. Local model servers fit `local_llm`. IDE extensions, daemons, websockets, and service meshes need dedicated adapters instead of being forced into one-shot CLI wrappers.
7. Build a quick and deep control plane.
   Quick ping should avoid heavy generation whenever possible. Deep ping must prove a real roundtrip.
8. Apply Windows-only runtime rules.
   Read `references/windows-runtime.md` before changing subprocess, permissions, or cancellation logic.
9. Validate the worker manifest.
   Run `python scripts/validate_worker_manifest.py <path-to-json>`.
10. Validate the routing policy.
    Run `python scripts/validate_routing_policy.py <path-to-json>`.
11. Package for GitHub.
   Keep the skill folder self-contained. Do not add macOS guidance. Do not add extra user-facing docs outside the skill structure.

## Required Worker Contract

Each platform worker should define at least:

- `worker_id`
- `worker_type`
- `display_name`
- `role`
- `capabilities`
- `cost_tier`
- `quality_tier`
- `speed_tier`
- `local_only`
- `api_cost`
- `supports_workspace_write`
- `platform.framework_label`
- `platform.backend_label`
- `platform.target_label`
- `platform.mode_family`

For `external_cli_agent`, also require:

- `config.binary`
- `config.invoke_command`
- `config.timeout`
- `config.quick_timeout`
- `config.deep_timeout`

Recommended when available:

- `config.health_command`
- `config.quick_ping_command`
- `config.deep_ping_command`
- `config.response_parser`
- `config.response_text_field`
- `config.prompt_transport`

For `extension_bridge_agent`, also require:

- `config.binary`
- `config.bridge_command`
- `config.timeout`
- `config.quick_timeout`
- `config.deep_timeout`

For `service_mesh_agent`, also require:

- `config.base_url`
- `config.invoke_path`
- `config.timeout`
- `config.quick_timeout`
- `config.deep_timeout`

Recommended when available:

- `config.health_path`
- `config.quick_ping_path`
- `config.deep_ping_path`
- `config.headers`
- `config.auth_env`
- `config.request_template`

## Required Routing Policy

Each leader deployment should also define:

- `routing_policy.planner_roles`
- `routing_policy.builder_roles`
- `routing_policy.reviewer_roles`
- `routing_policy.fallback_lanes`
- `routing_policy.budget_modes`
- `routing_policy.quick_ping_strategy`
- `routing_policy.deep_ping_strategy`

Use `assets/routing-policy.template.json` as the baseline and validate it with `scripts/validate_routing_policy.py`.

## Framework Notes

- For OpenClaw, Hermes Agent, Claude Code, Codex, Ollama, and generic CLI adapters, read `references/framework-compatibility.md`.
- For Aider, Goose, Gemini CLI, IDE-extension agents, and service-style agent meshes, also read `references/framework-compatibility.md`.
- For Windows 10 and 11 process behavior, hidden terminals, env lookup, and cancellation, read `references/windows-runtime.md`.
- For a starter worker manifest, use `assets/external-cli-agent.template.json`.
- For a starter extension bridge manifest, adapt `assets/extension-bridge-agent.template.json`.
- For a starter service mesh manifest, adapt `assets/service-mesh-agent.template.json`.
- For a starter routing policy, use `assets/routing-policy.template.json`.
- For a popular-platform starter pack, use `assets/popular-agent-workers.example.json`.

## Guardrails

- Default to read-only or planning mode unless a subtask explicitly needs writes.
- Prefer local planning or review lanes for cheap repetitive work.
- Do not let every framework act as both planner and builder by default. Separate planner, builder, reviewer, and fallback roles.
- Hide console windows on Windows and avoid spawning visible PowerShell or terminal windows during routine orchestration.
- Cache quick ping results and prefer local-state probes before expensive CLI status calls.
- Treat Hermes running through OpenClaw as an agent identity unless the environment exposes a separate runtime.
- Buffer audit writes and batch health checks if a CLI platform is blocking.
- If a platform needs long-lived sockets, daemons, or streaming sessions, prefer a dedicated worker type instead of overloading a CLI adapter.
- Keep public skill recommendations narrow: prefer domain-relevant skills over broad marketplace spam.
- Require at least one explicit reviewer lane and one fallback lane before calling the control plane production-ready.

## Deliverables

When implementing or revising a platform, return:

- the worker manifest or code patch
- the routing policy
- the quick and deep ping design
- the read and write permission model
- the routing role the platform should play
- the fallback and reroute behavior
- the validation commands that were run
