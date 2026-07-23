param(
    [Parameter(Mandatory = $true)]
    [string]$Source,

    [Parameter(Mandatory = $true)]
    [string]$Work,

    [ValidateSet("json", "txt")]
    [string]$DumpFormat = "json",

    [ValidateSet("png", "jpg")]
    [string]$ImageFormat = "png",

    [int]$Quality = 90
)

$project = Join-Path $PSScriptRoot "..\UnityResourceCLI\UnityResourceCLI.csproj"
$args = @(
    "run",
    "--project", $project,
    "--",
    "import",
    "--source", $Source,
    "--work", $Work,
    "--dump-format", $DumpFormat,
    "--image-format", $ImageFormat,
    "--quality", $Quality
)
& dotnet @args
exit $LASTEXITCODE
