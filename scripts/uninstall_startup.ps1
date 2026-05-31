$ShortcutPath = Join-Path ([Environment]::GetFolderPath("Startup")) "Discord Music Bot.lnk"

if (Test-Path $ShortcutPath) {
    Remove-Item -LiteralPath $ShortcutPath
    Write-Host "Startup shortcut removed:"
    Write-Host $ShortcutPath
} else {
    Write-Host "Startup shortcut was not found."
}
