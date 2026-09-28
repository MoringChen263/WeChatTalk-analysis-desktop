<#
    make_release.ps1 -- turn the PyInstaller output into something you can hand
    to another person.

    Why a separate script instead of "just zip the folder":
      1. A one-dir build is *not* self-contained as a single .exe. Whoever gets it
         needs the whole tree. Shipping a zip whose root is the app folder means
         they unzip once and double-click -- no instructions required.
      2. The build output (dist\) is a build artifact and must stay clean. This
         script stages a copy, adds the docs/licenses, and zips the copy.
      3. Most important: it refuses to produce a zip that contains YOUR data. A
         release that silently carries config.json (which holds the DPAPI blob for
         your API key) or a memory sqlite3 file would leak the sender's key and
         chat history to whoever receives it. That check is an assertion here, not
         a checklist item, because "remember to check" fails eventually.

    Notes / constraints:
      - ASCII only. PowerShell 5.1 reads a BOM-less .ps1 as ANSI, so any non-ASCII
        literal here would be silently mis-decoded (this bit us in build.ps1 before).
        All Chinese lives in the packaging\ files, which are copied verbatim.
      - Never deletes anything: the sandbox denies deletes, and this script should
        not depend on being allowed to delete. Old staging dirs are renamed.
      - Output zip name carries a timestamp, so re-runs never collide.

    Usage:
        powershell -File scripts\make_release.ps1
        powershell -File scripts\make_release.ps1 -SkipZip     # stage only, fast
#>
param(
    [switch]$SkipZip
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$appName  = 'jev-chat-analyzer'
$distDir  = Join-Path $root "dist\$appName"
$relDir   = Join-Path $root 'release'
$packDir  = Join-Path $root 'packaging'
$stamp    = Get-Date -Format 'yyyyMMdd-HHmmss'

Write-Host '== 1. check the build output'
$exe = Join-Path $distDir "$appName.exe"
if (-not (Test-Path $exe)) {
    throw "no exe at $exe -- run scripts\build.ps1 first"
}
$inner = Join-Path $distDir '_internal'
if (-not (Test-Path $inner)) {
    throw "no _internal at $inner -- this is not a one-dir build"
}

# Payload checks. A missing skill/OCR model still starts and still shows a UI, so
# without these the release would look fine and only fail inside the receiver's
# hands -- at the moment they first try the feature that needs it.
Write-Host '== 2. payload check'
$must = @(
    "_internal\skills\goutoujunshi\SKILL.md",
    "_internal\skills\goutoujunshi\scripts\memory_store.py"
)
foreach ($m in $must) {
    $p = Join-Path $distDir $m
    if (Test-Path $p) { Write-Host "   OK      $m" } else { throw "missing payload: $m" }
}
function Find-Count($dir, $pattern) {
    return @(Get-ChildItem $dir -Recurse -Filter $pattern -ErrorAction SilentlyContinue).Count
}
$nOnnx = Find-Count $distDir '*.onnx'
$nPyd  = Find-Count $distDir 'cv2*.pyd'
Write-Host "   OCR models (*.onnx) : $nOnnx"
Write-Host "   cv2 binary (*.pyd)  : $nPyd"
if ($nOnnx -lt 3) { throw "expected >=3 onnx models, found $nOnnx" }
if ($nPyd -lt 1)  { throw "cv2 binary not bundled" }

Write-Host '== 3. stage a copy (dist\ stays a pure build artifact)'
New-Item -ItemType Directory -Path $relDir -Force | Out-Null
$stageRoot = Join-Path $relDir '_staging'
if (Test-Path $stageRoot) {
    # Rename instead of delete: works without delete permission, and keeps the
    # previous stage around in case the new one turns out broken.
    $prev = Join-Path $relDir "_staging_prev_$stamp"
    Move-Item $stageRoot $prev
    Write-Host "   previous staging kept as $(Split-Path -Leaf $prev)"
}
New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null
$stageApp = Join-Path $stageRoot $appName
New-Item -ItemType Directory -Path $stageApp -Force | Out-Null

Write-Host "   copying $distDir ..."
$rc = (robocopy $distDir $stageApp /E /NFL /NDL /NJH /NJS /NP /R:1 /W:1)
if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
$LASTEXITCODE = 0

Write-Host '== 4. add the reader-facing files'
# Everything in packaging\ is copied, so filenames may be Chinese without this
# script ever containing a non-ASCII literal.
if (Test-Path $packDir) {
    Get-ChildItem $packDir -File | ForEach-Object {
        Copy-Item $_.FullName (Join-Path $stageApp $_.Name) -Force
        Write-Host "   + $($_.Name)"
    }
} else {
    Write-Host "   (no packaging\ dir -- release will have no instructions)"
}
# License compliance: MIT requires the notice to travel with the binaries, and
# THIRD_PARTY_NOTICES lists everything else that is bundled.
foreach ($f in @('LICENSE', 'THIRD_PARTY_NOTICES.md')) {
    $p = Join-Path $root $f
    if (Test-Path $p) {
        Copy-Item $p (Join-Path $stageApp $f) -Force
        Write-Host "   + $f"
    }
}
# build.ps1 writes the full third-party list into dist\; keep it next to the exe.
$tp = Join-Path $root 'dist\THIRD_PARTY_LICENSES.txt'
if (Test-Path $tp) {
    Copy-Item $tp (Join-Path $stageApp 'THIRD_PARTY_LICENSES.txt') -Force
    Write-Host '   + THIRD_PARTY_LICENSES.txt'
}

Write-Host '== 4b. normalise top-level text encoding'
# The docs are written as BOM-less UTF-8. Windows 11 Notepad sniffs that fine, but
# older Notepad (and several zip viewers' preview pane) decode it as ANSI and show
# mojibake -- for a Chinese README that means the receiver sees garbage on first
# open, which is exactly the wrong first impression. Re-save with a BOM.
# Top level only: never touch bundled third-party files.
$utf8bom = New-Object System.Text.UTF8Encoding($true)
Get-ChildItem $stageApp -File | Where-Object { $_.Extension -in @('.txt', '.md') } | ForEach-Object {
    $text = Get-Content $_.FullName -Raw -Encoding UTF8
    [System.IO.File]::WriteAllText($_.FullName, $text, $utf8bom)
    Write-Host "   utf-8 BOM: $($_.Name)"
}

Write-Host '== 5. privacy gate: the release must not carry your data'
# These are the exact artifacts that hold the sender's secrets/history. If any of
# them made it into the package, stop -- do not hand out a zip that leaks a key.
$leakPatterns = @('*.sqlite3', 'selftest.txt', 'ocr_check.json', 'llm-cost-*.json')
$leaks = @()
foreach ($pat in $leakPatterns) {
    $leaks += @(Get-ChildItem $stageApp -Recurse -Filter $pat -ErrorAction SilentlyContinue)
}
$leaks += @(Get-ChildItem $stageApp -Recurse -Filter 'config.json' -ErrorAction SilentlyContinue | Where-Object {
    $_.DirectoryName -eq $stageApp -or $_.DirectoryName -eq (Join-Path $stageApp '_internal')
})
$logsDir = Join-Path $stageApp 'logs'
if (Test-Path $logsDir) { $leaks += @(Get-Item $logsDir) }
$leaks = @($leaks | Where-Object { $_ })
if ($leaks.Count -gt 0) {
    Write-Host '!! refused to package: these look like local/private data'
    $leaks | ForEach-Object { Write-Host "     $($_.FullName)" }
    throw 'privacy gate failed -- remove the files above from dist\ and rebuild'
}
Write-Host '   clean: no config, no credential blob, no memory db, no logs'

$stageMb = [math]::Round(((Get-ChildItem $stageApp -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "   staged size: $stageMb MB"

if ($SkipZip) {
    Write-Host ''
    Write-Host "== staged at $stageApp (-SkipZip: no zip produced)"
    exit 0
}

Write-Host '== 6. zip it (CompressionLevel Optimal; a few minutes for ~300 MB)'
$verLine = Select-String -Path (Join-Path $root 'app\version.py') -Pattern '__version__\s*=\s*"([^"]+)"'
$ver = if ($verLine) { $verLine.Matches[0].Groups[1].Value } else { '0.0.0' }
$zipName = "$appName-v$ver-win64-$stamp.zip"
$zipPath = Join-Path $relDir $zipName

Add-Type -AssemblyName System.IO.Compression.FileSystem
# includeBaseDirectory = $true so the archive root is the app folder: the receiver
# unzips and gets one clean folder, not 300 loose files in their Downloads.
[System.IO.Compression.ZipFile]::CreateFromDirectory(
    $stageApp, $zipPath,
    [System.IO.Compression.CompressionLevel]::Optimal,
    $true)
if (-not (Test-Path $zipPath)) { throw 'zip was not produced' }

$zipMb  = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
$sha    = (Get-FileHash $zipPath -Algorithm SHA256).Hash
$ratio  = [math]::Round(100 * $zipMb / $stageMb, 0)

Write-Host ''
Write-Host '== release ready'
Write-Host "   version      : $ver"
Write-Host "   archive      : $zipPath"
Write-Host "   archive size : $zipMb MB  (from $stageMb MB, ~$ratio%)"
Write-Host "   sha256       : $sha"
Write-Host ''
Write-Host '   send that single zip. The receiver unzips it anywhere, reads'
Write-Host '   README-FIRST, and double-clicks the exe -- no Python needed.'
Write-Host "   sidebar: $appName.exe is useless without its _internal\ folder."
