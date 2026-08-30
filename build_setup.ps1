[CmdletBinding()]
param([switch]$LauncherOnly, [switch]$StageOnly)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$projectPrefix = $projectRoot.TrimEnd('\') + '\'

function Assert-ProjectPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($projectPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing a build path outside this project: $full"
    }
    $current = $full
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "Build paths cannot pass through junctions or symbolic links: $current"
            }
        }
        $current = [IO.Path]::GetDirectoryName($current)
    }
    return $full
}

$appRoot = Assert-ProjectPath (Join-Path $projectRoot 'App Source')
$python = Assert-ProjectPath (Join-Path $appRoot 'runtime\python\python.exe')
$version = Get-Content -LiteralPath (Join-Path $appRoot 'version.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($version.product_id -ne 'png-mockup' -or $version.name -ne 'PNG Mockup' -or $version.version -notmatch '^\d+\.\d+\.\d+$') {
    throw 'Application product identity/version is invalid.'
}
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'The private runtime is missing. Prepare App Source/runtime/python before building.'
}
$compilerRoot = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319'
$compiler = Join-Path $compilerRoot 'csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) {
    throw 'The Windows .NET Framework 4 C# compiler is required to build this offline installer.'
}
$stage = Assert-ProjectPath (Join-Path $projectRoot ('build\setup-' + [Guid]::NewGuid().ToString('N')))
New-Item -ItemType Directory -Path $stage -Force | Out-Null
$utf8 = New-Object Text.UTF8Encoding($false)
$manifestPath = Join-Path $stage 'application.manifest'
$manifest = @'
<?xml version="1.0" encoding="utf-8"?>
<assembly manifestVersion="1.0" xmlns="urn:schemas-microsoft-com:asm.v1">
  <assemblyIdentity version="1.0.0.0" name="png-mockup" />
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>
    <requestedExecutionLevel level="asInvoker" uiAccess="false" />
  </requestedPrivileges></security></trustInfo>
  <compatibility xmlns="urn:schemas-microsoft-com:compatibility.v1"><application>
    <supportedOS Id="{8e0f7a12-bfb3-4fe8-b9a5-48fd50a15a9a}" />
  </application></compatibility>
  <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
    <dpiAware xmlns="http://schemas.microsoft.com/SMI/2005/WindowsSettings">true</dpiAware>
  </windowsSettings></application>
</assembly>
'@
[IO.File]::WriteAllText($manifestPath, $manifest, $utf8)

function Compile-Program([string]$Source, [string]$Destination, [string]$Title, [string]$Icon = 'assets\app_icon.ico') {
    [void](Assert-ProjectPath $Source)
    [void](Assert-ProjectPath $Destination)
    $infoPath = Join-Path $stage (([IO.Path]::GetFileNameWithoutExtension($Destination)) + '.AssemblyInfo.cs')
    $assemblyVersion = [string]$version.version + '.0'
    $assemblyInfo = @"
using System.Reflection;
[assembly: AssemblyTitle("$Title")]
[assembly: AssemblyDescription("Independent offline PNG/PSD application")]
[assembly: AssemblyProduct("PNG Mockup")]
[assembly: AssemblyCompany("PNG Mockup")]
[assembly: AssemblyVersion("$assemblyVersion")]
[assembly: AssemblyFileVersion("$assemblyVersion")]
[assembly: AssemblyInformationalVersion("$($version.version)")]
[assembly: AssemblyMetadata("ProductId", "png-mockup")]
"@
    [IO.File]::WriteAllText($infoPath, $assemblyInfo, $utf8)
    $iconPath = Assert-ProjectPath (Join-Path $appRoot $Icon)
    if (-not (Test-Path -LiteralPath $iconPath -PathType Leaf)) { throw "Program icon is missing: $iconPath" }
    $compilerArgs = @('/nologo', '/target:winexe', '/platform:x64', '/optimize+', '/codepage:65001',
        "/out:$Destination", "/win32icon:$iconPath", "/win32manifest:$manifestPath",
        '/reference:System.dll', '/reference:System.Core.dll', '/reference:System.Drawing.dll',
        '/reference:System.Windows.Forms.dll', '/reference:System.Web.Extensions.dll',
        '/reference:System.IO.Compression.dll', '/reference:System.IO.Compression.FileSystem.dll',
        $Source, (Join-Path $appRoot 'tools\product_support.cs'), $infoPath)
    & $compiler @compilerArgs
    if ($LASTEXITCODE -ne 0) { throw "Compilation failed: $Title" }
}

$launcher = Join-Path $stage 'PNG Mockup.exe'
Compile-Program (Join-Path $appRoot 'tools\portable_launcher.cs') $launcher 'PNG Mockup'
Copy-Item -LiteralPath $launcher -Destination (Join-Path $appRoot 'PNG Mockup.exe') -Force
foreach ($helper in @(
    @{ Source = 'uninstall.cs'; Name = 'Uninstall.exe'; Title = 'Uninstall PNG Mockup'; Icon = 'assets\uninstall_icon.ico' },
    @{ Source = 'technical_visibility.cs'; Name = 'Show technical files.exe'; Title = 'PNG Mockup technical files'; Icon = 'assets\technical_files_icon.ico' }
)) {
    $helperPath = Join-Path $stage $helper.Name
    Compile-Program (Join-Path $appRoot ('tools\' + $helper.Source)) $helperPath $helper.Title $helper.Icon
    Copy-Item -LiteralPath $helperPath -Destination (Join-Path $appRoot $helper.Name) -Force
}
& $python -I -B -X utf8 (Join-Path $appRoot 'tools\runtime_check.py')
if ($LASTEXITCODE -ne 0) { throw 'The private runtime failed verification.' }
$launcherReport = Join-Path $stage 'launcher-verification.json'
$launchArgs = '/self-test /report "' + $launcherReport + '"'
$launchProbe = Start-Process -FilePath (Join-Path $appRoot 'PNG Mockup.exe') -ArgumentList $launchArgs -WindowStyle Hidden -PassThru -Wait
if ($launchProbe.ExitCode -ne 0) { throw "Launcher verification failed. See $launcherReport" }
$verifiedLauncher = Get-Content -LiteralPath $launcherReport -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $verifiedLauncher.ok -or -not $verifiedLauncher.isolated -or -not $verifiedLauncher.private_paths_only) {
    throw 'The launcher did not confirm the isolated private runtime.'
}
Write-Host "Launcher ready: $(Join-Path $appRoot 'PNG Mockup.exe')"
if ($LauncherOnly) { return }

$stub = Join-Path $stage 'installer-stub.exe'
Compile-Program (Join-Path $projectRoot 'installer\SetupInstaller.cs') $stub 'PNG Mockup Setup'
$payloadArgs = @('--stage', $stage)
if ($StageOnly) { $payloadArgs += '--stage-only' }
& $python -I -B -X utf8 (Join-Path $projectRoot 'tools\build_installer_payload.py') @payloadArgs
if ($LASTEXITCODE -ne 0) { throw 'Building or verifying the offline payload failed.' }
if ($StageOnly) {
    Write-Host "Temporary installer for testing: $(Join-Path $stage 'PNG Mockup Setup.exe')"
    return
}
Write-Host "Installer ready: $(Join-Path $projectRoot 'PNG Mockup Setup.exe')"
Write-Host "SHA-256 file: $(Join-Path $projectRoot 'PNG Mockup Setup.exe.sha256')"
Write-Host "Build audit: $stage"
