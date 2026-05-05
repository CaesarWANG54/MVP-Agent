[CmdletBinding()]
param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$publishRoot = Split-Path -Parent $repoRoot
$workspaceRoot = Split-Path -Parent $publishRoot

function Copy-Tree {
    param([string]$SourcePath, [string]$TargetPath)
    if (-not (Test-Path $SourcePath)) {
        throw "Source path not found: $SourcePath"
    }
    if (Test-Path $TargetPath) {
        Remove-Item -Recurse -Force $TargetPath
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $TargetPath) | Out-Null
    Copy-Item -Recurse -Force $SourcePath $TargetPath
}

function Copy-FileSafe {
    param([string]$SourcePath, [string]$TargetPath)
    if (-not (Test-Path $SourcePath)) {
        throw "Source file not found: $SourcePath"
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $TargetPath) | Out-Null
    Copy-Item -Force $SourcePath $TargetPath
}

if ($Clean) {
    foreach ($name in @("mvp", "tests", "tools", "skills")) {
        $path = Join-Path $repoRoot $name
        if (Test-Path $path) {
            Remove-Item -Recurse -Force $path
        }
    }
}

Copy-Tree (Join-Path $workspaceRoot "mvp") (Join-Path $repoRoot "mvp")
Copy-Tree (Join-Path $workspaceRoot "tests") (Join-Path $repoRoot "tests")
Copy-Tree (Join-Path $workspaceRoot "tools\mvp") (Join-Path $repoRoot "tools\mvp")
Copy-Tree (Join-Path $workspaceRoot "skills\windows-multi-agent-orchestrator") (Join-Path $repoRoot "skills\windows-multi-agent-orchestrator")

Copy-FileSafe (Join-Path $workspaceRoot "docs\MVP_PLATFORM.md") (Join-Path $repoRoot "docs\MVP_PLATFORM.md")
Copy-FileSafe (Join-Path $workspaceRoot "docs\MVP_AGENT_COMPAT.md") (Join-Path $repoRoot "docs\MVP_AGENT_COMPAT.md")
Copy-FileSafe (Join-Path $workspaceRoot "docs\MVP_PING_MODES.md") (Join-Path $repoRoot "docs\MVP_PING_MODES.md")
Copy-FileSafe (Join-Path $workspaceRoot "examples\mvp.external_agents.example.json") (Join-Path $repoRoot "examples\external-agents.example.json")

Get-ChildItem -Recurse -Directory $repoRoot | Where-Object { $_.Name -eq "__pycache__" } | ForEach-Object {
    Remove-Item -Recurse -Force $_.FullName
}
Get-ChildItem -Recurse -File $repoRoot -Include *.pyc,*.pyo | Remove-Item -Force

Write-Host "Source sync complete."
