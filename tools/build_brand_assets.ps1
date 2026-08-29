[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Source
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$assets = [IO.Path]::GetFullPath((Join-Path $projectRoot 'App Source\assets'))
$sourcePath = [IO.Path]::GetFullPath($Source)
if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
    throw "Logo source does not exist: $sourcePath"
}
if (-not $assets.StartsWith($projectRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Asset destination is outside the PNG Mockup project.'
}

Add-Type -AssemblyName System.Drawing
$image = [Drawing.Image]::FromFile($sourcePath)
try {
    if ($image.Width -ne 512 -or $image.Height -ne 512) {
        throw "The approved logo must be 512 x 512 pixels; received $($image.Width) x $($image.Height)."
    }

    function New-PngBytes([Drawing.Image]$SourceImage, [int]$Size) {
        $bitmap = New-Object Drawing.Bitmap($Size, $Size, [Drawing.Imaging.PixelFormat]::Format32bppArgb)
        try {
            $bitmap.SetResolution(96, 96)
            $graphics = [Drawing.Graphics]::FromImage($bitmap)
            try {
                $graphics.CompositingMode = [Drawing.Drawing2D.CompositingMode]::SourceCopy
                $graphics.CompositingQuality = [Drawing.Drawing2D.CompositingQuality]::HighQuality
                $graphics.InterpolationMode = [Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
                $graphics.PixelOffsetMode = [Drawing.Drawing2D.PixelOffsetMode]::HighQuality
                $graphics.SmoothingMode = [Drawing.Drawing2D.SmoothingMode]::HighQuality
                $graphics.DrawImage($SourceImage, 0, 0, $Size, $Size)
            }
            finally {
                $graphics.Dispose()
            }
            $stream = New-Object IO.MemoryStream
            try {
                $bitmap.Save($stream, [Drawing.Imaging.ImageFormat]::Png)
                return $stream.ToArray()
            }
            finally {
                $stream.Dispose()
            }
        }
        finally {
            $bitmap.Dispose()
        }
    }

    function Write-Ico([string]$Destination, [int[]]$Sizes) {
        $frames = @()
        foreach ($size in $Sizes) {
            $frames += ,(New-PngBytes $image $size)
        }
        $stream = [IO.File]::Open($Destination, [IO.FileMode]::Create, [IO.FileAccess]::Write, [IO.FileShare]::None)
        $writer = New-Object IO.BinaryWriter($stream)
        try {
            $writer.Write([uint16]0)
            $writer.Write([uint16]1)
            $writer.Write([uint16]$Sizes.Count)
            $offset = 6 + (16 * $Sizes.Count)
            for ($index = 0; $index -lt $Sizes.Count; $index++) {
                $size = $Sizes[$index]
                $frame = $frames[$index]
                $writer.Write([byte]$(if ($size -eq 256) { 0 } else { $size }))
                $writer.Write([byte]$(if ($size -eq 256) { 0 } else { $size }))
                $writer.Write([byte]0)
                $writer.Write([byte]0)
                $writer.Write([uint16]1)
                $writer.Write([uint16]32)
                $writer.Write([uint32]$frame.Length)
                $writer.Write([uint32]$offset)
                $offset += $frame.Length
            }
            foreach ($frame in $frames) {
                $writer.Write([byte[]]$frame)
            }
        }
        finally {
            $writer.Dispose()
            $stream.Dispose()
        }
    }

    New-Item -ItemType Directory -Path $assets -Force | Out-Null
    Copy-Item -LiteralPath $sourcePath -Destination (Join-Path $assets 'logo.png') -Force
    Copy-Item -LiteralPath $sourcePath -Destination (Join-Path $assets 'app_icon.png') -Force
    [IO.File]::WriteAllBytes((Join-Path $assets 'favicon.png'), (New-PngBytes $image 64))
    Write-Ico (Join-Path $assets 'app_icon.ico') @(16, 24, 32, 48, 64, 128, 256)
    Write-Ico (Join-Path $assets 'favicon.ico') @(16, 32, 48, 64)
}
finally {
    $image.Dispose()
}

$retiredSvg = Join-Path $assets 'app_icon.svg'
if (Test-Path -LiteralPath $retiredSvg) {
    Remove-Item -LiteralPath $retiredSvg -Force
}

Get-ChildItem -LiteralPath $assets -File | Sort-Object Name | ForEach-Object {
    $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host ("{0}  {1}  {2}" -f $_.Name, $_.Length, $hash)
}
