# Creates a "CS2 Highlighter" shortcut on the Desktop that starts the app (double-click to run).
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$desktop = [Environment]::GetFolderPath('Desktop')
$lnk = Join-Path $desktop 'CS2 Highlighter.lnk'
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$exe = Join-Path $root 'CS2 Highlighter.exe'
if (Test-Path $exe) { $s.TargetPath = $exe; $s.WindowStyle = 1 }   # the launcher: no console, lives in the tray
else { $s.TargetPath = Join-Path $root 'start.bat'; $s.WindowStyle = 7 }   # minimized console window
$s.WorkingDirectory = $root
$s.IconLocation = (Join-Path $root 'assets\icon.ico') + ',0'
$s.Description = 'CS2 FACEIT Highlighter'
$s.Save()
Write-Output "shortcut created: $lnk"
