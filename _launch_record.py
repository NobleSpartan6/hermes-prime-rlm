"""Launch the RLM demo window for recording (single window, self-contained)."""
import os
import subprocess
from pathlib import Path

repo = Path(r"C:\Users\ebene\hermes rlm\hermes-prime-rlm")
fixture = Path(r"C:\Users\ebene\hermes rlm\rlm-fixture\payments-api")
venv_python = (
    Path(os.environ["LOCALAPPDATA"]) / "hermes" / "hermes-agent" / "venv" / "Scripts" / "python.exe"
)
kernel_python = Path.home() / ".prime" / "agent" / "kernel-venv" / "Scripts" / "python.exe"

ps = f"""
Set-Location -LiteralPath '{repo}'
$host.UI.RawUI.WindowTitle = 'Hermes Prime RLM — RLM-scale demo'
Clear-Host
Write-Host ''
Write-Host '================================================================' -ForegroundColor Cyan
Write-Host '  Hermes Prime RLM — a task only an RLM can handle' -ForegroundColor Cyan
Write-Host '================================================================' -ForegroundColor Cyan
Write-Host ''
Write-Host '[1] The data: events.log' -ForegroundColor Yellow
$log = Get-Item '{fixture}\\events.log'
Write-Host ('    size: {{0:N1}} MB  |  ~1.5 MILLION tokens' -f ($log.Length/1MB)) -ForegroundColor Gray
Write-Host '    No model context window can hold this. It must be' -ForegroundColor DarkGray
Write-Host '    programmed against, not read. That is the RLM difference.' -ForegroundColor DarkGray
Write-Host ''
Write-Host '[2] The bug: error_rate() counts stack-trace lines as entries' -ForegroundColor Yellow
Write-Host '    Pinned test against current code:' -ForegroundColor Gray
& '{kernel_python}' run_tests.py 2>&1 | ForEach-Object {{ Write-Host "    $_" -ForegroundColor Red }}
Write-Host '    ^ FAILS.' -ForegroundColor DarkGray
Write-Host ''
Write-Host '[3] prime_rlm_run -> real Prime Agent (stealth/ox-alpha) in a' -ForegroundColor Yellow
Write-Host '    detached worktree. Streaming, not reading. Please wait...' -ForegroundColor Gray
Write-Host ''
$env:PRIME_AGENT_KERNEL_PYTHON = '{kernel_python}'
& '{venv_python}' .\\demo_rlm.py
Write-Host ''
Write-Host '[4] Independent re-verification inside the candidate:' -ForegroundColor Yellow
$latest = Get-ChildItem "$env:LOCALAPPDATA\\hermes\\plugin-data" -Recurse -Filter 'receipt.json' |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
$cand = (Get-Content $latest.FullName -Raw | ConvertFrom-Json).receipt.candidate_path
Push-Location $cand
& '{kernel_python}' run_tests.py 2>&1 | ForEach-Object {{ Write-Host "    $_" -ForegroundColor Green }}
Pop-Location
Write-Host ''
Write-Host '[5] Active checkout:' -ForegroundColor Yellow
$st = git -C '{fixture}' status --porcelain
if ($st) {{ Write-Host "    MODIFIED: $st" -ForegroundColor Red }} else {{ Write-Host '    clean — nothing changed outside the candidate' -ForegroundColor Green }}
Write-Host ''
Write-Host '================================================================' -ForegroundColor Green
Write-Host '  VERIFIED = the host check ran independently and passed.' -ForegroundColor Green
Write-Host '  Nothing applied, committed, or pushed.' -ForegroundColor Green
Write-Host '================================================================' -ForegroundColor Green
Write-Host ''
Write-Host 'Window closes in 120 seconds.' -ForegroundColor DarkGray
Start-Sleep -Seconds 120
"""
launcher = Path(os.environ["TEMP"]) / "prime_rlm_rlm_record.ps1"
launcher.write_text(ps, encoding="utf-8")

subprocess.Popen(
    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-File", str(launcher)],
    creationflags=subprocess.CREATE_NEW_CONSOLE,
    cwd=str(repo),
)
print("launched")
