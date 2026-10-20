# Membuat shortcut Pulo di Desktop dan Start Menu.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe = Join-Path $here 'dist\Pulo.exe'
if (-not (Test-Path $exe)) {
    Write-Host 'Pulo.exe belum ada. Jalankan buat-exe.bat dulu.'
    exit 1
}
$shell = New-Object -ComObject WScript.Shell
$places = @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))
foreach ($dir in $places) {
    $path = Join-Path $dir 'Pulo.lnk'
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath = $exe
    $lnk.WorkingDirectory = Split-Path $exe
    $lnk.IconLocation = "$exe,0"
    $lnk.Description = 'Pulo: pulau fokus di tepi atas layar'
    $lnk.Save()
    Write-Host "Shortcut dibuat: $path"
}
