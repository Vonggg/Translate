param(
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Release",
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory,
    [string]$PreferredVisualStudioPath = "D:\ProgramFile\SoftwareTool\Develop\VisualStudio\2022\Community"
)

$ErrorActionPreference = "Stop"

$assetPipelineRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$assetsToolsRoot = Join-Path $assetPipelineRoot "AssetsTools.NET"
$buildRoot = Join-Path $assetsToolsRoot "build-native"
$outputRoot = [System.IO.Path]::GetFullPath($OutputDirectory)

function Test-CMakeExecutable {
    param([string]$Path)

    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }

    try {
        $null = & $Path --version 2>$null
        return $LASTEXITCODE -eq 0
    }
    catch {
        return $false
    }
}

function Get-VisualStudioCMake {
    param([string]$InstallationPath)

    if (-not $InstallationPath -or -not (Test-Path -LiteralPath $InstallationPath -PathType Container)) {
        return $null
    }

    $cmake = Join-Path $InstallationPath "Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    $msbuild = Join-Path $InstallationPath "MSBuild\Current\Bin\MSBuild.exe"
    $msvcRoot = Join-Path $InstallationPath "VC\Tools\MSVC"
    $hasCompiler = Get-ChildItem -LiteralPath $msvcRoot -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "bin\Hostx64\x64\cl.exe") } |
        Select-Object -First 1

    if ((Test-CMakeExecutable $cmake) -and
        (Test-Path -LiteralPath $msbuild -PathType Leaf) -and
        $hasCompiler) {
        return $cmake
    }

    return $null
}

function Get-VsWherePath {
    $candidates = @()
    if (${env:ProgramFiles(x86)}) {
        $candidates += Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    }
    $command = Get-Command vswhere.exe -ErrorAction SilentlyContinue
    if ($command) {
        $candidates += $command.Source
    }

    return $candidates |
        Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
        Select-Object -Unique -First 1
}

$cmakePath = Get-VisualStudioCMake $PreferredVisualStudioPath
$selectedVisualStudioPath = if ($cmakePath) { $PreferredVisualStudioPath } else { $null }

if (-not $cmakePath) {
    Write-Output "[TextureEncoder] 首选 Visual Studio 不可用，正在自动查找: $PreferredVisualStudioPath"
    $vswhere = Get-VsWherePath
    if ($vswhere) {
        $installationPaths = & $vswhere -all -products * `
            -requires Microsoft.VisualStudio.Component.VC.CMake.Project Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
            -property installationPath
        foreach ($installationPath in $installationPaths) {
            $candidate = Get-VisualStudioCMake $installationPath
            if ($candidate) {
                $cmakePath = $candidate
                $selectedVisualStudioPath = $installationPath
                break
            }
        }
    }
}

if (-not $cmakePath) {
    $cmakeCommand = Get-Command cmake.exe -ErrorAction SilentlyContinue
    if ($cmakeCommand -and (Test-CMakeExecutable $cmakeCommand.Source)) {
        $cmakePath = $cmakeCommand.Source
    }
}

if (-not $cmakePath) {
    throw "未找到有效的 Visual Studio C++/CMake 构建环境。请在 Visual Studio Installer 中安装‘使用 C++ 的桌面开发’和 CMake 组件。"
}

if ($selectedVisualStudioPath) {
    Write-Output "[TextureEncoder] 使用 Visual Studio: $selectedVisualStudioPath"
}
else {
    Write-Output "[TextureEncoder] 使用 PATH 中的 CMake: $cmakePath"
}

$cachePath = Join-Path $buildRoot "CMakeCache.txt"
if (Test-Path -LiteralPath $cachePath -PathType Leaf) {
    $cachedCmakeLine = Select-String -LiteralPath $cachePath -Pattern '^CMAKE_COMMAND:INTERNAL=(.+)$' |
        Select-Object -First 1
    $cachedCmakePath = if ($cachedCmakeLine) { $cachedCmakeLine.Matches[0].Groups[1].Value } else { $null }
    $cachedInstanceLine = Select-String -LiteralPath $cachePath -Pattern '^CMAKE_GENERATOR_INSTANCE:INTERNAL=(.+)$' |
        Select-Object -First 1
    $cachedInstancePath = if ($cachedInstanceLine) { $cachedInstanceLine.Matches[0].Groups[1].Value } else { $null }
    $normalizedCachedPath = if ($cachedCmakePath) {
        [System.IO.Path]::GetFullPath($cachedCmakePath).Replace('/', '\').TrimEnd('\')
    }
    $normalizedSelectedPath = [System.IO.Path]::GetFullPath($cmakePath).Replace('/', '\').TrimEnd('\')
    $cmakeChanged = -not $normalizedCachedPath -or
        -not $normalizedCachedPath.Equals($normalizedSelectedPath, [System.StringComparison]::OrdinalIgnoreCase)
    $visualStudioChanged = $false
    if ($selectedVisualStudioPath) {
        $normalizedSelectedInstance = [System.IO.Path]::GetFullPath($selectedVisualStudioPath).Replace('/', '\').TrimEnd('\')
        $normalizedCachedInstance = if ($cachedInstancePath) {
            [System.IO.Path]::GetFullPath($cachedInstancePath).Replace('/', '\').TrimEnd('\')
        }
        $visualStudioChanged = -not $normalizedCachedInstance -or
            -not $normalizedCachedInstance.Equals($normalizedSelectedInstance, [System.StringComparison]::OrdinalIgnoreCase)
    }
    elseif ($cachedInstancePath -and -not (Get-VisualStudioCMake $cachedInstancePath)) {
        $visualStudioChanged = $true
    }
    if ($cmakeChanged -or $visualStudioChanged) {
        Write-Output "[TextureEncoder] 检测到失效的旧构建环境缓存，正在重新配置。"
        Remove-Item -Force -LiteralPath $cachePath
        $cmakeFilesPath = Join-Path $buildRoot "CMakeFiles"
        if (Test-Path -LiteralPath $cmakeFilesPath -PathType Container) {
            Remove-Item -Recurse -Force -LiteralPath $cmakeFilesPath
        }
    }
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
