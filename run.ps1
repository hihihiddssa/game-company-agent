# 固定使用 learn-claude-code 已安装好依赖的 Python，不依赖当前终端是否激活了虚拟环境。
# 用法：.\run.ps1；检查配置：.\run.ps1 --check-config
# 额外参数会原样交给 main.py，例如：.\run.ps1 --workdir workspace\my-game
$ErrorActionPreference = 'Stop'
$studioPython = 'D:\project\learn-claude-code\.venv\Scripts\python.exe'
$studioConfig = Join-Path $PSScriptRoot '.env'
$studioMain = Join-Path $PSScriptRoot 'main.py'

if (!(Test-Path -LiteralPath $studioPython -PathType Leaf)) {
    throw "Existing Python environment not found: $studioPython"
}
if (!(Test-Path -LiteralPath $studioConfig -PathType Leaf)) {
    throw "Existing model configuration not found: $studioConfig"
}

# 复用旧虚拟环境，但模型配置读取本项目的 DeepSeek .env；不打印密钥。
& $studioPython -u $studioMain --env-file $studioConfig @args
exit $LASTEXITCODE
