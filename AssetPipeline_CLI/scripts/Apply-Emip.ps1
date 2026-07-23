param(
    [Parameter(Mandatory = $true)]
    [string]$EmipFile,

    [Parameter(Mandatory = $true)]
    [string]$Directory,

    [switch]$ForceDecompToMemory,
    [switch]$KeepDecomp,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs = @()
)

$argsList = @("applyemip", $EmipFile, $Directory)
if ($KeepDecomp) { $argsList += "-kd" }
if ($ForceDecompToMemory) { $argsList += "-md" }
$argsList += $ExtraArgs

& "$PSScriptRoot\Invoke-UABEA.ps1" @argsList
