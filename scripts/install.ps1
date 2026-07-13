[CmdletBinding()]
param(
    [string]$KnowledgeHome,
    [switch]$SkipKnowledgeInit,
    [switch]$ForceReinstall,
    [switch]$Apply,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$arguments = @((Join-Path $PSScriptRoot 'plugin_admin.py'), 'install')
if ($KnowledgeHome) { $arguments += @('--knowledge-home', $KnowledgeHome) }
if ($SkipKnowledgeInit) { $arguments += '--skip-knowledge-init' }
if ($ForceReinstall) { $arguments += '--force-reinstall' }
if ($Apply) { $arguments += '--apply' }
if ($DryRun) { $arguments += '--dry-run' }
& python @arguments
exit $LASTEXITCODE
