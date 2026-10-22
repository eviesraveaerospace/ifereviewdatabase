# IFE-chapter OCR from the laptop: the same pass the VM runs nightly (60 min, newest
# first, two shards) but oldest-first and with more shards, so the two machines work
# from opposite ends of the backlog. Then commit and push like nightly_transcripts.cmd.
#
#   .\ife_ocr_laptop.ps1                         # 6 shards, 120 minutes, pushes to team
#   .\ife_ocr_laptop.ps1 -Shards 8 -Minutes 240
#   .\ife_ocr_laptop.ps1 -Minutes 3 -NoPush      # smoke test: leaves the cache change uncommitted
#
# Needs the ios-screen-recording-ocr checkout (IFE_OCR_REPO, or C:\workspace\ios-screen-recording-ocr),
# git + git-lfs on PATH, and the YouTube cookies the VM uses (cookies.txt here).
param(
    [int]$Shards = 6,
    [int]$Minutes = 120,
    [double]$Interval = 6,
    [switch]$NoPush
)
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$py = if ($env:IFE_PYTHON) { $env:IFE_PYTHON } else { "python" }
if (-not $env:IFE_OCR_REPO) { $env:IFE_OCR_REPO = "C:\workspace\ios-screen-recording-ocr" }
$log = "ife_ocr_laptop_log.txt"
function Log($m) { "$((Get-Date).ToString('s')) $m" | Add-Content $log }

Log "==== laptop IFE OCR started ($Shards shards, $Minutes min) ===="
if (-not $NoPush) {
    git pull --rebase --autostash team main *>> $log
    if ($LASTEXITCODE -ne 0) { Log "pull failed; continuing on the local copy"; git rebase --abort *>> $log }
}

$env:MAX_RUNTIME_MIN = "$Minutes"
$procs = @()
for ($i = 0; $i -lt $Shards; $i++) {
    $procs += Start-Process -FilePath $py -NoNewWindow -PassThru `
        -ArgumentList @("-u", "gather_short_chapters.py", "--ife-chapters", "--oldest-first",
                        "--interval", "$Interval", "--shard", "$i/$Shards") `
        -RedirectStandardOutput "ife_ocr_shard$i.out.txt" -RedirectStandardError "ife_ocr_shard$i.err.txt"
}
$procs | Wait-Process
for ($i = 0; $i -lt $Shards; $i++) {
    Log ("shard ${i}: " + ((Select-String -Path "ife_ocr_shard$i.out.txt" -Pattern "^Done:" | Select-Object -Last 1).Line))
}
if ($NoPush) { Log "NoPush: cache change left uncommitted"; exit 0 }

git add ife_cache.json *>> $log
git diff --staged --quiet
if ($LASTEXITCODE -eq 0) { Log "no changes to commit"; exit 0 }
git commit -m "data: IFE-chapter OCR screen tags, oldest first (laptop)" *>> $log
git push team main *>> $log
if ($LASTEXITCODE -ne 0) {
    # CI or the VM committed meanwhile: replay our commit on top, merging the cache by URL.
    git pull --rebase --autostash team main *>> $log
    if ($LASTEXITCODE -ne 0) {
        & $py resolve_cache_conflict.py *>> $log
        $env:GIT_EDITOR = "true"
        git rebase --continue *>> $log
        if ($LASTEXITCODE -ne 0) { Log "rebase conflict; commit left local, resolve by hand"; git rebase --abort *>> $log; exit 1 }
    }
    git push team main *>> $log
}
Log "==== laptop IFE OCR finished ===="
