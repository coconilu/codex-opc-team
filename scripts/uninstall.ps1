[CmdletBinding()]
param(
    [string]$KnowledgeHome,
    [switch]$RemoveMarketplace,
    [switch]$Apply,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$arguments = @((Join-Path $PSScriptRoot 'plugin_admin.py'), 'uninstall')
if ($KnowledgeHome) { $arguments += @('--knowledge-home', $KnowledgeHome) }
if ($RemoveMarketplace) { $arguments += '--remove-marketplace' }
if ($Apply) { $arguments += '--apply' }
if ($DryRun) { $arguments += '--dry-run' }
& python @arguments
exit $LASTEXITCODE
