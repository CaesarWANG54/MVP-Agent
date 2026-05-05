# MVP Ping Modes

`MVP` now exposes two roundtrip validation modes:

## Quick Ping

Command:

```powershell
python -m mvp ping --mode quick
```

Behavior:

- `OpenClaw/Hermes`: checks configured model status and auth readiness.
- `Ollama`: checks model reachability and install state.
- `Codex`: checks CLI, target model, and sandbox readiness.

Use this for everyday readiness checks when you want low overhead.

## Deep Ping

Command:

```powershell
python -m mvp ping --mode deep
```

Behavior:

- Sends a real delegated request through each worker path.
- Verifies the end-to-end reply flow instead of only control-plane readiness.

Use this before important runs or after changing framework/model wiring.

## Design Note

`quick ping` audit rows are recorded with a dedicated stage and are excluded from routing latency metrics, so health checks do not distort real task-routing decisions.
