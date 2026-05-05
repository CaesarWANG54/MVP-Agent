# Framework Compatibility

This skill is Windows-only. It is written for Windows 10 and Windows 11.

## Preferred platform mapping

| Platform | Recommended MVP mapping | Mode family | Notes |
| --- | --- | --- | --- |
| OpenClaw | Dedicated `openclaw_agent` worker when available | `agent_cli` | Prefer local-state quick ping before heavy status calls. |
| Hermes Agent | Usually a named OpenClaw agent identity unless a separate runtime exists | `agent_cli` | Treat Hermes as a role-specialized planner or architect by default. |
| Claude Code | Dedicated `claude_cli` worker when available | `agent_cli` | Good for planning, code review, and higher-end synthesis. |
| Codex | Dedicated `codex_cli` worker when available | `agent_cli` | Strong builder or auditor depending on write permissions. |
| Ollama | Dedicated `ollama_api` worker | `local_llm` | Best for cheap planning, local review, and repetitive low-risk work. |
| Aider / Goose / Gemini CLI / similar CLIs | `external_cli_agent` | `agent_cli` | Use a config-driven adapter unless a native worker exists. |
| Cline / Roo Code / Cursor agent panes | Dedicated `extension_bridge_agent` | `extension_bridge` | Do not force IDE extensions into one-shot CLI wrappers. Build a persistent bridge. |
| LangGraph / CrewAI / AutoGen / similar service meshes | Dedicated `service_mesh_agent` | `service_mesh` | Prefer a service transport with health, queue, and cancellation primitives. |
| Open WebUI / LM Studio / similar local servers | Dedicated HTTP or local-LLM worker | `local_llm` | Good fit when the platform exposes a model API instead of an agent CLI. |

## Role guidance

- `planning`, `architecture`, `requirements`:
  prefer Hermes, Claude Code, Codex architect lanes, or other strong planning agents.
- `coding`, `testing`:
  prefer write-capable builders such as OpenClaw coder, Codex builder, Aider-like agents, or similar.
- `review`, `qa`, `security`:
  prefer strong read-only auditors.
- `triage`, `summarization`, cheap repetitive work:
  prefer Ollama or another local lane first.

## OpenClaw and Hermes

Use a dedicated adapter when the environment already exposes:

- agent identity
- expected model
- quick ping path
- deep ping path
- local-state files or cached session state

Preferred quick ping strategy:

1. read local config or auth state
2. confirm agent target model
3. confirm auth is usable
4. only fall back to a heavy CLI status call if local state is missing

Preferred deep ping strategy:

1. send a compact JSON-only prompt
2. verify visible assistant text exists
3. capture access mode and target model

## Claude Code and Codex

Preferred quick ping:

- version check
- model and sandbox or permission-mode summary

Preferred deep ping:

- short JSON-only request
- no session persistence unless needed

## Ollama

Preferred quick ping:

- model installed
- local server reachable

Preferred deep ping:

- small deterministic JSON task
- separate quick and deep token budgets

## Generic external CLI agents

Use `external_cli_agent` when:

- the platform is mainly controlled by one CLI entrypoint
- prompt input can be sent inline or via stdin
- responses are text or JSON
- Windows process cancellation can be handled at the subprocess level

Do not force `external_cli_agent` when:

- the platform requires websocket streaming
- the platform requires persistent daemon sessions
- the platform has a richer native transport than one-shot CLI execution

## Extension bridges

Use `extension_bridge_agent` when:

- the agent primarily lives inside an IDE extension or editor pane
- a local bridge command can relay prompts and responses
- quick ping can stay local and cheap
- write permissions should stay bounded by the bridge contract

Preferred examples:

- Cline
- Roo Code
- Cursor agent panes

## Service meshes

Use `service_mesh_agent` when:

- the agent exposes an HTTP or daemon API
- the platform already has service-level health, auth, and cancellation hooks
- one-shot subprocess execution would throw away valuable session or queue behavior

Preferred examples:

- LangGraph services
- CrewAI daemons
- AutoGen services
- internal multi-agent APIs

## Popular compatibility recipes

- `Aider`:
  treat as a write-capable builder or patch lane. Use `agent_cli` and keep a separate read-only reviewer.
- `Goose`:
  good planner or builder candidate when a structured CLI contract is available.
- `Gemini CLI`:
  good strategist or reviewer candidate when the local environment exposes a stable CLI.
- `Cline`, `Roo Code`, `Cursor`:
  treat as extension-driven architectures. Plan an extension bridge or message relay instead of pretending they are normal CLIs.
- `LangGraph`, `CrewAI`, `AutoGen`:
  treat as orchestrated service architectures. Prefer queue-backed or HTTP-backed workers, not subprocess-per-task adapters.
- marketplace or public `skills` ecosystems:
  gate them through task relevance, capability overlap, and cost policy before exposing them to the leader.
