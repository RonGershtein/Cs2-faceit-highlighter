# Creates a "CS2 Highlighter" shortcut on the Desktop that starts the app (double-click to run).
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'CS2 Highlighter.lnk'
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = Join-Path $root 'start.bat'
$s.WorkingDirectory = $root
$s.IconLocation = (Join-Path $root 'assets\icon.ico') + ',0'
$s.Description = 'CS2 FACEIT Highlighter'
$s.WindowStyle = 7   # minimized console window
$s.Save()
Write-Output "shortcut created: $lnk"
