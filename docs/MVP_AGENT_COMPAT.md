# MVP Agent Compatibility Foundation

`MVP Agent` now has a broader compatibility base for agent platforms, not just hardcoded support for `Ollama`, `OpenClaw/Hermes`, `Claude Code`, and `Codex`.

## What changed

- Added a reusable CLI worker family: `external_cli_agent`
- Added an IDE-extension bridge worker family: `extension_bridge_agent`
- Added an HTTP or daemon worker family: `service_mesh_agent`
- Unified platform labels, target labels, backend labels, and permission descriptions
- Unified quick/deep mode routing through `platform.mode_family`

This means a new platform can now be integrated in three layers:

1. `config`
   add a worker entry
2. `platform`
   define how MVP should describe and reason about that worker
3. `worker_type`
   choose the transport family that best matches the real runtime

## Worker families

### `external_cli_agent`

Use this when the platform is primarily driven through one-shot CLI commands.

Typical use cases:

- Aider
- Goose
- Gemini CLI
- internal team CLI wrappers
- future local or remote shells with text or JSON output

Required config:

- `config.binary`
- `config.invoke_command`

Recommended config:

- `config.health_command`
- `config.quick_ping_command`
- `config.deep_ping_command`
- `config.response_parser`
- `config.response_text_field`
- `config.prompt_transport`

### `extension_bridge_agent`

Use this when the real platform lives inside an editor, IDE extension, or local helper process and should not be squeezed into a plain CLI abstraction.

Typical use cases:

- Cline-like bridge processes
- Roo Code bridge daemons
- Cursor-sidecar or editor-hosted agent bridges

Required config:

- `config.binary`
- `config.bridge_command`

Recommended config:

- `config.health_command`
- `config.quick_ping_command`
- `config.deep_ping_command`
- `config.response_parser`
- `config.response_text_field`
- `config.prompt_transport`

### `service_mesh_agent`

Use this when the platform is exposed through HTTP, a daemon, or another service boundary.

Typical use cases:

- LangGraph service runners
- CrewAI task APIs
- AutoGen orchestration daemons
- internal multi-agent HTTP control planes

Required config:

- `config.base_url`
- `config.invoke_path`

Recommended config:

- `config.health_path`
- `config.quick_ping_path`
- `config.deep_ping_path`
- `config.headers`
- `config.auth_env`
- `config.request_template`
- `config.response_parser`
- `config.response_text_field`

## Mode families

`platform.mode_family` tells `MVP Agent` how to choose quick/deep lanes.

- `local_llm`
  for local model APIs such as Ollama
- `agent_cli`
  for CLI-native agent platforms
- `extension_bridge`
  for local editor or extension bridge lanes
- `service_mesh`
  for HTTP, daemon, or service-backed lanes
- `default`
  fallback behavior

## Template variables

These placeholders can be used in CLI command arrays or request templates:

- `{prompt}`
- `{mode}`
- `{allow_write}`
- `{write_mode}`
- `{worker_id}`
- `{display_name}`
- `{workspace_root}`
- `{model}`

## Prompt transport

Supported CLI prompt transports:

- `prompt_transport: "stdin"`
  MVP sends the prompt to stdin
- `prompt_transport: "inline"`
  MVP expects the prompt to be embedded into command arguments via `{prompt}`

## Response parsing

Supported parsers:

- `response_parser: "text"`
  MVP treats stdout, stderr, or HTTP body as plain text
- `response_parser: "json_field"`
  MVP extracts one field from a JSON response, usually `result`

## Recommended integration path

When adding a new platform:

1. Get `health` working first
2. Add a cheap `quick ping`
3. Add a real `deep ping`
4. Wire the invoke path
5. Mark the right `mode_family`
6. Set `supports_workspace_write` honestly
7. Tune routing with capabilities and quality/speed/cost tiers

## Example files

See:

- [examples/mvp.external_agents.example.json](/C:/Users/CaesarWang/Documents/New%20project/examples/mvp.external_agents.example.json)
- [skills/windows-multi-agent-orchestrator/assets/extension-bridge-agent.template.json](/C:/Users/CaesarWang/Documents/New%20project/skills/windows-multi-agent-orchestrator/assets/extension-bridge-agent.template.json)
- [skills/windows-multi-agent-orchestrator/assets/service-mesh-agent.template.json](/C:/Users/CaesarWang/Documents/New%20project/skills/windows-multi-agent-orchestrator/assets/service-mesh-agent.template.json)
- [skills/windows-multi-agent-orchestrator/assets/popular-agent-workers.example.json](/C:/Users/CaesarWang/Documents/New%20project/skills/windows-multi-agent-orchestrator/assets/popular-agent-workers.example.json)

## Current limitation

The new foundations are ready for:

- CLI agents
- IDE bridge lanes
- HTTP and daemon-backed agent services

Still worth improving later:

- true streaming transports for non-CLI workers
- websocket-native worker types
- queue-backed cancellation and retry semantics for long-lived service meshes
