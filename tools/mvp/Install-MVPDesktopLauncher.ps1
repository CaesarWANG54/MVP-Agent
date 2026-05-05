[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$desktop = [Environment]::GetFolderPath("Desktop")
$guiCmdPath = Join-Path $desktop "MVP Agent.cmd"
$cliCmdPath = Join-Path $desktop "MVP Agent CLI.cmd"
$guiVbsPath = Join-Path $desktop "MVP Agent.vbs"
$shortcutPath = Join-Path $desktop "MVP Agent.lnk"
$pythonExe = (Get-Command python -ErrorAction Stop).Source
$pythonwExe = Join-Path (Split-Path -Parent $pythonExe) "pythonw.exe"
if (-not (Test-Path $pythonwExe)) {
    $pythonwExe = $pythonExe
}
$configPath = Join-Path $repoRoot "mvp.config.json"
$iconPath = Join-Path $repoRoot "mvp\assets\mvp_icon.ico"

$guiCmd = @"
@echo off
title MVP Agent Console
cd /d "$repoRoot"
start "" "$pythonwExe" -m mvp --config "$configPath" app
"@

$cliCmd = @"
@echo off
title MVP Agent CLI
cd /d "$repoRoot"
"$pythonExe" -m mvp shell
pause
"@

$guiVbs = @"
Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = "$repoRoot"
shell.Run """" & "$pythonwExe" & """ -m mvp --config """ & "$configPath" & """ app", 0
"@

Set-Content -LiteralPath $guiCmdPath -Value $guiCmd -Encoding ASCII
Set-Content -LiteralPath $cliCmdPath -Value $cliCmd -Encoding ASCII
Set-Content -LiteralPath $guiVbsPath -Value $guiVbs -Encoding ASCII

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonwExe
$shortcut.Arguments = "-m mvp --config ""$configPath"" app"
$shortcut.WorkingDirectory = $repoRoot
$shortcut.Description = "MVP Agent desktop launcher"
if (Test-Path $iconPath) {
    $shortcut.IconLocation = "${iconPath},0"
}
$shortcut.Save()

Write-Output "Created: $guiCmdPath"
Write-Output "Created: $cliCmdPath"
Write-Output "Created: $guiVbsPath"
Write-Output "Created: $shortcutPath"
