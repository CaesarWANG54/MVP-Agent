[CmdletBinding()]
param(
    [switch]$LiveQuickPing
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$workerValidator = Join-Path $repoRoot "skills\windows-multi-agent-orchestrator\scripts\validate_worker_manifest.py"
$routingValidator = Join-Path $repoRoot "skills\windows-multi-agent-orchestrator\scripts\validate_routing_policy.py"
$externalAgents = Join-Path $repoRoot "examples\external-agents.example.json"
$routingPolicy = Join-Path $repoRoot "examples\routing-policy.example.json"

function Invoke-Step {
    param([string]$Name, [scriptblock]$Action)
    Write-Host "==> $Name"
    & $Action
}

Invoke-Step "Repository validation" {
    powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "validate-repo.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Repository validation failed."
    }
}

Invoke-Step "External agent example validation" {
    python $workerValidator $externalAgents
    if ($LASTEXITCODE -ne 0) {
        throw "External agent example validation failed."
    }
}

Invoke-Step "Routing policy validation" {
    python $routingValidator $routingPolicy
    if ($LASTEXITCODE -ne 0) {
        throw "Routing policy validation failed."
    }
}

if ($LiveQuickPing) {
    Invoke-Step "MVP Agent quick ping" {
        Push-Location $repoRoot
        try {
            python -m mvp ping --mode quick
            if ($LASTEXITCODE -ne 0) {
                throw "MVP Agent quick ping failed."
            }
        }
        finally {
            Pop-Location
        }
    }
}

Write-Host "MVP Agent E2E smoke completed."
