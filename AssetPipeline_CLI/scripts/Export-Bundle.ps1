param(
    [Parameter(Mandatory = $true)]
    [string]$Directory,

    [switch]$KeepNames,
    [switch]$KeepDecomp,
    [switch]$ForceDecompToMemory,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs = @()
)

$argsList = @("batchexportbundle", $Directory)
if ($KeepNames) { $argsList += "-keepnames" }
if ($KeepDecomp) { $argsList += "-kd" }
if ($ForceDecompToMemory) { $argsList += "-md" }
$argsList += $ExtraArgs

& "$PSScriptRoot\Invoke-UABEA.ps1" @argsList
