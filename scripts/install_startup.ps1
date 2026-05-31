$ErrorActionPreference = "Stop"

$ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$Startup = [Environment]::GetFolderPath("Startup")
$Target = Join-Path $ProjectRoot "scripts\start_bot.bat"
$ShortcutPath = Join-Path $Startup "Discord Music Bot.lnk"

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Target
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.WindowStyle = 7
$Shortcut.Description = "Start Discord Music Bot on Windows login"
$Shortcut.Save()

Write-Host "Startup shortcut created:"
Write-Host $ShortcutPath
