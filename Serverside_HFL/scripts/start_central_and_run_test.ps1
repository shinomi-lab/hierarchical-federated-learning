Set-Location -LiteralPath "$PSScriptRoot\.."

$env:MARKER_DB_PATH='C:\Users\tetsu\AppData\Local\hfl_markers\markers.db'
$env:HFL_STORAGE_DIR='C:\Users\tetsu\AppData\Local\hfl_data'

Write-Host "Env set:" $env:MARKER_DB_PATH $env:HFL_STORAGE_DIR

$p = $null
try {
    $conn = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue
    if ($conn) { $p = $conn.OwningProcess }
} catch {
    Write-Host 'Get-NetTCPConnection failed or not available'
}

if ($p) {
    Write-Host 'Killing process on port 8000:' $p
    try { Stop-Process -Id $p -Force -ErrorAction SilentlyContinue } catch {}
} else {
    Write-Host 'No process found on port 8000'
}

Write-Host 'Starting uvicorn in background...'
Start-Process -FilePath python -ArgumentList '-m','uvicorn','central_server.main:app','--host','0.0.0.0','--port','8000','--timeout-keep-alive','120' -NoNewWindow -PassThru | Out-Null
Start-Sleep -Seconds 4
Write-Host 'uvicorn started (background)'

Write-Host 'Running 50MB two-run test script (this may take a while)'
.\scripts\run_50MB_test.ps1

Write-Host 'Test finished, check logs in logs\time_records and scripts output.'
