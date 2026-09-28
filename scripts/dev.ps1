# dev.ps1 - run the app from source.
# NOTE: keep this file ASCII-only. Windows PowerShell 5.1 mis-decodes UTF-8 .ps1 without BOM.
#
#   .\scripts\dev.ps1              start GUI
#   .\scripts\dev.ps1 -SelfTest    run headless self-check
#   .\scripts\dev.ps1 -Modules     list parsed modules (syntax check only)

param(
    [switch]$SelfTest,
    [switch]$Modules
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Resolve-Python {
    if ($env:JEV_PY -and (Test-Path $env:JEV_PY)) { return $env:JEV_PY }
    # Reuse the already-provisioned venv (PySide6 / windows-capture / rapidocr / pyinstaller).
    $sibling = Join-Path (Split-Path -Parent $root) 'jev-chat-src\.venv\Scripts\python.exe'
    if (Test-Path $sibling) { return $sibling }
    $mine = Join-Path $root '.venv\Scripts\python.exe'
    if (Test-Path $mine) { return $mine }
    throw 'No python found. Set $env:JEV_PY or create .venv per requirements.txt'
}

$py = Resolve-Python
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
Write-Host "python: $py"

if ($Modules) {
    Get-ChildItem -Recurse -Filter *.py app | ForEach-Object {
        $f = $_.FullName
        & $py -c "import ast,sys; ast.parse(open(sys.argv[1],encoding='utf-8').read())" $f
        if ($LASTEXITCODE -ne 0) { throw "syntax error: $f" }
        Write-Host "  ok $($_.Name)"
    }
    Write-Host 'all modules parsed'
    exit 0
}

if ($SelfTest) {
    & $py -m app.main --selftest
    exit $LASTEXITCODE
}

& $py -m app.main
exit $LASTEXITCODE
