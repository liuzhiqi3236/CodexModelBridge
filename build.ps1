param([string]$OutputDirectory = (Split-Path -Parent $PSScriptRoot))
$ErrorActionPreference = 'Stop'
# 仅使用项目虚拟环境，不安装全局软件；凭据助手使用 Windows 已有编译器。
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (!(Test-Path -LiteralPath $python)) { throw '请先按 README 建立 .venv 并安装锁定依赖。' }
if (!(Test-Path -LiteralPath $compiler)) { throw '当前系统缺少 .NET Framework C# 编译器。' }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$helperDirectory = Join-Path $OutputDirectory 'runtime'
New-Item -ItemType Directory -Path $helperDirectory -Force | Out-Null
# 辅助程序放进内部目录，外层只提供主程序启动入口。
$helper = Join-Path $helperDirectory 'ModelBridge.Key.exe'
& $compiler /nologo /target:exe /optimize+ /reference:System.Security.dll "/out:$helper" (Join-Path $PSScriptRoot 'credential_helper.cs')
if ($LASTEXITCODE -ne 0) { throw '凭据助手编译失败。' }
Push-Location $PSScriptRoot
try {
    $webAssets = (Join-Path $PSScriptRoot 'web') + ';web'
    & $python -m PyInstaller --noconfirm --onefile --windowed --name CodexModelBridge --add-data $webAssets --distpath $OutputDirectory --workpath 'work\build' --specpath 'work' --exclude-module tkinter main.py
    if ($LASTEXITCODE -ne 0) { throw '桌面程序打包失败。' }
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'sources.json') -Destination (Join-Path $OutputDirectory 'sources.json')
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Destination (Join-Path $OutputDirectory '使用说明.md')
} finally { Pop-Location }
