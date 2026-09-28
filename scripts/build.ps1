# build.ps1 - PyInstaller one-dir build, mirroring jev-chat-windows-v0.1.9.
# NOTE: keep this file ASCII-only (PowerShell 5.1 + UTF-8 without BOM issue).
#
#   .\scripts\build.ps1                 build + verify
#   .\scripts\build.ps1 -SkipValidate   skip skill validation
#   .\scripts\build.ps1 -SkipSmoke      skip the packaged-exe selftest
#
# Output: dist\jev-chat-analyzer\jev-chat-analyzer.exe (+ _internal\)

param(
    [switch]$SkipValidate,
    [switch]$SkipSmoke,
    [int]$SizeBudgetMB = 400
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Resolve-Python {
    if ($env:JEV_PY -and (Test-Path $env:JEV_PY)) { return $env:JEV_PY }
    $sibling = Join-Path (Split-Path -Parent $root) 'jev-chat-src\.venv\Scripts\python.exe'
    if (Test-Path $sibling) { return $sibling }
    $mine = Join-Path $root '.venv\Scripts\python.exe'
    if (Test-Path $mine) { return $mine }
    throw 'No python found.'
}

$py = Resolve-Python
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

# ---- 1. sync skill from the single source of truth (no git in that folder) ----
$skillSrc = 'D:\Jev\goutoujunshi'
$skillDst = Join-Path $root 'skills\goutoujunshi'
if (Test-Path $skillSrc) {
    Write-Host "== sync skill: $skillSrc -> $skillDst"
    robocopy $skillSrc $skillDst /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
} else {
    Write-Host "!! skill source not found: $skillSrc (keep existing copy)"
}

# ---- 2. validate the skill before packaging ----
if (-not $SkipValidate) {
    $validator = Join-Path $skillDst 'scripts\validate_skill.py'
    if (Test-Path $validator) {
        Write-Host '== validate skill'
        & $py $validator --runtime
        if ($LASTEXITCODE -ne 0) { throw "validate_skill.py failed ($LASTEXITCODE)" }
    } else {
        Write-Host "!! validator missing: $validator"
    }
}

# ---- 3. self-check ----
Write-Host '== selftest'
& $py -m app.main --selftest
if ($LASTEXITCODE -ne 0) { throw 'selftest failed' }

# ---- 3b. teach PyInstaller about the imports hiding inside the bundled skill ----
# E40: skills/ ships as *data* (--add-data), so PyInstaller's module graph cannot see
# anything it imports. Official memory_store.py needs sqlite3 and uuid, and nothing in
# app/ imports either -- so without this the packaged app dies with
# "No module named 'sqlite3'" the first time you touch long-term memory, while a source
# run works fine forever. Scanning (instead of a hand-written list) means a skill upgrade
# that pulls in a new stdlib module cannot silently break the build.
Write-Host '== scan skill imports (they ship as data, so PyInstaller cannot see them)'
$skillMods = @()
$scanner = Join-Path $root 'scripts\scan_skill_imports.py'
if (Test-Path $scanner) {
    $found = (& $py $scanner $skillDst 2>$null | Select-Object -First 1)
    if ($found) {
        $skillMods = @($found -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    }
    Write-Host "   $($skillMods.Count) modules: $($skillMods -join ', ')"
} else {
    Write-Host "!! scanner missing: $scanner"
}

# ---- 4. PyInstaller one-dir ----
Write-Host '== pyinstaller'
$sep = ';'  # Windows path separator for --add-data
$pyiArgs = @(
    '-m', 'PyInstaller',
    '--noconfirm', '--clean',
    '--windowed',
    '--name', 'jev-chat-analyzer',
    # E37: --add-data copies the *contents* of the source dir, so "skills;." would land
    # the skill at _internal/goutoujunshi/ and app/paths.py (which looks for
    # _internal/skills/) would not find it. The destination must repeat the dir name.
    '--add-data', "skills$($sep)skills",
    '--collect-submodules', 'app',
    # NOT --collect-submodules qfluentwidgets: that drags in qfluentwidgets.multimedia,
    # which pulls PySide6.QtMultimedia + Qt Qml/Quick/Pdf + ffmpeg (avcodec/avformat).
    # We only use FluentWindow/labels/buttons/InfoBar, so plain import analysis is enough.
    '--collect-data', 'qfluentwidgets',
    '--collect-submodules', 'rapidocr_onnxruntime',
    '--collect-data', 'rapidocr_onnxruntime',
    '--hidden-import', 'onnxruntime',
    '--hidden-import', 'cv2',
    '--hidden-import', 'windows_capture',
    # Explicit belt-and-braces: if the scanner above ever fails, missing sqlite3 is the
    # one failure that would take the whole memory feature down. Keep this line.
    '--hidden-import', 'sqlite3',
    '--exclude-module', 'torch',
    '--exclude-module', 'transformers',
    '--exclude-module', 'laya',
    '--exclude-module', 'matplotlib',
    '--exclude-module', 'pandas',
    '--exclude-module', 'tkinter',
    # E39: numpy.f2py is a Fortran build helper that nothing loads at runtime, but it
    # imports scipy -- so the whole of scipy (68 MB incl. scipy.libs) was shipping.
    '--exclude-module', 'numpy.f2py',
    '--exclude-module', 'scipy',
    '--exclude-module', 'tensorflow',
    # Belt and braces for the multimedia chain (see the note above).
    '--exclude-module', 'qfluentwidgets.multimedia',
    '--exclude-module', 'PySide6.QtMultimedia',
    '--exclude-module', 'PySide6.QtMultimediaWidgets'
)
foreach ($m in $skillMods) { $pyiArgs += @('--hidden-import', $m) }
$pyiArgs += 'app\main.py'

# E39: opencv-python bundles opencv_videoio_ffmpeg*.dll (~29 MB) for video decode, and
# PyInstaller has no "exclude this binary" flag. Deleting it out of dist/ afterwards is
# fragile (sandbox rules, AV, locked files all can refuse), so instead we rename it inside
# site-packages for the duration of the build and restore it in finally -- a rename is
# atomic, never touches the file's contents, and cannot half-fail.
# If cv2.pyd ever needed it at import time, the OCR smoke test in step 7 would fail loudly.
$stashed = @()
try {
    $cv2Dir = (& $py -c "import os, cv2; print(os.path.dirname(cv2.__file__))" 2>$null |
                Select-Object -First 1)
    if ($cv2Dir -and (Test-Path $cv2Dir)) {
        Get-ChildItem $cv2Dir -Filter 'opencv_videoio_ffmpeg*.dll' -ErrorAction SilentlyContinue | ForEach-Object {
            $stash = $_.FullName + '.pyi-stash'
            $orig = $_.FullName
            $mbF = [math]::Round($_.Length / 1MB, 1)
            Move-Item $orig $stash -Force
            $stashed += [pscustomobject]@{ Stash = $stash; Original = $orig }
            Write-Host "   stash $($_.Name) ($mbF MB, video decode - unused, we only pass still frames)"
        }
    }
    if ($stashed.Count -eq 0) { Write-Host '   ffmpeg payload: nothing to stash' }

    & $py @pyiArgs
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed ($LASTEXITCODE)" }
} finally {
    foreach ($s in $stashed) {
        if (Test-Path $s.Stash) { Move-Item $s.Stash $s.Original -Force }
    }
    if ($stashed.Count -gt 0) { Write-Host "   restored $($stashed.Count) stashed file(s) in site-packages" }
}

$outDir = Join-Path $root 'dist\jev-chat-analyzer'

# ---- 5. report size (budget guard) ----
$mb = [math]::Round(((Get-ChildItem $outDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
Write-Host "== package size: $mb MB (budget $SizeBudgetMB MB)"
$sizeReport = @()
$sizeReport += "package total: $mb MB (budget $SizeBudgetMB MB)"
$sizeReport += "_internal breakdown:"
Write-Host '   _internal breakdown:'
Get-ChildItem (Join-Path $outDir '_internal') -Directory | ForEach-Object {
    [pscustomobject]@{
        Name = $_.Name
        MB   = [math]::Round(((Get-ChildItem $_.FullName -Recurse -File |
                                Measure-Object Length -Sum).Sum / 1MB), 1)
    }
} | Sort-Object MB -Descending | Select-Object -First 15 | ForEach-Object {
    Write-Host ("      {0,-26} {1,8} MB" -f $_.Name, $_.MB)
    $sizeReport += ("      {0,-26} {1,8} MB" -f $_.Name, $_.MB)
}
if ($mb -gt $SizeBudgetMB) {
    Write-Host "!! over budget by $([math]::Round($mb - $SizeBudgetMB,1)) MB"
}
Set-Content -Path (Join-Path $root 'dist\SIZE_REPORT.txt') -Value $sizeReport -Encoding UTF8

# ---- 6. sanity check the payload that the app resolves at runtime ----
Write-Host '== payload check'
$inner = Join-Path $outDir '_internal'
$mem = Join-Path $inner 'skills\goutoujunshi\scripts\memory_store.py'
if (Test-Path $mem) { Write-Host "   skill memory_store.py  OK" } else { Write-Host "!! skill memory_store.py MISSING ($mem)" }
$skillMd = Join-Path $inner 'skills\goutoujunshi\SKILL.md'
if (Test-Path $skillMd) { Write-Host "   skill SKILL.md         OK" } else { Write-Host "!! skill SKILL.md MISSING" }
$cv2pyd = Join-Path $inner 'cv2\cv2.pyd'
if (Test-Path $cv2pyd) { Write-Host "   cv2.pyd                OK" } else { Write-Host "!! cv2.pyd MISSING - OCR cannot decode frames" }
# OCR models are the silent killer: the app opens fine and dies on first OCR pass.
$onnx = @(Get-ChildItem $outDir -Recurse -Filter *.onnx -ErrorAction SilentlyContinue)
if ($onnx.Count -gt 0) {
    $onnxMB = [math]::Round((($onnx | Measure-Object Length -Sum).Sum / 1MB), 1)
    Write-Host "   onnx models            OK ($($onnx.Count) files, $onnxMB MB)"
} else {
    Write-Host "!! NO *.onnx FOUND - rapidocr models are missing, OCR will fail at runtime"
}

# ---- 7. smoke test the packaged exe ----
# A --windowed exe has no console, so stdout is gone. The app writes its reports to
# <JEV_DATA_DIR>\selftest.txt / ocr_check.json instead -- that is what we read here.
# These two runs are the real end-to-end proof:
#   * --selftest : inside the exe paths.is_frozen() is true, so the memory layer takes
#                  the in-process path (see app/memory/store.py::_run_inproc) -- E34/E35
#                  are only exercised here, never by a source-tree run.
#   * --ocr-check: proves the onnx models + onnxruntime + cv2 actually made it into the
#                  package. "import rapidocr_onnxruntime" succeeding proves nothing (E38).
if (-not $SkipSmoke) {
    Write-Host '== smoke test packaged exe'
    $exe = Join-Path $outDir 'jev-chat-analyzer.exe'
    # One timestamped dir per run, never deleted. Two reasons: (1) wiping the previous
    # run's output is a filesystem delete, which is exactly the thing that gets refused
    # (sandbox policy, AV, locked files) -- a build should not hinge on being allowed to
    # delete things; (2) it guarantees the reports we read back below were written by
    # *this* run, instead of a stale file from the last one that would fake a PASS.
    $smokeDir = Join-Path $root ("dist\_smoke\" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Path $smokeDir -Force | Out-Null
    Write-Host "   report dir: $smokeDir"

    $oldDataDir = $env:JEV_DATA_DIR
    $env:JEV_DATA_DIR = $smokeDir
    $rcSelf = -1
    $rcOcr = -1
    try {
        $p1 = Start-Process -FilePath $exe -ArgumentList '--selftest' -Wait -PassThru
        $rcSelf = $p1.ExitCode
        $p2 = Start-Process -FilePath $exe -ArgumentList '--ocr-check' -Wait -PassThru
        $rcOcr = $p2.ExitCode
    } finally {
        if ($oldDataDir) { $env:JEV_DATA_DIR = $oldDataDir }
        else { Remove-Item Env:\JEV_DATA_DIR -ErrorAction SilentlyContinue }
    }

    Write-Host "   selftest  exit code: $rcSelf"
    $rep = Join-Path $smokeDir 'selftest.txt'
    if (-not (Test-Path $rep)) {
        Write-Host "!! packaged selftest produced no report at $rep"
        Write-Host '   (the exe died before selftest could write -- check for missing DLLs)'
    } elseif ($rcSelf -eq 0) {
        Write-Host '   packaged selftest : PASS'
    } else {
        Write-Host '   packaged selftest : FAIL'
        # Judge on the exit code, never on a phrase from the report: this file must stay
        # ASCII-only (PowerShell 5.1 reads a BOM-less script as ANSI, so a Chinese literal
        # written here would not match the Chinese text read from the report -- which is
        # exactly the bug that made a fully-passing build report FAIL). Dump the report for
        # diagnosis; the console may render it imperfectly, the file itself is fine.
        Get-Content $rep -Encoding UTF8 | ForEach-Object { Write-Host "     $_" }
    }

    Write-Host "   ocr-check exit code: $rcOcr"
    $ocrRep = Join-Path $smokeDir 'ocr_check.json'
    if (Test-Path $ocrRep) {
        $j = Get-Content $ocrRep -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($j.ok) {
            Write-Host "   packaged OCR      : PASS ($($j.texts.Count) lines: $($j.texts -join ' | '))"
        } else {
            Write-Host "   packaged OCR      : FAIL $($j.error)"
        }
    } else {
        Write-Host "!! no ocr_check.json at $ocrRep"
    }
}

# ---- 8. third-party license bundle ----
Write-Host '== collect third-party licenses'
$lic = Join-Path $root 'dist\THIRD_PARTY_LICENSES.txt'
$lines = @()
$lines += 'jev-chat-analyzer third-party license bundle'
$lines += 'Generated: ' + (Get-Date -Format s)
$lines += ("-" * 60)
$lines += Get-Content (Join-Path $root 'THIRD_PARTY_NOTICES.md') -Encoding UTF8
# bundled skill license
$skillLic = Join-Path $skillDst 'LICENSE'
if (Test-Path $skillLic) {
    $lines += ("-" * 60)
    $lines += 'skills/goutoujunshi/LICENSE'
    $lines += ("-" * 60)
    $lines += Get-Content $skillLic -Encoding UTF8
}
# installed distributions
$lines += ("-" * 60)
$lines += 'pip distributions'
$lines += ("-" * 60)
$lines += (& $py -m pip list --format=freeze)
Set-Content -Path $lic -Value $lines -Encoding UTF8
Write-Host "   -> $lic"

Write-Host '== done'
