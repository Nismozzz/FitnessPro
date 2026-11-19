$ErrorActionPreference = "SilentlyContinue"
Get-Process FitnessPro -ErrorAction SilentlyContinue | Stop-Process -Force
$dest = Join-Path $env:LOCALAPPDATA "Programs\FitnessPro"
Remove-Item (Join-Path ([Environment]::GetFolderPath("Desktop")) "FitnessPro.lnk") -Force
Remove-Item (Join-Path ([Environment]::GetFolderPath("Programs")) "FitnessPro") -Recurse -Force
Remove-Item "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\FitnessPro" -Recurse -Force
Start-Process -FilePath "cmd.exe" -WindowStyle Hidden -ArgumentList "/c timeout /t 2 >nul & rmdir /s /q `"$dest`""
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.MessageBox]::Show("FitnessPro 已卸载。训练记录和模型密钥仍保留在本机用户目录。", "FitnessPro")
