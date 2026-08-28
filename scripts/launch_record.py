"""Launch the RLM demo; stay windowless unless recording is explicit."""
import argparse
import os
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--visible-console", action="store_true")
args = parser.parse_args()

repo = Path(__file__).resolve().parent.parent
fixture = repo / "examples" / "payments-api"
venv_python = (
    Path(os.environ["LOCALAPPDATA"]) / "hermes" / "hermes-agent" / "venv" / "Scripts" / "python.exe"
)
kernel_python = Path.home() / ".prime" / "agent" / "kernel-venv" / "Scripts" / "python.exe"
linger = (
    "Write-Host 'Window closes in 120 seconds.' -ForegroundColor DarkGray\n"
    "Start-Sleep -Seconds 120"
    if args.visible_console
    else ""
)

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
Push-Location -LiteralPath '{fixture}'
& '{kernel_python}' run_tests.py 2>&1 | ForEach-Object {{ Write-Host "    $_" -ForegroundColor Red }}
Pop-Location
Write-Host '    ^ FAILS.' -ForegroundColor DarkGray
Write-Host ''
Write-Host '[3] prime_agent(action=run) -> Prime Agent v0.8.1 in a' -ForegroundColor Yellow
Write-Host '    detached worktree. Streaming, not reading. Please wait...' -ForegroundColor Gray
Write-Host ''
$env:PRIME_AGENT_KERNEL_PYTHON = '{kernel_python}'
& '{venv_python}' .\\scripts\\demo_rlm.py
Write-Host ''
Write-Host '[4] Host-observed re-run inside the candidate:' -ForegroundColor Yellow
$result = Get-Content '{repo}\\rlm-demo-last-result.json' -Raw | ConvertFrom-Json
$cand = $result.candidate_path
Push-Location $cand
& '{kernel_python}' run_tests.py 2>&1 | ForEach-Object {{ Write-Host "    $_" -ForegroundColor Green }}
Pop-Location
Write-Host ''
Write-Host '[5] Active checkout:' -ForegroundColor Yellow
$st = git -C '{fixture}' status --porcelain
if ($st) {{ Write-Host "    MODIFIED: $st" -ForegroundColor Red }} else {{ Write-Host '    clean — nothing changed outside the candidate' -ForegroundColor Green }}
Write-Host ''
Write-Host '================================================================' -ForegroundColor Green
Write-Host '  VERIFIED = the recorded host check exited zero.' -ForegroundColor Green
Write-Host '  Nothing applied, committed, or pushed.' -ForegroundColor Green
Write-Host '================================================================' -ForegroundColor Green
Write-Host ''
{linger}
"""
launcher = Path(os.environ["TEMP"]) / "prime_rlm_rlm_record.ps1"
launcher.write_text(ps, encoding="utf-8")

mode_flag = "-NoExit" if args.visible_console else "-NonInteractive"
command = [
    "powershell.exe",
    "-NoProfile",
    "-ExecutionPolicy",
    "Bypass",
    mode_flag,
    "-File",
    str(launcher),
]
creationflags = (
    subprocess.CREATE_NEW_CONSOLE if args.visible_console else subprocess.CREATE_NO_WINDOW
)
if args.visible_console:
    subprocess.Popen(command, creationflags=creationflags, cwd=str(repo))
    print("launched visible recording console")
else:
    log_path = Path(os.environ["TEMP"]) / "prime_rlm_rlm_record.log"
    with open(log_path, "ab") as log:
        subprocess.Popen(
            command,
            creationflags=creationflags,
            cwd=str(repo),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    print(f"launched silently; log: {log_path}")
