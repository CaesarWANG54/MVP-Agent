[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Version
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$distRoot = Join-Path $repoRoot "dist"
$stageRoot = Join-Path $distRoot "stage"
$packageRoot = Join-Path $stageRoot "mvp-agent"
$zipPath = Join-Path $distRoot ("mvp-agent-{0}.zip" -f $Version)
$hashPath = "$zipPath.sha256"
$manifestPath = Join-Path $distRoot "release-manifest.json"

powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot "validate-repo.ps1")

if (Test-Path $stageRoot) {
    Remove-Item -Recurse -Force $stageRoot
}
New-Item -ItemType Directory -Force -Path $packageRoot | Out-Null

$excludeNames = @(".git", "dist", "__pycache__")
Get-ChildItem -Force $repoRoot | Where-Object { $excludeNames -notcontains $_.Name } | ForEach-Object {
    Copy-Item -Recurse -Force $_.FullName (Join-Path $packageRoot $_.Name)
}

Get-ChildItem -Recurse -Directory $packageRoot | Where-Object { $_.Name -eq "__pycache__" } | ForEach-Object {
    Remove-Item -Recurse -Force $_.FullName
}
Get-ChildItem -Recurse -File $packageRoot -Include *.pyc,*.pyo | Remove-Item -Force

if (Test-Path $zipPath) {
    Remove-Item -Force $zipPath
}
Compress-Archive -Path (Join-Path $packageRoot "*") -DestinationPath $zipPath -Force

$hash = Get-FileHash -Algorithm SHA256 $zipPath
$hash.Hash | Set-Content -Encoding ASCII $hashPath

$manifest = [ordered]@{
    package = "mvp-agent"
    version = $Version
    zip = (Split-Path $zipPath -Leaf)
    sha256 = (Split-Path $hashPath -Leaf)
}
$manifest | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $manifestPath

Write-Host "Release packaged: $zipPath"
