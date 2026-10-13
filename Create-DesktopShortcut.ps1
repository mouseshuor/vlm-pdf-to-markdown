param([switch]$Force)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = Join-Path $taskRoot '.venv/Scripts/pythonw.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskCommand = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $taskCommand) { throw '找不到 pythonw.exe。请先按 README 创建 Python 环境。' }
    $taskPython = $taskCommand.Source
}
$taskDesktop = [Environment]::GetFolderPath('Desktop')
$taskShortcutPath = Join-Path $taskDesktop 'VLM PDF转MD.lnk'
if ((Test-Path -LiteralPath $taskShortcutPath) -and -not $Force) {
    throw '桌面已有同名快捷方式。确认需要替换时，显式传入 -Force。'
}
$taskShell = New-Object -ComObject WScript.Shell
$taskLink = $taskShell.CreateShortcut($taskShortcutPath)
$taskLink.TargetPath = $taskPython
$taskLink.Arguments = '"' + (Join-Path $taskRoot 'launcher.py') + '"'
$taskLink.WorkingDirectory = $taskRoot
$taskLink.IconLocation = Join-Path $taskRoot 'app.ico'
$taskLink.Description = '使用本地 MinerU VLM 批量转换 PDF 为 Markdown'
$taskLink.Save()
Write-Output '桌面快捷方式已创建。'
