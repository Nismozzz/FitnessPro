$ErrorActionPreference = "Stop"
$source = Split-Path -Parent $MyInvocation.MyCommand.Path
$dest = Join-Path $env:LOCALAPPDATA "Programs\FitnessPro"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
$zip = Join-Path $source "payload.zip"
if (Test-Path $zip) {
    & tar.exe -xf $zip -C $dest
    if ($LASTEXITCODE -ne 0) { throw "解压 FitnessPro 失败，代码 $LASTEXITCODE" }
} else {
    & robocopy $source $dest /E /R:1 /W:1 /NFL /NDL /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "复制文件失败，代码 $LASTEXITCODE" }
}

$shell = New-Object -ComObject WScript.Shell
$target = Join-Path $dest "FitnessPro.exe"
$desktop = $shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath("Desktop")) "FitnessPro.lnk"))
$desktop.TargetPath = $target
$desktop.WorkingDirectory = $dest
$desktop.Description = "FitnessPro"
$desktop.Save()

$menuDir = Join-Path ([Environment]::GetFolderPath("Programs")) "FitnessPro"
New-Item -ItemType Directory -Force -Path $menuDir | Out-Null
$menu = $shell.CreateShortcut((Join-Path $menuDir "FitnessPro.lnk"))
$menu.TargetPath = $target
$menu.WorkingDirectory = $dest
$menu.Save()
$remove = $shell.CreateShortcut((Join-Path $menuDir "卸载 FitnessPro.lnk"))
$remove.TargetPath = Join-Path $dest "uninstall.cmd"
$remove.WorkingDirectory = $dest
$remove.Save()

$key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FitnessPro"
New-Item -Force -Path $key | Out-Null
Set-ItemProperty $key DisplayName "FitnessPro"
Set-ItemProperty $key Publisher "FitnessPro"
Set-ItemProperty $key DisplayVersion "0.1.0"
Set-ItemProperty $key InstallLocation $dest
Set-ItemProperty $key UninstallString "`"$dest\uninstall.cmd`""
Set-ItemProperty $key NoModify 1 -Type DWord
Set-ItemProperty $key NoRepair 1 -Type DWord

Start-Process -FilePath $target -WorkingDirectory $dest
