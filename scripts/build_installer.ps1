# 生成可安装的 FitnessPro-Setup.exe。
# 用法：powershell -ExecutionPolicy Bypass -File scripts\build_installer.ps1
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "找不到 $Python。请先在项目根目录创建虚拟环境并安装 requirements.txt。"
}

Write-Host "构建前端…"
Push-Location (Join-Path $Root "frontend")
npm run build
Pop-Location

Write-Host "安装打包工具…"
if (Get-Command uv -ErrorAction SilentlyContinue) {
    uv pip install --python $Python "pyinstaller>=6.0"
} else {
    & $Python -m pip install "pyinstaller>=6.0"
}

Write-Host "打包应用…"
if (Test-Path (Join-Path $Root "release\FitnessPro")) {
    Remove-Item -Recurse -Force (Join-Path $Root "release\FitnessPro")
}
& $Python -m PyInstaller --noconfirm --distpath (Join-Path $Root "release") --workpath (Join-Path $Root "release\work") fitnesspro.spec

Copy-Item (Join-Path $Root "installer\payload\*") (Join-Path $Root "release\FitnessPro") -Force

$Iscc = @(
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1

if ($Iscc) {
    Write-Host "用 Inno Setup 生成安装包…"
    & $Iscc (Join-Path $Root "installer\FitnessPro.iss")
} else {
    Write-Host "用 Windows 自带的 IExpress 生成安装包…"
    $Stage = Join-Path $Root "release\iexpress-stage"
    if (Test-Path $Stage) { Remove-Item -Recurse -Force $Stage }
    New-Item -ItemType Directory -Path $Stage | Out-Null
    tar.exe -a -cf (Join-Path $Stage "payload.zip") -C (Join-Path $Root "release\FitnessPro") .
    Copy-Item (Join-Path $Root "installer\payload\install.cmd") $Stage
    Copy-Item (Join-Path $Root "installer\payload\install.ps1") $Stage
    $setup = Join-Path $Root "release\FitnessPro-Setup.exe"
    $sedPath = Join-Path $Root "release\FitnessPro.sed"
    @"
[Version]
Class=IEXPRESS
SEDVersion=3
[Options]
PackagePurpose=InstallApp
ShowInstallProgramWindow=0
HideExtractAnimation=0
UseLongFileName=1
InsideCompressed=0
CAB_FixedSize=0
CAB_ResvCodeSigning=0
RebootMode=N
InstallPrompt=%InstallPrompt%
DisplayLicense=%DisplayLicense%
FinishMessage=%FinishMessage%
TargetName=%TargetName%
FriendlyName=%FriendlyName%
AppLaunched=%AppLaunched%
PostInstallCmd=%PostInstallCmd%
AdminQuietInstCmd=%AdminQuietInstCmd%
UserQuietInstCmd=%UserQuietInstCmd%
SourceFiles=SourceFiles

[Strings]
InstallPrompt=
DisplayLicense=
FinishMessage=FitnessPro installed. Open it from the desktop and enter the model key in Settings.
TargetName=$setup
FriendlyName=FitnessPro
AppLaunched=install.cmd
PostInstallCmd=<None>
AdminQuietInstCmd=
UserQuietInstCmd=
FILE0="install.cmd"
FILE1="install.ps1"
FILE2="payload.zip"

[SourceFiles]
SourceFiles0=$Stage

[SourceFiles0]
%FILE0%=
%FILE1%=
%FILE2%=
"@ | Set-Content -Path $sedPath -Encoding Unicode
    cmd /c "iexpress /N `"$sedPath`""
    if ($LASTEXITCODE -ne 0) { throw "IExpress 失败，退出码 $LASTEXITCODE" }
}
Write-Host "完成：$(Join-Path $Root 'release\FitnessPro-Setup.exe')"
