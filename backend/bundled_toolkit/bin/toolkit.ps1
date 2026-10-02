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
$lockPath = "$statePath.lock"

# Cross-process lock: hold an exclusive range lock on <state>.lock for the
# whole Read-State -> mutate -> Save -> Update-Index critical section, so
# two concurrent installs can't last-write-win each other. Fails loudly
# when the lock is held elsewhere instead of silently losing an entry.
function Enter-ManifestLock {
    $fs = [System.IO.File]::Open($lockPath,
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::None)
    try { $fs.Lock(0, 1) } catch { $fs.Dispose(); throw }
    return $fs
}

function Exit-ManifestLock($fs) {
    try { $fs.Unlock(0, 1) } finally { $fs.Dispose() }
}

function Read-State {
    if (Test-Path -LiteralPath $statePath) {
        try { return Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json }
        catch { Write-Error "state.json is not valid JSON: $_"; exit 2 }
    }
    return [pscustomobject]@{ tools = [pscustomobject]@{} }
}

function Save-State($state) {
    $json = $state | ConvertTo-Json -Depth 10
    # Atomic-ish write: unique temp file then move, so a killed process can't
    # truncate and concurrent holders can't collide on one temp path.
    $tmp = "$statePath.$PID.$([System.IO.Path]::GetRandomFileName()).tmp"
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
        # Escape for table rendering only; state.json keeps values verbatim.
        $cells = @($n, $t.version, $t.kind, $t.path, $t.invocation, $t.check, $t.note) |
            ForEach-Object { ("$_" -replace '\|', '\|') -replace "(`r?`n|`r)", " " }
        $lines += ("| {0} |" -f ($cells -join " | "))
    }
    # Atomic write: unique temp file then move, same contract as
    # Save-State, so an I/O failure mid-write leaves the existing
    # INDEX.md intact instead of empty or partial.
    $tmp = "$indexPath.$PID.$([System.IO.Path]::GetRandomFileName()).tmp"
    try {
        [System.IO.File]::WriteAllLines($tmp, $lines, (New-Object System.Text.UTF8Encoding($false)))
        Move-Item -Force -LiteralPath $tmp -Destination $indexPath
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -Force -LiteralPath $tmp }
    }
}

switch ($Action) {
    "install" {
        if (-not $Name) { Write-Error "usage: toolkit install <name> [-Version ...] [-Kind ...] [-Path ...] [-Check ...] [-Invocation ...] [-Note ...]"; exit 2 }
        $lock = Enter-ManifestLock
        try {
            $state = Read-State
            if ($state.tools -eq $null) {
                $state | Add-Member -MemberType NoteProperty -Name tools -Value ([pscustomobject]@{})
            }
            # Seed from the existing entry so a partial reinstall (e.g. only
            # -Version) preserves the metadata already recorded.
            $entry = [ordered]@{}
            if ($state.tools.PSObject.Properties[$Name]) {
                foreach ($p in $state.tools.$Name.PSObject.Properties) {
                    if ($p.Name -ne "installed_at" -and $p.Value) { $entry[$p.Name] = $p.Value }
                }
            }
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
        } finally { Exit-ManifestLock $lock }
        Write-Output "recorded '$Name' in state.json and regenerated INDEX.md"
    }
    "remove" {
        if (-not $Name) { Write-Error "usage: toolkit remove <name>"; exit 2 }
        $lock = Enter-ManifestLock
        try {
            $state = Read-State
            if ($state.tools -and $state.tools.PSObject.Properties[$Name]) {
                $state.tools.PSObject.Properties.Remove($Name)
                Save-State $state
                Update-Index $state
                $removed = $true
            } else {
                $removed = $false
            }
        } finally { Exit-ManifestLock $lock }
        if ($removed) {
            Write-Output "removed '$Name' from state.json and regenerated INDEX.md"
        } else {
            Write-Output "'$Name' not in state.json (nothing to remove)"
        }
    }
    "list" {
        (Read-State).tools | ConvertTo-Json -Depth 10
    }
    "index" {
        $lock = Enter-ManifestLock
        try { Update-Index (Read-State) } finally { Exit-ManifestLock $lock }
        Write-Output "regenerated $indexPath"
    }
}
