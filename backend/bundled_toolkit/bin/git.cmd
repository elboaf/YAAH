# yaah sandbox git shim.
# Prepend-safe: resolve the real git.exe once (MinGit layout first, then a
# bare git.exe dropped next to this shim) and re-exec it with GIT_CONFIG_GLOBAL
# pointed at the toolkit gitconfig (mapped-workspace 'dubious ownership' fix).
$tk = Split-Path -Parent $PSScriptRoot   # toolkit root (this shim lives in toolkit\bin)
$env:GIT_CONFIG_GLOBAL = Join-Path $tk 'gitconfig'
$exe = $null
foreach ($c in @(
    (Join-Path $tk 'mingit\cmd\git.exe'),
    (Join-Path $PSScriptRoot 'git.real.exe'),
    (Join-Path $tk 'git.exe'))) {
  if (Test-Path $c) { $exe = $c; break }
}
if (-not $exe) {
  Write-Error ("git not found in the toolkit: expected toolkit\mingit\cmd\git.exe " +
               "(MinGit) or toolkit\bin\git.real.exe. Download MinGit from " +
               "https://github.com/git-for-windows/git/releases (zip, no installer) " +
               "and expand it to toolkit\mingit.")
  exit 9009
}
& $exe @args
exit $LASTEXITCODE
