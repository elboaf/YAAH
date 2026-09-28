# Toolkit index

Generated from `state.json` by `toolkit install` - do not edit by hand.
Paths are relative to the toolkit root (on PATH inside the VM:
`toolkit`, `toolkit\bin`, `toolkit\Scripts`, `toolkit\node_modules\.bin`).

| name | version | kind | path | invocation | check | note |
|---|---|---|---|---|---|---|
| git-shim | 1.0 | script |  |  | Test-Path '<toolkit>\bin\git.cmd' |  |
| vm-capture | 1.0 | script |  |  | Test-Path '<toolkit>\bin\vm-capture.ps1' |  |
| windows-mcp | 0.8.5 | vendored |  |  | Test-Path '<toolkit>\vendor\windows_mcp\__init__.py' | Vendored MCP server for sandbox GUI control. Start inside the VM: powershell -ExecutionPolicy Bypass -File <toolkit>\bin\windows-mcp-serve.ps1. Host connects to http://<vm-ip>:<port>/mcp (no trailing slash) with Authorization: Bearer <key>. Pinned deps in vendor\windows-mcp-requirements.txt install on first run. |
