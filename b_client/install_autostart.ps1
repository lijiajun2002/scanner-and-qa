$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$startup = [Environment]::GetFolderPath('Startup')
$lnkPath = Join-Path $startup 'ScannerQA-Agent.lnk'

$exe = Join-Path $here 'ScannerQA-Agent.exe'
if (Test-Path $exe) {
    $target = $exe
    $arguments = ''
    Write-Host "Using packaged exe: $exe"
} else {
    $pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
    if (-not $pythonw) {
        Write-Host "[ERROR] ScannerQA-Agent.exe not found, and pythonw.exe not found either."
        Write-Host "        Run build_exe.bat first, or install Python."
        Read-Host
        exit 1
    }
    $target = $pythonw
    $arguments = '"' + (Join-Path $here 'capture_agent.py') + '"'
    Write-Host "Using python source: $pythonw"
}

$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath = $target
$lnk.Arguments = $arguments
$lnk.WorkingDirectory = $here
$lnk.WindowStyle = 7
$lnk.Save()

Write-Host "Autostart added: $lnkPath"
