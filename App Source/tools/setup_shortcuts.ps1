param([string]$DesktopPath = "")

$ErrorActionPreference = "Stop"
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$version = Get-Content -LiteralPath (Join-Path $root "version.json") -Raw -Encoding UTF8 | ConvertFrom-Json
if ($version.product_id -ne "png-mockup") { throw "This is not a PNG Mockup installation." }
$launcher = Join-Path $root "PNG Mockup.exe"
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw "PNG Mockup.exe is missing." }
if ([string]::IsNullOrWhiteSpace($DesktopPath)) { $DesktopPath = [Environment]::GetFolderPath("Desktop") }
$desktop = [IO.Path]::GetFullPath($DesktopPath)
if (-not (Test-Path -LiteralPath $desktop -PathType Container)) { throw "Desktop directory is missing: $desktop" }
$assembly = [Reflection.Assembly]::LoadFile($launcher)
$support = $assembly.GetType('ProductSupport', $true)
$method = $support.GetMethod('CreateShortcut', [Reflection.BindingFlags]'Public, Static')
if ($null -eq $method) { throw 'The PNG Mockup launcher does not provide safe shortcut creation. Reinstall the current version.' }
try {
    $result = $method.Invoke($null, [object[]]@($root, $desktop))
} catch {
    if ($_.Exception.InnerException) { throw $_.Exception.InnerException }
    throw
}
Write-Host "$result`: $(Join-Path $desktop 'PNG Mockup.lnk')"
