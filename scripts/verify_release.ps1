<#
    verify_release.ps1 -- prove a release zip actually works, the way the receiver
    will experience it.

    This is deliberately NOT a re-run of build-time checks. Those run against
    dist\, inside the developer's tree, with the developer's environment. The
    question this script answers is different: "I emailed this zip to someone --
    will it work on their machine?" So it:

      1. extracts the archive into a clean directory (nothing inherited),
      2. asserts the archive root is the app folder (not 300 loose files),
      3. re-runs the privacy gate on the extracted tree,
      4. runs the packaged exe with a *private, empty* data dir, so it behaves
         like a first launch on a stranger's PC,
      5. judges pass/fail by EXIT CODE, never by matching text in the report.
         Text matching is what silently broke build.ps1 before: the script is
         read as ANSI, so a Chinese pattern never matched and a green build was
         reported as FAIL. Exit codes have no encoding.

    Usage:
        powershell -File scripts\verify_release.ps1 -Zip release\jev-chat-analyzer-v0.1.0-win64-XXXX.zip
#>
param(
    [Parameter(Mandatory = $true)][string]$Zip
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path $Zip)) { throw "no such archive: $Zip" }
$Zip = (Resolve-Path $Zip).Path
$zipName = Split-Path -Leaf $Zip
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$work = Join-Path $root "release\_verify\$stamp"
New-Item -ItemType Directory -Path $work -Force | Out-Null

Write-Host "== verifying $zipName"
$zipMb = [math]::Round((Get-Item $Zip).Length / 1MB, 1)
$sha = (Get-FileHash $Zip -Algorithm SHA256).Hash
Write-Host "   size   : $zipMb MB"
Write-Host "   sha256 : $sha"

Write-Host "== 1. extract into a clean directory"
Write-Host "   -> $work"
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::ExtractToDirectory($Zip, $work)

Write-Host '== 2. archive layout'
$top = @(Get-ChildItem $work)
$dirs = @($top | Where-Object { $_.PSIsContainer })
$files = @($top | Where-Object { -not $_.PSIsContainer })
foreach ($d in $dirs) { Write-Host "   [dir]  $($d.Name)" }
foreach ($f in $files) { Write-Host "   [file] $($f.Name)" }
$appDir = $null
foreach ($d in $dirs) {
    if (Test-Path (Join-Path $d.FullName 'jev-chat-analyzer.exe')) { $appDir = $d.FullName }
}
if (-not $appDir) { throw 'no top-level folder containing jev-chat-analyzer.exe' }
if ($files.Count -gt 0) { Write-Host "   note: $($files.Count) loose file(s) at root" }
Write-Host "   app dir: $(Split-Path -Leaf $appDir)"

Write-Host '== 3. privacy gate on the extracted tree'
$leakPatterns = @('*.sqlite3', 'selftest.txt', 'ocr_check.json', 'llm-cost-*.json', 'config.json')
$leaks = @()
foreach ($pat in $leakPatterns) {
    $leaks += @(Get-ChildItem $appDir -Recurse -Filter $pat -ErrorAction SilentlyContinue)
}
if (Test-Path (Join-Path $appDir 'logs')) { $leaks += @(Get-Item (Join-Path $appDir 'logs')) }
$leaks = @($leaks | Where-Object { $_ })
if ($leaks.Count -gt 0) {
    Write-Host '!! PRIVACY FAIL: private data inside the archive'
    $leaks | ForEach-Object { Write-Host "     $($_.FullName)" }
    exit 1
}
Write-Host '   clean'

Write-Host '== 4. run the packaged exe like a first-time user'
$exe = Join-Path $appDir 'jev-chat-analyzer.exe'
# A private, empty data dir = exactly what happens on someone else's PC where
# %APPDATA%\jev-chat-analyzer does not exist yet.
$data = Join-Path $work '_data'
New-Item -ItemType Directory -Path $data -Force | Out-Null
$oldData = $env:JEV_DATA_DIR
$env:JEV_DATA_DIR = $data
$results = @()
try {
    foreach ($mode in @('--selftest', '--ocr-check')) {
        $p = Start-Process -FilePath $exe -ArgumentList $mode -Wait -PassThru
        $rc = $p.ExitCode
        $verdict = if ($rc -eq 0) { 'PASS' } else { 'FAIL' }
        Write-Host "   $mode -> exit $rc  $verdict"
        $results += [pscustomobject]@{ Mode = $mode; Rc = $rc; Verdict = $verdict }
    }
} finally {
    if ($oldData) { $env:JEV_DATA_DIR = $oldData } else { Remove-Item Env:\JEV_DATA_DIR -ErrorAction SilentlyContinue }
}

Write-Host '== 5. what the exe produced'
foreach ($f in @('selftest.txt', 'ocr_check.json')) {
    $p = Join-Path $data $f
    if (Test-Path $p) {
        $size = (Get-Item $p).Length
        Write-Host "   $f ($size bytes)"
    } else {
        Write-Host "   $f MISSING (the exe died before writing it)"
    }
}
$log = Join-Path $data 'logs\app.log'
if (Test-Path $log) {
    Write-Host '   --- app.log tail ---'
    Get-Content $log -Encoding UTF8 | Select-Object -Last 6 | ForEach-Object { Write-Host "   $_" }
}

$bad = @($results | Where-Object { $_.Verdict -ne 'PASS' })
Write-Host ''
if ($bad.Count -eq 0) {
    Write-Host '== RELEASE OK: unzips to a self-contained folder and the packaged exe'
    Write-Host '   passes both diagnostic entry points from a clean data dir.'
    Write-Host "   (extracted copy left at $work)"
    exit 0
} else {
    Write-Host '== RELEASE CHECK FAILED'
    $bad | ForEach-Object { Write-Host "   $($_.Mode) exit $($_.Rc)" }
    exit 1
}
