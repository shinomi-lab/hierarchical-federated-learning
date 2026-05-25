Param(
    [string]$TargetDir = "$env:USERPROFILE\AppData\Local\hfl_markers",
    [string]$TargetDb = "markers.db"
)

Write-Host "Preparing marker DB target: $TargetDir\$TargetDb"
New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
$full = Join-Path $TargetDir $TargetDb

Write-Host "Invoking migration helper via python..."
python -c "import sys; sys.path.insert(0, r'C:\\Users\\tetsu\\Serverside'); from pathlib import Path; from central_server.utils.marker_store import migrate_marker_db; print('migrate result:', migrate_marker_db(Path(r'$full')))"

Write-Host "If migration succeeded, set these env vars before starting central_server:"
Write-Host "  MARKER_DB_PATH=$full"
Write-Host "  HFL_STORAGE_DIR=C:\Users\$env:USERNAME\AppData\Local\hfl_data"
Write-Host "You can set them for the current PowerShell session like this:"
Write-Host "  $env:MARKER_DB_PATH = '$full'"
Write-Host "  $env:HFL_STORAGE_DIR = 'C:\Users\$env:USERNAME\AppData\Local\hfl_data'"
Write-Host "Then restart central_server process (stop and start uvicorn/process)."

Write-Host "Also consider running: .\scripts\check_marker_smoke.py to verify backend operations."
