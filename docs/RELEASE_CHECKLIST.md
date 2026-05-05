# Release Checklist

Use this before publishing `MVP Agent`.

## Required

- Run `powershell -ExecutionPolicy Bypass -File .\scripts\validate-repo.ps1`
- Run `powershell -ExecutionPolicy Bypass -File .\scripts\run-e2e-smoke.ps1`
- Run `powershell -ExecutionPolicy Bypass -File .\scripts\package-release.ps1 -Version <version>`
- Confirm only the bundled `mvp-agent-pet` is active in the desktop product
- Confirm `examples/external-agents.example.json` and `examples/routing-policy.example.json` still match the shipped control-plane model

## Recommended

- Run `powershell -ExecutionPolicy Bypass -File .\scripts\run-e2e-smoke.ps1 -LiveQuickPing`
- Capture fresh screenshots listed in `docs/SCREENSHOTS.md`
- Check release artifacts in `dist/`
