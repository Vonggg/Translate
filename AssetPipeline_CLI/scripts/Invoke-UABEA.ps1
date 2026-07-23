param(
    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet("batchexportbundle", "batchimportbundle", "applyemip")]
    [string]$Command,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs = @()
)

$project = Join-Path $PSScriptRoot "..\UABEA\UABEAvalonia\UABEAvalonia.csproj"

if (-not (Test-Path -LiteralPath $project)) {
    throw "UABEA project not found: $project"
}

$dotnetArgs = @("run", "--project", $project, "--", $Command) + $RemainingArgs
& dotnet @dotnetArgs
exit $LASTEXITCODE
