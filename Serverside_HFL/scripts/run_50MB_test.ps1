<#
Run a controlled 50MB two-run test against a locally running central server.

Usage (PowerShell):
  # Start server in a separate window (manual) or background job
  python -m uvicorn central_server.main:app --host 0.0.0.0 --port 8000 --reload --timeout-keep-alive 120

  # Then run this script from the repo root
  .\scripts\run_50MB_test.ps1

What it does:
  - Runs the terminal client send twice (first cold, second warm) and prints Measure-Command timings.
  - Tails the latest central_server time_records log lines containing 'upload_streamed' and 'request_e2e'.

Notes:
  - Ensure `weights_50MB.bin` exists in the repo root (or edit the $file variable below).
#>

$serverUrl = 'http://localhost:8000/edge_update'
$file = Join-Path $PSScriptRoot '..\weights_50MB.bin'
$edge = 'edge-1'
$round = 100

Write-Host "Running 50MB two-run test against $serverUrl using file $file"

Write-Host "First (cold) run:" -ForegroundColor Cyan
$first = Measure-Command {
    python .\scripts\terminal_client.py send --url $serverUrl --file $file --edge $edge --round $round --num-clients 1 --sum-n-samples 1
}
Write-Host "Elapsed (first):" $first.TotalSeconds

Start-Sleep -Seconds 2

Write-Host "Second (warm) run:" -ForegroundColor Cyan
$second = Measure-Command {
    python .\scripts\terminal_client.py send --url $serverUrl --file $file --edge $edge --round $round --num-clients 1 --sum-n-samples 1
}
Write-Host "Elapsed (second):" $second.TotalSeconds

Write-Host "Tailing recent central server timing lines (upload_streamed, request_e2e):" -ForegroundColor Green
$logs = Get-ChildItem -Path .\logs\time_records\central_server_*.log | Sort-Object LastWriteTime -Descending
if ($logs -and $logs.Length -gt 0) {
    $latest = $logs[0].FullName
    Write-Host "Latest log: $latest"
    Select-String -Path $latest -Pattern 'upload_streamed|request_e2e' -SimpleMatch | Select-Object -Last 40
} else {
    Write-Host "No central_server logs found in logs/time_records" -ForegroundColor Yellow
}

Write-Host "Done"
