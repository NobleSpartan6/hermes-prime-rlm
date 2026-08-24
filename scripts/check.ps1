# Local Windows gate. Prefers uv when present; falls back to plain python -m.
$ErrorActionPreference = "Stop"

function Run-Gate {
    param([string]$Label, [scriptblock]$Uv, [scriptblock]$Plain)
    Write-Host "==> $Label"
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        & $Uv
    } else {
        & $Plain
    }
    if ($LASTEXITCODE -ne 0) { throw "$Label failed" }
}

Run-Gate "ruff check ." `
    { uv run ruff check . } `
    { python -m ruff check . }

Run-Gate "pytest -q" `
    { uv run pytest -q } `
    { python -m pytest -q }

Write-Host "==> hermes plugins doctor . --ci"
hermes plugins doctor . --ci
if ($LASTEXITCODE -ne 0) { throw "plugin doctor failed" }

Write-Host "All local gates passed."
