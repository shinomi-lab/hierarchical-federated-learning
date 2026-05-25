# PowerShell example: send terminal payload to edge with simple retry
# Usage: .\scripts\terminal_client.ps1 -EdgeUrl "http://127.0.0.1:8001" -TerminalId "sim-term-01" -Round 1 -File "C:\path\to\payload.bin"

param(
    [string]$EdgeUrl = "http://127.0.0.1:8001",
    [string]$TerminalId,
    [int]$Round,
    [string]$File = $null,
    [string]$RunId = $null,
    [int]$LocalSeq = $null,
    [int]$Attempts = 5
)

if (-not $TerminalId -or -not $Round) {
    Write-Error "TerminalId and Round are required. Example: .\scripts\terminal_client.ps1 -TerminalId sim-term-01 -Round 1"
    exit 2
}

function Get-Sha256Hex([byte[]]$bytes) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $hash = $sha.ComputeHash($bytes)
    $sb = New-Object -TypeName System.Text.StringBuilder
    foreach ($b in $hash) { [void]$sb.AppendFormat("{0:x2}", $b) }
    return $sb.ToString()
}

if ($File) {
    $payload = [System.IO.File]::ReadAllBytes($File)
} else {
    $payload = [System.Text.Encoding]::UTF8.GetBytes((Get-Date).ToString())
}

$shahex = Get-Sha256Hex $payload
$contentSig = "sha256:$shahex"

$url = "$($EdgeUrl.TrimEnd('/'))/receive_terminal_weights/$TerminalId"

$form = @{}
$form['round_id'] = [string]$Round
$form['base_hash'] = $contentSig
$form['n_samples'] = '1'
$form['payload_kind'] = 'full'
$form['dtype'] = 'f32_flat'
$form['content_sha256'] = $contentSig
if ($RunId) { $form['run_id'] = $RunId }
if ($LocalSeq) { $form['local_seq'] = [string]$LocalSeq }

$attempt = 0
while ($attempt -lt $Attempts) {
    $attempt++
    Write-Host "Attempt $attempt/$Attempts -> POST $url"
    try {
        $resp = Invoke-RestMethod -Uri $url -Method Post -Form $form -Body @{ weights = [System.IO.MemoryStream]::new($payload) } -TimeoutSec 120
        # Invoke-RestMethod will parse JSON automatically
        Write-Host "Response:`n" ($resp | ConvertTo-Json -Depth 5)
        if ($resp.ack -eq $true -or $resp.status -eq 'duplicate_ignored') {
            Write-Host "Upload acknowledged."
            break
        } else {
            throw "No ack in response"
        }
    } catch {
        Write-Warning "Upload attempt failed: $_"
        if ($attempt -ge $Attempts) { throw }
        # simple backoff
        $delay = [math]::Min(300, 2 * [math]::Pow(2, $attempt - 1))
        $jitter = Get-Random -Minimum -0.3 -Maximum 0.3
        $sleep = [math]::Max(0.5, $delay + $delay * $jitter)
        Write-Host "Retrying in $([math]::Round($sleep,1))s..."
        Start-Sleep -Seconds $sleep
    }
}

Write-Host "done"
