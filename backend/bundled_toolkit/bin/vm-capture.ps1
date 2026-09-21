# vm-capture.ps1 - capture the VM's own screen to a PNG on the toolkit mount
# so the host (and a vision-capable model) can see what the sandbox shows.
#
# Usage (inside the VM; toolkit\bin is on PATH already):
#   powershell -NoProfile -ExecutionPolicy Bypass -File toolkit\bin\vm-capture.ps1 [outPath]
#
# Default output: <toolkit>\vm-screen.png (host side: ~/.yaah/toolkit/vm-screen.png).
# Read the PNG from the host afterwards (view_image on the host path) - the
# mount is the transport; no host screenshot of the sandbox window needed.
param([string]$OutPath = "")
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
if (-not $OutPath) {
  $tk = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
  $OutPath = Join-Path $tk 'vm-screen.png'
}
$b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap($b.Width, $b.Height)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Location, [System.Drawing.Point]::Empty, $b.Size)
$g.Dispose()
$bmp.Save($OutPath, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Output ("saved " + $OutPath + " " + $b.Width + "x" + $b.Height)
