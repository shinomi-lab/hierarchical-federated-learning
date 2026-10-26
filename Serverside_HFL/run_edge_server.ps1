# PowerShell script to run edge server without auto-reload (stable for long-lived device connections)
# Usage: .\run_edge_server.ps1

$ErrorActionPreference = "Stop"

# Ensure stdout is unbuffered for logs
$env:PYTHONUNBUFFERED = "1"

Write-Host "Starting edge server (uvicorn) on 0.0.0.0:8001 ..."
python -m uvicorn edge_server.main:app --host 0.0.0.0 --port 8001
