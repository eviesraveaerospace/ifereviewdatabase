# Second OCR machine: read the videos the VM's nightly pass has not reached yet.
#
# Runs gather_short_chapters.py --reverse (lowest-priority end of each queue, so it meets the
# VM's newest-first / best-candidate-first order in the middle instead of repeating it) in
# $Shards parallel processes at below-normal priority, rotating through the passes in
# $SliceMin-minute slices. Results go to ocr_results\laptop.json (a few MB), not the 55 MB
# LFS cache; this script commits and pushes that file at most every $PushEveryMin minutes, and
# the VM's nightly job (ocr_results.py) applies it to the live cache.
#
#   .\laptop_ocr.ps1                       # 4 shards until stopped
#   .\laptop_ocr.ps1 -Shards 6 -Until 18:00
#
# Needs: the ios-screen-recording-ocr checkout (IFE_OCR_REPO or the default path in
# gather_short_chapters.py), rapidocr, yt-dlp, ffmpeg, and cookies.txt beside this script.
param(
    [int]$Shards = 4,
    [int]$SliceMin = 100,
    [int]$PushEveryMin = 120,
    [string]$Until = ""          # local time HH:mm; empty = run until stopped
)
$ErrorActionPreference = "Continue"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
$outDir = Join-Path $here "ocr_results"
New-Item -ItemType Directory -Force $outDir | Out-Null
$results = Join-Path $outDir "laptop.json"
$deadline = $null
if ($Until) { $deadline = [datetime]::Today.Add([timespan]::Parse($Until)); if ($deadline -lt (Get-Date)) { $deadline = $deadline.AddDays(1) } }
$lastPush = Get-Date

function Minutes-Left { if ($deadline) { [int](($deadline - (Get-Date)).TotalMinutes) } else { 100000 } }

function Publish-Results {
    git add -- "ocr_results/laptop.json" 2>$null
    git diff --cached --quiet -- "ocr_results/laptop.json"
    if ($LASTEXITCODE -eq 0) { return }          # nothing new
    git commit -q -m "ocr: laptop results ($(Get-Date -Format 'yyyy-MM-dd HH:mm'))" -- "ocr_results/laptop.json"
    git pull --rebase --autostash team main 2>&1 | Out-Null
    git push team main 2>&1 | Select-Object -Last 1
    $script:lastPush = Get-Date
}

function Run-Pass([string]$Name, [string[]]$PassArgs) {
    $b = [math]::Min($SliceMin, (Minutes-Left))
    if ($b -lt 5) { return }
    Write-Host "== $(Get-Date -Format 'yyyy-MM-dd HH:mm') $Name (budget $b min, $Shards shards)"
    $env:MAX_RUNTIME_MIN = "$b"
    $procs = @()
    for ($k = 0; $k -lt $Shards; $k++) {
        $a = @("-u", "gather_short_chapters.py") + $PassArgs + @("--reverse", "--results-out", $results, "--shard", "$k/$Shards")
        $p = Start-Process python -ArgumentList $a -PassThru -NoNewWindow `
             -RedirectStandardOutput (Join-Path $outDir "laptop_shard$k.log") -RedirectStandardError (Join-Path $outDir "laptop_shard$k.err.log")
        try { $p.PriorityClass = "BelowNormal" } catch {}
        $procs += $p
    }
    while (($procs | Where-Object { -not $_.HasExited }).Count -gt 0) {
        Start-Sleep -Seconds 30
        if (((Get-Date) - $lastPush).TotalMinutes -ge $PushEveryMin) { Publish-Results }
    }
    Publish-Results
}

while ((Minutes-Left) -ge 5) {
    Run-Pass "chaptered videos without tags" @("--tag-chaptered", "--interval", "4")
    Run-Pass "chapters flagged IFE"         @("--ife-chapters", "--interval", "4")
    Run-Pass "full-length without chapters" @("--long", "--interval", "4")
    Run-Pass "Shorts"                       @()
    if ((Minutes-Left) -ge 25) { Write-Host "== round done; sleeping 20 min"; Start-Sleep -Seconds 1200 }
}
Write-Host "== deadline reached"
Publish-Results
