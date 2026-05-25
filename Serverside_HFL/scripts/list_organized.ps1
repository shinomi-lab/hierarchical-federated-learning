# List organized logs in newest-first order
param(
    [int]$Groups = 10,
    [int]$Files = 20
)
 $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
 $repoRoot = (Resolve-Path (Join-Path $scriptDir '..')).Path
 $py = Join-Path $repoRoot '.venv\Scripts\python.exe'
 $pyScript = Join-Path $repoRoot 'scripts\list_organized.py'
if ((Test-Path $py -PathType Leaf) -and (Test-Path $pyScript -PathType Leaf)) {
    & $py $pyScript --groups $Groups --files $Files
} elseif (Test-Path $pyScript -PathType Leaf) {
    # fallback to system python
    & python $pyScript --groups $Groups --files $Files
} else {
    Write-Error "list_organized.py not found"
}
