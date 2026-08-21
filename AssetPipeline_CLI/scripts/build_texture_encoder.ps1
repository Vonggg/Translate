param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release",
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"

$assetPipelineRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$assetsToolsRoot = Join-Path $assetPipelineRoot "AssetsTools.NET"
$buildRoot = Join-Path $assetsToolsRoot "build-native"
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)

$cmakeCommand = Get-Command cmake.exe -ErrorAction SilentlyContinue
$cmakePath = if ($cmakeCommand) { $cmakeCommand.Source } else { $null }
if (-not $cmakePath) {
    $vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (Test-Path -LiteralPath $vswhere) {
        $installationPath = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.CMake.Project -property installationPath
        if ($installationPath) {
            $candidate = Join-Path $installationPath "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
            if (Test-Path -LiteralPath $candidate) {
                $cmakePath = $candidate
            }
        }
    }
}
if (-not $cmakePath) {
    throw "未找到 CMake。请在 Visual Studio Installer 中安装‘使用 C++ 的桌面开发’和 CMake 组件。"
}

& $cmakePath -S $assetsToolsRoot -B $buildRoot -A x64
if ($LASTEXITCODE -ne 0) {
    throw "TextureEncoder CMake 配置失败，返回码: $LASTEXITCODE"
}
& $cmakePath --build $buildRoot --config $Configuration --target TextureEncoder
if ($LASTEXITCODE -ne 0) {
    throw "TextureEncoder 构建失败，返回码: $LASTEXITCODE"
}

$nativeOutput = Join-Path $buildRoot "TextureEncoder\$Configuration"
$requiredFiles = @("textureencoder.dll", "cuttlefish.dll", "PVRTexLib.dll")
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
foreach ($name in $requiredFiles) {
    $source = Join-Path $nativeOutput $name
    if (-not (Test-Path -LiteralPath $source)) {
        throw "TextureEncoder 构建产物缺失: $source"
    }
    Copy-Item -Force -LiteralPath $source -Destination (Join-Path $outputRoot $name)
}

Write-Output "[TextureEncoder] 原生编码库已就绪: $outputRoot"
