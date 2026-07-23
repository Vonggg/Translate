param(
    [Parameter(Mandatory = $true)]
    [string]$Directory,

    [switch]$KeepDecomp,
    [switch]$ForceDecompToMemory,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs = @()
)

$argsList = @("batchimportbundle", $Directory)
if ($KeepDecomp) { $argsList += "-kd" }
if ($ForceDecompToMemory) { $argsList += "-md" }
$argsList += $ExtraArgs

& "$PSScriptRoot\Invoke-UABEA.ps1" @argsList
