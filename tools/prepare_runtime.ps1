param(
    [Parameter(Mandatory = $true)][string]$SourceRuntime,
    [switch]$ReplaceExisting
)

$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$appRoot = Join-Path $projectRoot "App Source"
$source = (Resolve-Path -LiteralPath $SourceRuntime).Path
$target = [IO.Path]::GetFullPath((Join-Path $appRoot "runtime\python"))
$workspacePrefix = $projectRoot.TrimEnd('\') + '\'
function Assert-WorkspacePath([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    if (-not $resolved.StartsWith($workspacePrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing path outside project: $resolved"
    }
    $current = $resolved
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            if (((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Runtime destination cannot pass through a junction or symbolic link: $current"
            }
        }
        $current = [IO.Path]::GetDirectoryName($current)
    }
    return $resolved
}
[void](Assert-WorkspacePath $target)
foreach ($required in @("python.exe", "pythonw.exe", "python312.dll", "LICENSE.txt", "DLLs\_tkinter.pyd", "Lib\tkinter\__init__.py", "Lib\site-packages\PIL\__init__.py", "tcl\tcl8.6\init.tcl", "tcl\tk8.6\tk.tcl")) {
    if (-not (Test-Path -LiteralPath (Join-Path $source $required) -PathType Leaf)) {
        throw "Runtime source is missing: $required"
    }
}
if ((Test-Path -LiteralPath $target) -and -not $ReplaceExisting) {
    throw "Private runtime already exists. Use -ReplaceExisting only when intentionally rebuilding the distributable runtime."
}
$stageRoot = Assert-WorkspacePath (Join-Path $projectRoot ("build\runtime-" + [Guid]::NewGuid().ToString("N")))
$stage = Join-Path $stageRoot "python"
New-Item -ItemType Directory -Path $stage -Force | Out-Null

function Copy-RuntimeTree([string]$Relative, [switch]$StandardLibrary) {
    $from = Join-Path $source $Relative
    if (-not (Test-Path -LiteralPath $from -PathType Container)) { return }
    Get-ChildItem -LiteralPath $from -File -Recurse -Force | ForEach-Object {
        $tail = $_.FullName.Substring($from.Length).TrimStart('\')
        if ($tail -match '(^|\\)(__pycache__|\.pytest_cache)(\\|$)' -or $_.Extension -in @('.pyc', '.pyo')) { return }
        if ($StandardLibrary -and $tail -match '^site-packages(\\|$)') { return }
        $destination = Join-Path (Join-Path $stage $Relative) $tail
        if (($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Runtime source contains a symbolic link: $($_.FullName)" }
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $destination
    }
}

foreach ($name in @("python.exe", "pythonw.exe", "python3.dll", "python312.dll", "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt")) {
    Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $stage $name)
}
Copy-RuntimeTree "Lib" -StandardLibrary
Copy-RuntimeTree "DLLs"
foreach ($name in @("tcl8", "tcl8.6", "tk8.6", "dde1.4", "reg1.3", "tix8.4.3")) {
    Copy-RuntimeTree ("tcl\" + $name)
}
Copy-RuntimeTree "Lib\site-packages\PIL"
$pillowMetadata = @(Get-ChildItem -LiteralPath (Join-Path $source "Lib\site-packages") -Directory -Filter "pillow-*.dist-info")
if ($pillowMetadata.Count -ne 1) { throw "Expected exactly one Pillow package metadata directory." }
Copy-RuntimeTree ("Lib\site-packages\" + $pillowMetadata[0].Name)

$utf8NoBom = New-Object Text.UTF8Encoding($false)
$searchPath = ".`r`nDLLs`r`nLib`r`nLib\site-packages`r`n..\..\src`r`nimport site`r`n"
[IO.File]::WriteAllText((Join-Path $stage "python312._pth"), $searchPath, $utf8NoBom)
$probe = & (Join-Path $stage "python.exe") -I -B -X utf8 -c "import json,sys,tkinter,PIL; print(json.dumps({'python':sys.version.split()[0],'pillow':PIL.__version__,'tk':tkinter.TkVersion,'isolated':bool(sys.flags.isolated),'x64':sys.maxsize>2**32}))"
if ($LASTEXITCODE -ne 0) { throw "The staged private runtime failed its import check." }
$info = $probe | ConvertFrom-Json
if ($info.python -notlike '3.12.*' -or -not $info.isolated -or -not $info.x64) { throw "Expected isolated 64-bit CPython 3.12." }
$notice = @"
# Bundled runtime

PNG Mockup includes a private CPython $($info.python) runtime with Tcl/Tk $($info.tk), Pillow $($info.pillow), and oxipng.
Only the standard library, Tcl/Tk runtime, and Pillow are included; unrelated third-party packages and build caches were omitted.
Relative python312._pth paths isolate this runtime from machine Python installations and user site packages.

Original license notices are retained at:
- runtime/python/LICENSE.txt (Python and bundled runtime notices)
- runtime/python/tcl/tk8.6/license.terms (Tcl/Tk)
- runtime/python/Lib/site-packages/$($pillowMetadata[0].Name)/licenses/LICENSE (Pillow and bundled image libraries)
- tools/oxipng/LICENSE

The runtime is distributed without changing the component binaries. Do not remove their accompanying license notices.
"@
[IO.File]::WriteAllText((Join-Path $stage "BUNDLED_RUNTIME.md"), $notice, $utf8NoBom)
New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
if (Test-Path -LiteralPath $target) {
    $backup = Assert-WorkspacePath (Join-Path $stageRoot "previous-runtime")
    [void](Assert-WorkspacePath $target)
    Move-Item -LiteralPath $target -Destination $backup
}
[void](Assert-WorkspacePath $stage)
[void](Assert-WorkspacePath $target)
Move-Item -LiteralPath $stage -Destination $target
& (Join-Path $target "python.exe") -I -B -X utf8 (Join-Path $appRoot "tools\runtime_check.py")
if ($LASTEXITCODE -ne 0) { throw "The installed private runtime failed relocation verification." }
Write-Host "Prepared private runtime: $target"
