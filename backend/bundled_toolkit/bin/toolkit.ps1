# toolkit.ps1 - record toolkit installs into state.json + regenerate INDEX.md.
# Usage (inside the VM, toolkit dirs are on PATH so `toolkit install ...` works):
#   toolkit install <name> -Version 1.2 -Kind zip -Path mytool/bin/tool.exe `
#       -Check "Test-Path '<toolkit>\mytool\bin\tool.exe'" `
#       -Invocation "mytool\bin\tool.exe --help" [-Note "gotcha text"]
#   toolkit remove <name>
#   toolkit list          # print state.json entries as JSON
#   toolkit index         # regenerate INDEX.md from state.json
# The wrapper owns the manifest mutation; never hand-edit state.json.
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet("install", "remove", "list", "index")]
    [string]$Action,
    [Parameter(Position = 1)]
    [string]$Name,
    [string]$Version,
    [string]$Kind,
    [string]$Path,
    [string]$Check,
    [string]$Invocation,
    [string]$Note,
    [string]$ToolkitDir
)

$ErrorActionPreference = "Stop"

if (-not $ToolkitDir) {
    # Default: this script lives in <toolkit>\bin, so the toolkit root is its parent.
    $ToolkitDir = Split-Path -Parent $PSScriptRoot
}
if (-not (Test-Path -LiteralPath $ToolkitDir)) {
    Write-Error "toolkit dir not found: $ToolkitDir"
    exit 2
}
$statePath = Join-Path $ToolkitDir "state.json"
$indexPath = Join-Path $ToolkitDir "INDEX.md"

function Read-State {
    if (Test-Path -LiteralPath $statePath) {
        try { return Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { Write-Error "state.json is not valid JSON: $_"; exit 2 }
    }
    return [pscustomobject]@{ tools = [pscustomobject]@{} }
}

function Save-State($state) {
    $json = $state | ConvertTo-Json -Depth 10
    # Atomic-ish write: temp file then move, so a killed process can't truncate.
    $tmp = "$statePath.tmp"
    [System.IO.File]::WriteAllText($tmp, $json + "`n", (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -Force -LiteralPath $tmp -Destination $statePath
}

function Update-Index($state) {
    $lines = @(
        "# Toolkit index",
        "",
        "Generated from ``state.json`` by ``toolkit install`` - do not edit by hand.",
        "Paths are relative to the toolkit root (on PATH inside the VM:",
        "``toolkit``, ``toolkit\bin``, ``toolkit\Scripts``, ``toolkit\node_modules\.bin``).",
        "",
        "| name | version | kind | path | invocation | check | note |",
        "|---|---|---|---|---|---|---|"
    )
    $tools = $state.tools
    $names = @()
    if ($tools) { $names = $tools.PSObject.Properties.Name | Sort-Object }
    foreach ($n in $names) {
        $t = $tools.$n
        $lines += ("| {0} | {1} | {2} | {3} | {4} | {5} | {6} |" -f `
            $n, $t.version, $t.kind, $t.path, $t.invocation, $t.check, $t.note)
    }
    [System.IO.File]::WriteAllLines($indexPath, $lines, (New-Object System.Text.UTF8Encoding($false)))
}

switch ($Action) {
    "install" {
        if (-not $Name) { Write-Error "usage: toolkit install <name> [-Version ...] [-Kind ...] [-Path ...] [-Check ...] [-Invocation ...] [-Note ...]"; exit 2 }
        $state = Read-State
        if ($state.tools -eq $null) {
            $state | Add-Member -MemberType NoteProperty -Name tools -Value ([pscustomobject]@{})
        }
        $entry = [ordered]@{}
        foreach ($pair in @(@("version", $Version), @("kind", $Kind), @("path", $Path),
                            @("check", $Check), @("invocation", $Invocation), @("note", $Note))) {
            if ($pair[1]) { $entry[$pair[0]] = $pair[1] }
        }
        $entry["installed_at"] = (Get-Date).ToString("yyyy-MM-dd")
        $newEntry = [pscustomobject]$entry
        if ($state.tools.PSObject.Properties[$Name]) {
            $state.tools.$Name = $newEntry
        } else {
            $state.tools | Add-Member -MemberType NoteProperty -Name $Name -Value $newEntry
        }
        Save-State $state
        Update-Index $state
        Write-Output "recorded '$Name' in state.json and regenerated INDEX.md"
    }
    "remove" {
        if (-not $Name) { Write-Error "usage: toolkit remove <name>"; exit 2 }
        $state = Read-State
        if ($state.tools -and $state.tools.PSObject.Properties[$Name]) {
            $state.tools.PSObject.Properties.Remove($Name)
            Save-State $state
            Update-Index $state
            Write-Output "removed '$Name' from state.json and regenerated INDEX.md"
        } else {
            Write-Output "'$Name' not in state.json (nothing to remove)"
        }
    }
    "list" {
        (Read-State).tools | ConvertTo-Json -Depth 10
    }
    "index" {
        Update-Index (Read-State)
        Write-Output "regenerated $indexPath"
    }
}
