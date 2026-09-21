# AutoCAB PowerShell hook. The installer loads it from the user profile.
# Get-History provides the command, timing, and status. The wrapper preserves
# existing PSReadLine key bindings.

if ($env:__WFREC_LOADED) { return }
$env:__WFREC_LOADED = 1

if (-not $env:WFREC_RUN) { $env:WFREC_RUN = '@WFREC_RUN@' }

$global:__wfrecRS = [char]0x1e
$global:__wfrecUS = [char]0x1f
$global:__wfrecLastId = -1
$global:__wfrecSeq = 0
$global:__wfrecOldPrompt = $function:prompt
$global:__wfrecHost = $env:COMPUTERNAME
if (-not $global:__wfrecHost) { $global:__wfrecHost = 'host' }

function global:prompt {
    $lastExit = $LASTEXITCODE
    $ok = $?
    try {
        $activeFile = Join-Path $env:WFREC_RUN 'active'
        if (Test-Path -LiteralPath $activeFile) {
            $line = [System.IO.File]::ReadAllText($activeFile).TrimEnd("`r", "`n")
            $parts = $line -split "`t"
            if ($parts.Count -eq 3 -and $parts[1] -like '*s*') {
                $h = Get-History -Count 1 -ErrorAction SilentlyContinue
                if ($h -and $h.Id -ne $global:__wfrecLastId) {
                    $global:__wfrecLastId = $h.Id
                    $global:__wfrecSeq++
                    # $LASTEXITCODE only reflects native .exe exits; $? covers
                    # cmdlet success. Record the more specific of the two.
                    $rc = if ($null -ne $lastExit) { $lastExit } elseif ($ok) { 0 } else { 1 }
                    $dur = [int](($h.EndExecutionTime - $h.StartExecutionTime).TotalMilliseconds)
                    $epoch = [int64](($h.EndExecutionTime.ToUniversalTime() - [datetime]'1970-01-01').TotalMilliseconds)
                    $U = $global:__wfrecUS
                    $rec = "$($global:__wfrecRS)cmd$U$epoch$U$($PWD.Path)$U$rc$U$dur$U$($h.CommandLine)$U" +
                           "powershell$U$PID$U$($global:__wfrecSeq)`n"
                    $spool = Join-Path $parts[2] "$($global:__wfrecHost)-$PID.rec"
                    # AppendAllText with explicit UTF-8-no-BOM. Never Out-File
                    # -Append: it is slow and PS 5.1 defaults to UTF-16 + BOM.
                    [System.IO.File]::AppendAllText($spool, $rec, (New-Object System.Text.UTF8Encoding $false))
                }
            }
        }
    } catch {
        # A recorder must never break the user's prompt.
    }
    $global:LASTEXITCODE = $lastExit
    & $global:__wfrecOldPrompt
}
