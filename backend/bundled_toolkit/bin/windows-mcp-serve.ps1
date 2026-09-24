# windows-mcp-serve.ps1 - start the windows-mcp MCP server inside the Windows
# Sandbox VM from YAAH's VENDORED copy (backend/bundled_toolkit/vendor/
# windows_mcp), so it works even if the upstream project disappears from PyPI.
# Run INSIDE the VM via sandbox_run:
#   powershell -ExecutionPolicy Bypass -File <toolkit>\bin\windows-mcp-serve.ps1
#
# Env overrides: WMCP_PORT (default 8000), WMCP_KEY (default sandbox-demo-key).
#
# Host side connects to:  http://<vm-ip>:<port>/mcp   (NO trailing slash!)
#   header: Authorization: Bearer <key>
#   flow: POST initialize -> grab mcp-session-id header -> POST
#         notifications/initialized -> tools/list & tools/call with the
#         mcp-session-id header on every request.
# Gotchas: /mcp/ (trailing slash) 307-redirects and DROPS the POST body.
#          Type/Click tools need loc:[x,y] or label from a Snapshot call.

$ErrorActionPreference = 'Stop'
$toolkit = 'C:\Users\WDAGUtilityAccount\Desktop\toolkit'
$port    = if ($env:WMCP_PORT) { $env:WMCP_PORT } else { '8000' }
$key     = if ($env:WMCP_KEY)  { $env:WMCP_KEY }  else { 'sandbox-demo-key' }
$py      = Join-Path $toolkit 'python312\python.exe'
$vendor  = Join-Path $toolkit 'vendor\windows_mcp'
$marker  = Join-Path $toolkit 'vendor\.windows-mcp-deps-ok'

if (-not (Test-Path $py)) { throw "toolkit python not found at $py" }
if (-not (Test-Path $vendor)) { throw "vendored windows_mcp not found at $vendor" }

# 1. Dependencies: install pinned deps once (marker file skips re-install).
if (-not (Test-Path $marker)) {
    Write-Host '[1/3] Installing pinned windows-mcp dependencies (first run)...'
    & $py -m pip install --quiet -r (Join-Path $toolkit 'vendor\windows-mcp-requirements.txt')
    if ($LASTEXITCODE -ne 0) { throw 'pip install of windows-mcp deps failed' }
    New-Item -ItemType File -Path $marker -Force | Out-Null
} else {
    Write-Host '[1/3] Dependencies already installed.'
}

# 2. Restart clean, then launch from the vendored source.
Write-Host '[2/3] Starting vendored windows-mcp on 0.0.0.0:'$port '(bearer auth on)'
Get-Process windows-mcp, python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like "$toolkit*" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

$env:WINDOWS_MCP_AUTH_KEY = $key
$env:PYTHONPATH = $vendor
Start-Process -FilePath $py `
    -ArgumentList '-m','windows_mcp','serve','--transport','streamable-http',`
        '--host','0.0.0.0','--port',$port `
    -WindowStyle Hidden

# 3. Wait for readiness (401 without auth == up and listening).
Write-Host '[3/3] Waiting for readiness...'
$up = $false; $code = ''
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    $code = curl.exe -s -o NUL -w "%{http_code}" "http://localhost:$port/mcp"
    if ($code -eq '401' -or $code -eq '200') { $up = $true; break }
}
if (-not $up) { throw 'Server did not become ready within 20s' }
Write-Host "Server ready (HTTP $code on /mcp)."

$ip = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notmatch '^127\.|^169\.254\.' } |
    Select-Object -First 1).IPAddress

Write-Host ''
Write-Host '=== HOST CONNECTION INFO ==='
Write-Host "URL:      http://${ip}:${port}/mcp     <-- no trailing slash"
Write-Host "Auth:     Authorization: Bearer $key"
Write-Host 'Flow:      POST initialize -> grab mcp-session-id header -> POST'
Write-Host '           notifications/initialized -> then tools/list / tools/call'
Write-Host '           with the mcp-session-id header on every request.'
Write-Host 'Gotchas:   /mcp/ (trailing slash) 307-redirects and DROPS the POST body.'
Write-Host '           Type/Click tools need loc:[x,y] or label from a Snapshot call.'
