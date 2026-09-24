$ErrorActionPreference = "Stop"

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Startup = [Environment]::GetFolderPath("Startup")
$Target = Join-Path $env:WINDIR "System32\wscript.exe"
$HiddenLauncher = Join-Path $ProjectRoot "scripts\start_tray_hidden.vbs"
$ShortcutPath = Join-Path $Startup "Discord Music Bot.lnk"

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Target
$Shortcut.Arguments = "`"$HiddenLauncher`""
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.WindowStyle = 7
$Shortcut.Description = "Start Discord Music Bot tray on Windows login"
$Shortcut.Save()

Write-Host "Startup shortcut created:"
Write-Host $ShortcutPath
