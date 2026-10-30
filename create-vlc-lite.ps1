$ErrorActionPreference = 'Stop'

$source = Join-Path $PSScriptRoot 'VLC'
$destination = Join-Path $PSScriptRoot 'VLC-lite'

if (-not (Test-Path (Join-Path $source 'libvlc.dll'))) {
    throw "VLC source folder was not found: $source"
}

$transformPlugin = Join-Path $source 'plugins/video_filter/libtransform_plugin.dll'
if (-not (Test-Path -LiteralPath $transformPlugin -PathType Leaf)) {
    throw "VLC video transform plugin was not found: $transformPlugin"
}

# This program uses libVLC only for silent, local video wallpaper playback.
# Retain all modules in the selected playback-related categories so libVLC can
# fall back safely for different local files and Windows GPU configurations.
# Selected categories still include some audio, network, disc, and subtitle
# modules; categories not listed below are omitted.
$rootFiles = @(
    'libvlc.dll',
    'libvlccore.dll',
    'vlc-cache-gen.exe',
    'AUTHORS.txt',
    'COPYING.txt',
    'NEWS.txt',
    'README.txt',
    'THANKS.txt'
)

$pluginCategories = @(
    'access',
    'codec',
    'd3d11',
    'd3d9',
    'demux',
    'packetizer',
    'video_chroma',
    'video_output'
)

if (Test-Path $destination) {
    Remove-Item -LiteralPath $destination -Recurse -Force
}

New-Item -ItemType Directory -Path $destination | Out-Null

foreach ($file in $rootFiles) {
    Copy-Item -LiteralPath (Join-Path $source $file) -Destination $destination
}

foreach ($category in $pluginCategories) {
    $targetDirectory = Join-Path $destination (Join-Path 'plugins' $category)
    New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
    Get-ChildItem -LiteralPath (Join-Path (Join-Path $source 'plugins') $category) -File |
        Copy-Item -Destination $targetDirectory
}

# Preserve video orientation in thumbnails.
$filterDirectory = Join-Path $destination 'plugins/video_filter'
New-Item -ItemType Directory -Path $filterDirectory -Force | Out-Null
Copy-Item -LiteralPath $transformPlugin -Destination $filterDirectory

& (Join-Path $destination 'vlc-cache-gen.exe') (Join-Path $destination 'plugins')
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to generate VLC plugin cache.'
}

$sourceBytes = (Get-ChildItem $source -Recurse -File | Measure-Object Length -Sum).Sum
$liteBytes = (Get-ChildItem $destination -Recurse -File | Measure-Object Length -Sum).Sum
Write-Host ("Created VLC-lite: {0:N1} MB (source: {1:N1} MB, reduction: {2:N1}%)" -f ($liteBytes / 1MB), ($sourceBytes / 1MB), (100 * (1 - $liteBytes / $sourceBytes)))
