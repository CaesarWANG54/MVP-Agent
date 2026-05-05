[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot

function Assert-Exists {
    param([string]$PathValue, [string]$Label)
    if (-not (Test-Path $PathValue)) {
        throw "$Label not found: $PathValue"
    }
}

Assert-Exists (Join-Path $repoRoot "README.md") "README"
Assert-Exists (Join-Path $repoRoot "requirements.txt") "requirements"
Assert-Exists (Join-Path $repoRoot "mvp") "mvp package"
Assert-Exists (Join-Path $repoRoot "tests") "tests"
Assert-Exists (Join-Path $repoRoot "tools\mvp\Install-MVPDesktopLauncher.ps1") "desktop launcher installer"
Assert-Exists (Join-Path $repoRoot "skills\windows-multi-agent-orchestrator\SKILL.md") "leader skill"
Assert-Exists (Join-Path $repoRoot "examples\mvp.config.example.json") "example config"
Assert-Exists (Join-Path $repoRoot "examples\external-agents.example.json") "external agents example"
Assert-Exists (Join-Path $repoRoot "examples\routing-policy.example.json") "routing policy example"
Assert-Exists (Join-Path $repoRoot "mvp\workers\extension_bridge_agent.py") "extension bridge worker"
Assert-Exists (Join-Path $repoRoot "mvp\workers\service_mesh_agent.py") "service mesh worker"
Assert-Exists (Join-Path $repoRoot "skills\windows-multi-agent-orchestrator\assets\extension-bridge-agent.template.json") "extension bridge template"
Assert-Exists (Join-Path $repoRoot "skills\windows-multi-agent-orchestrator\assets\service-mesh-agent.template.json") "service mesh template"
Assert-Exists (Join-Path $repoRoot ".github\workflows\validate.yml") "validate workflow"
Assert-Exists (Join-Path $repoRoot ".github\workflows\release.yml") "release workflow"
Assert-Exists (Join-Path $repoRoot "docs\MVP_PLATFORM.md") "platform doc"
Assert-Exists (Join-Path $repoRoot "docs\MVP_AGENT_COMPAT.md") "compatibility doc"
Assert-Exists (Join-Path $repoRoot "docs\MVP_PING_MODES.md") "ping doc"

Get-ChildItem -Recurse -File $repoRoot -Include *.pyc,*.pyo | Remove-Item -Force
Get-ChildItem -Recurse -Directory $repoRoot | Where-Object { $_.Name -eq "__pycache__" } |
    Sort-Object { $_.FullName.Length } -Descending |
    ForEach-Object {
        try {
            Remove-Item -Recurse -Force $_.FullName -ErrorAction Stop
        }
        catch {
            Write-Warning "Skipping locked cache directory: $($_.FullName)"
        }
    }

Push-Location $repoRoot
try {
    python .\skills\windows-multi-agent-orchestrator\scripts\validate_worker_manifest.py .\examples\external-agents.example.json
    if ($LASTEXITCODE -ne 0) {
        throw "External agent example validation failed."
    }
    python .\skills\windows-multi-agent-orchestrator\scripts\validate_routing_policy.py .\examples\routing-policy.example.json
    if ($LASTEXITCODE -ne 0) {
        throw "Routing policy example validation failed."
    }
    @'
from pathlib import Path
import sys

errors = []
for root in ("mvp", "tests"):
    for path in Path(root).rglob("*.py"):
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except Exception as exc:
            errors.append(f"{path}: {exc}")

if errors:
    print("\n".join(errors))
    raise SystemExit(1)

print("Python syntax check passed.")
'@ | python -
    if ($LASTEXITCODE -ne 0) {
        throw "Python syntax check failed."
    }
    python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        throw "Unit tests failed."
    }
    python -m mvp app --self-test
    if ($LASTEXITCODE -ne 0) {
        throw "Desktop self-test failed."
    }
}
finally {
    Pop-Location
}

Write-Host "MVP Agent repository validation passed."
