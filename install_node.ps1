# Installs the bridge node into ComfyUI.
#
# No LAUNCHER marker here: install_node.bat and uninstall_node.bat carry them,
# because a .ps1 has no file association on Windows and cannot be double-clicked.
# Both call this file, so the checks below stay the only copy.
#
# A directory junction rather than a copy, so edits to comfy_node/ take effect on
# the next ComfyUI restart with nothing to re-run and no second copy to drift.
# Junctions need no administrator rights.
#
#   .\install_node.ps1            # install (or repair) the link
#   .\install_node.ps1 -Uninstall # remove it
#
# ComfyUI must be restarted afterwards: custom nodes are imported once at startup.

[CmdletBinding()]
param(
    [string] $ComfyRoot,
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$source = Join-Path $projectRoot 'comfy_node\comfyui_mcp_bridge'

& (Join-Path $projectRoot 'logo.ps1') -Subtitle $(
    if ($Uninstall) { 'comfyui-mcp - removing the bridge node' }
    else { 'comfyui-mcp - installing the bridge node' }
)

function Get-ComfyRootFromEnvFile {
    $envFile = Join-Path $projectRoot '.env'
    if (-not (Test-Path $envFile)) { return $null }
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*COMFYUI_ROOT\s*=\s*(.+?)\s*$') { return $Matches[1].Trim('"') }
    }
    return $null
}

if (-not $ComfyRoot) { $ComfyRoot = $env:COMFYUI_ROOT }
if (-not $ComfyRoot) { $ComfyRoot = Get-ComfyRootFromEnvFile }
if (-not $ComfyRoot) {
    throw "COMFYUI_ROOT is not set. Put it in .env, or pass -ComfyRoot <path>."
}

$customNodes = Join-Path $ComfyRoot 'ComfyUI\custom_nodes'
if (-not (Test-Path $customNodes)) {
    throw "No custom_nodes directory under $ComfyRoot. Is COMFYUI_ROOT right?"
}
$target = Join-Path $customNodes 'comfyui_mcp_bridge'

if ($Uninstall) {
    if (-not (Test-Path $target)) { Write-Host "Not installed: $target"; exit 0 }
    $item = Get-Item $target -Force
    if ($item.LinkType -ne 'Junction') {
        throw "$target is a real directory, not a link this script made. Remove it by hand."
    }
    [System.IO.Directory]::Delete($target)
    Write-Host "Removed $target"
    Write-Host "Restart ComfyUI to unload it."
    exit 0
}

if (Test-Path $target) {
    $item = Get-Item $target -Force
    if ($item.LinkType -eq 'Junction') {
        if ($item.Target -contains $source) {
            Write-Host "Already installed: $target -> $source"
            exit 0
        }
        [System.IO.Directory]::Delete($target)
    }
    else {
        throw "$target already exists and is not a link. Remove it by hand first."
    }
}

New-Item -ItemType Junction -Path $target -Target $source | Out-Null
Write-Host "Installed $target -> $source"
Write-Host ""
Write-Host "Restart ComfyUI, then reload the browser tab."
Write-Host "Check it with the workspace_status tool, or:"
Write-Host "  curl http://127.0.0.1:8188/mcp_bridge/clients"
