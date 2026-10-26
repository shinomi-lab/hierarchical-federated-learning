param(
    [int]$IntervalMinutes = 10,
    [string]$Day = "",
    [switch]$Latest
)

# Use current PowerShell session's Python
$root = Split-Path $PSScriptRoot -Parent
$outdir = Join-Path $root "logs/analysis"
$rollup = Join-Path $root "tools/shared_storage_rollup.py"

Write-Host "[auto_rollup] Root: $root"
Write-Host "[auto_rollup] OutDir: $outdir"

# Build argument list
$commonArgs = @("--outdir", $outdir, "--format", "both")
if ($Latest) { $commonArgs += "--latest" }
if ($Day -ne "") { $commonArgs += @("--day", $Day) }

try {
    Write-Host "[auto_rollup] Starting periodic rollup (every $IntervalMinutes min). Press Ctrl+C to stop."
    while ($true) {
        $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Write-Host "[auto_rollup] [$ts] Running rollup..."
        python $rollup @commonArgs | Out-String | Write-Host
        Write-Host "[auto_rollup] [$ts] Done. Sleeping..."
        Start-Sleep -Seconds ($IntervalMinutes * 60)
    }
}
finally {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[auto_rollup] [$ts] Final rollup before exit..."
    python $rollup @commonArgs | Out-String | Write-Host
    Write-Host "[auto_rollup] [$ts] Exit complete."
}
