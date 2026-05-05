# Windows Runtime Notes

This skill only targets Windows 10 and Windows 11.

## Process execution

- Prefer `cmd` or native executables over opening interactive PowerShell windows.
- Resolve `.cmd`, `.bat`, and native binaries with `shutil.which` or an equivalent lookup.
- Use hidden-window subprocess flags so routine orchestration does not pop visible terminals.
- If a process tree must be cancelled, prefer `taskkill /PID <pid> /T /F`.

## Environment handling

- Read from the current process environment first.
- When a platform stores required variables in user-scoped Windows environment settings, merge them into the child process environment.
- Keep platform-specific secrets out of logs.

## Permissions

- Prefer read-only or planning mode by default.
- Only grant write-capable execution to workers that must patch the workspace.
- Do not default to full-disk access.
- Separate planner, builder, and auditor roles so one platform does not silently become over-privileged.

## Console noise

- Hide background CLI windows.
- Avoid spawning raw PowerShell consoles for quick ping, health checks, or metadata refresh.
- Batch health checks and buffer audit writes so the control plane stays responsive.

## Quick and deep ping behavior

- Quick ping should be cheap and mostly non-generative.
- Deep ping should prove a real roundtrip but stay bounded by a short timeout.
- Cache quick ping results for short bursts so UI refreshes do not repeatedly hit every platform.

## Windows 10 and 11 compatibility baseline

The orchestration layer should assume:

- path quoting must be strict
- `.cmd` wrappers are common
- user-scoped environment variables may differ from the current shell
- process tree cleanup is required for reliable cancellation

Do not add macOS-specific shell guidance to this skill.
