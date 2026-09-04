# Dot-source this from your PowerShell $PROFILE so `sentinel` works from
# any directory, in any new PowerShell window:
#
#   . "E:\herdr-task-bridge\sentinel.profile.ps1"
#
# (substitute your own clone path). To find/edit your profile:
#   notepad $PROFILE

$Script:SentinelScript = Join-Path $PSScriptRoot "sentinel.ps1"

function sentinel {
    & $Script:SentinelScript @args
}
