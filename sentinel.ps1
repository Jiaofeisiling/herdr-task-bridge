param(
    [Parameter(Mandatory=$true, Position=0)]
    [ValidateSet(
        "ask",
        "prompt",
        "delegate",
        "task",
        "tasks",
        "wait",
        "status",
        "ready",
        "read",
        "health",
        "agents",
        "quota",
        "quota-reset"
    )]
    [string]$Command,

    [Parameter(Position=1, ValueFromRemainingArguments=$true)]
    [string[]]$Text,

    [ValidateRange(1, 5000)]
    [int]$Lines = 80,

    # Which part of the terminal `read` returns. Omit it and a read of a
    # working agent falls back to the visible screen on its own, saying so;
    # name it to get exactly that. PowerShell reads --Source as -Source, so
    # herdr's own spelling, which its error message recommends, works too.
    [ValidateSet("recent-unwrapped", "visible")]
    [string]$Source,

    # For `tasks`: list only tasks in this state, and how many. Without them
    # it is the newest twenty of everything, which is how two tasks stuck in
    # the queue for three weeks were never seen.
    [ValidateSet("queued", "running", "done", "error", "orphaned", "quota_exhausted")]
    [string]$Status,

    [ValidateRange(1, 200)]
    [int]$Limit,

    [int]$TimeoutMs = 120000,

    # Which herdr agent to target. Omit to use the bridge's own
    # SENTINEL_AGENT default -- see `agents` for the full list of what's
    # actually available on the remote host right now.
    [string]$Agent,

    # What the task is permitted to do with Slurm. Omit to use the
    # deployment's default, which does not restrict submission -- an
    # sbatch is reversible and holding one back costs more than it saves.
    # Pass a narrower value when a task genuinely should not submit.
    [ValidateSet("dry_run_only", "test_only", "submit")]
    [string]$SlurmPolicy,

    # Names one logical delegate across its retries. Omit and one is
    # generated; on a failure the client prints it, so a retry can pass it
    # back and the bridge collapses the repeat into the original task
    # instead of queueing a second copy.
    [string]$IdempotencyKey
)

$Utf8 = New-Object System.Text.UTF8Encoding($false)

[Console]::InputEncoding  = $Utf8
[Console]::OutputEncoding = $Utf8
$OutputEncoding           = $Utf8

$BaseUrl = if ($env:SENTINEL_BRIDGE_URL) { $env:SENTINEL_BRIDGE_URL } else { "http://127.0.0.1:8765" }

# Upper bound, in seconds, on any call that is meant to be quick. Without it
# there was none at all: PowerShell 7's Invoke-RestMethod default is
# infinite, so a forward that still accepted connections but no longer
# reached the host hung the client until something killed it. A real caller
# gives a command ten or thirty seconds before handing back an empty result
# and reporting it blocked -- measured over 3,400 recorded calls, health took
# 1.9 s at the median and every empty result took exactly the caller's yield
# time. The bound has to be shorter than that, or the explanation printed on
# failure arrives after the reader has gone.
$QuickTimeoutSec = if ($env:SENTINEL_CLIENT_TIMEOUT_SEC) { [int]$env:SENTINEL_CLIENT_TIMEOUT_SEC } else { 5 }

# Slack added on top of an ask/prompt's own -TimeoutMs before the client gives
# up on it, so it outlives the bridge's timeout rather than abandoning work
# that is proceeding normally.
$AskGraceSec = if ($env:SENTINEL_ASK_GRACE_SEC) { [int]$env:SENTINEL_ASK_GRACE_SEC } else { 30 }

# How long `wait` keeps polling through a continuous channel outage before it
# gives up. wait is meant to outlast a task that may run for hours, so a
# brief tunnel blip must not end it -- but a channel that stays dead should.
$WaitToleranceSec = if ($env:SENTINEL_WAIT_TOLERANCE_SEC) { [int]$env:SENTINEL_WAIT_TOLERANCE_SEC } else { 60 }

# What the current command is, for the one thing that depends on it: what to
# say about retrying when a request gets no reply. Reads are safe to repeat;
# a delegate or an ask may already have taken effect.
$Script:ActiveRequest = $null
$Script:ChannelFailed = $false
$Script:LastChannelError = $null

# How long the follow-up liveness probe may take. Worst case for a stalled
# call is therefore the bound plus this plus about a second of PowerShell
# startup -- about 7 s against the 10 s a caller gives a quick command. An
# earlier 6 s bound with a 2 s probe came to 9.3 s, which only moved the
# point at which the same failure appears by 0.7 s.
$ProbeTimeoutSec = 1

# Query-string suffix for the GET endpoints that accept ?agent=... . Empty
# when -Agent wasn't passed, so the bridge falls back to its own default.
$AgentQuery = if ($PSBoundParameters.ContainsKey("Agent")) {
    "?agent=$([uri]::EscapeDataString($Agent))"
} else {
    ""
}

$ReadQueryParts = @("lines=$Lines")
if ($PSBoundParameters.ContainsKey("Agent")) {
    $ReadQueryParts += "agent=$([uri]::EscapeDataString($Agent))"
}
if ($PSBoundParameters.ContainsKey("Source")) {
    $ReadQueryParts += "source=$([uri]::EscapeDataString($Source))"
}
$ReadQuery = "?" + ($ReadQueryParts -join "&")

# `$Text` collects every argument that is not a declared parameter, which is
# what lets a task be typed without quotes -- and also swallows anything
# mistyped or unsupported. For a command with no use for free text that meant
# an option such as `--source visible` was silently dropped while the caller
# believed it had been sent: 33 failed reads in a night, each followed by a
# retry with the same dropped option. Refuse instead of pretending.
$TakesNoText = @("read", "ready", "status", "health", "agents", "quota", "quota-reset", "tasks")
if (($TakesNoText -contains $Command) -and $Text -and ($Text.Count -gt 0)) {
    [Console]::Error.WriteLine("sentinel.ps1 ${Command}: unrecognised argument(s): " + ($Text -join " "))
    exit 1
}


function Join-TaskText {
    return ($Text -join " ").Trim()
}


# Classify a connection-layer failure without reading the exception text.
# That text is localised -- on a Chinese Windows a refused connection is
# reported in Chinese, not English -- so matching on it would work on one
# machine and quietly fail on the next. SocketError and WebExceptionStatus are stable and language
# independent, and PowerShell 5.1 and 7 wrap them differently, so both
# shapes are unwrapped here.
function Get-ChannelFailureKind {
    param($ErrorRecord)

    $ex = $ErrorRecord.Exception

    while ($ex) {
        if ($ex -is [System.Net.Sockets.SocketException]) {
            if ($ex.SocketErrorCode -eq [System.Net.Sockets.SocketError]::ConnectionRefused) {
                return "refused"
            }
            if ($ex.SocketErrorCode -eq [System.Net.Sockets.SocketError]::TimedOut) {
                return "timeout"
            }
        }

        if ($ex -is [System.Net.WebException]) {
            if ($ex.Status -eq [System.Net.WebExceptionStatus]::ConnectFailure) { return "refused" }
            if ($ex.Status -eq [System.Net.WebExceptionStatus]::Timeout) { return "timeout" }
        }

        if ($ex -is [System.TimeoutException]) { return "timeout" }
        if ($ex -is [System.Threading.Tasks.TaskCanceledException]) { return "timeout" }

        $ex = $ex.InnerException
    }

    return "unknown"
}

# Exit 4 -- distinct from 1 (a request failed), 2 (orphaned) and 3 (quota),
# because none of those happened: the bridge was never reached, so nothing
# is known about it either way. A caller once reported a remote outage on
# the strength of a failure that was entirely on this side of the tunnel.
function Test-BridgeAnswers {
    $Script:ProbeNote = $null

    try {
        $null = Invoke-RestMethod -Uri "$BaseUrl/health" -Method Get -TimeoutSec $ProbeTimeoutSec
        return $true
    }
    catch {
        # Any HTTP response at all -- even a 503 -- means the bridge is there
        # and answering. Treating every exception as "no answer" would report
        # a bridge that is up but unhealthy (its worker thread dead, say) as a
        # dead channel, and send the reader to reconnect a tunnel that works.
        if ($null -ne $_.Exception.Response) {
            $reason = $null
            if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
                try { $reason = ($_.ErrorDetails.Message | ConvertFrom-Json).reason } catch { }
            }
            $Script:ProbeNote = if ($reason) { $reason } else { "an HTTP error" }
            return $true
        }

        return $false
    }
}

function Get-RetryAdvice {
    $req = $Script:ActiveRequest
    if ($null -eq $req) { return @() }

    switch ($req.Kind) {
        "delegate" {
            return @(
                "  This delegate MAY HAVE BEEN QUEUED: the reply was lost, not necessarily the request.",
                "  Do not simply run it again -- that can queue it twice. Retry with the same key,",
                "  which is safe, and the bridge will return the original task if it exists:",
                "    sentinel.ps1 delegate -IdempotencyKey $($req.Key) <the same task>",
                "  Or look for it first with: sentinel.ps1 tasks"
            )
        }
        "execute" {
            return @(
                "  This request MAY HAVE ALREADY RUN on the host. Do not retry blindly --",
                "  check the agent with ready and read first."
            )
        }
        "wait" {
            return @(
                "  The task itself is unaffected and is still on the host.",
                "  Run again when the channel is back: sentinel.ps1 wait $($req.TaskId)"
            )
        }
    }

    return @()
}

# Exit 4 -- the bridge was never reached, so nothing is known about it.
# Exit 5 -- the bridge answers /health, but this one request got no reply.
#
# Told apart by asking, not by guessing. A request that times out looks the
# same whether the forward is dead or the bridge is up and one call behind it
# (a herdr call hung on the host) is stuck, and the two want opposite
# reactions: reconnect VS Code, or do not touch the tunnel at all. /health
# is the right probe because it deliberately depends on nothing else.
function Exit-ChannelDown {
    param($ErrorRecord, [string]$Uri, [int]$WaitedSec = 0)

    $kind = Get-ChannelFailureKind -ErrorRecord $ErrorRecord
    $isHealthCall = $Uri -like "*/health"

    if ($kind -eq "timeout" -and -not $isHealthCall -and (Test-BridgeAnswers)) {
        $lines = @(
            "NO REPLY: $Uri did not answer within ${WaitedSec}s, but the bridge's /health does.",
            "  The channel is fine and the bridge is up; this one request is stuck behind it,",
            "  most often a herdr call on the host that is slow or hung. Do NOT reconnect",
            "  VS Code."
        )
        if ($Script:ProbeNote) {
            $lines += "  Note: /health answered, but reports the bridge itself unhealthy ($Script:ProbeNote)."
            $lines += "  The channel is fine; the bridge needs attention on the host (restart it)."
        }

        $advice = Get-RetryAdvice
        if ($advice.Count -eq 0) {
            # Only for a read, where repeating it cannot do any harm.
            $lines += "  Retry once; if it repeats, look at what the host's herdr is doing."
        }
        else {
            $lines += $advice
        }
        foreach ($line in $lines) { [Console]::Error.WriteLine($line) }
        exit 5
    }

    $lines = switch ($kind) {
        "refused" {
            @(
                "CHANNEL DOWN: nothing is listening on $Uri.",
                "  The SSH port forward is not in place. Reconnect VS Code to the host,",
                "  then try again. The bridge was not reached, so its state is unknown --",
                "  it is most likely still running fine on the remote side."
            )
        }
        "timeout" {
            @(
                "CHANNEL DOWN: connected to $Uri but nothing came back within ${WaitedSec}s$(if (-not $isHealthCall) { ', and /health did not answer either' }).",
                "  The local port is still forwarded, but the forward's path to the host",
                "  is dead. Disconnect and reconnect VS Code -- reloading the window",
                "  usually does not rebuild the forward. The bridge was not reached, so",
                "  its state is unknown; it is probably fine."
            )
        }
        default {
            @(
                "CHANNEL DOWN: could not reach $Uri.",
                "  $($ErrorRecord.Exception.Message)",
                "  The bridge was not reached, so its state is unknown. Check that VS Code",
                "  is connected to the host and the port forward is in place."
            )
        }
    }

    foreach ($line in ($lines + (Get-RetryAdvice))) {
        [Console]::Error.WriteLine($line)
    }

    exit 4
}

function Invoke-SentinelApi {
    param(
        [Parameter(Mandatory=$true)][string]$Uri,
        [string]$Method = "Get",
        [string]$Body = $null,
        [int]$TimeoutSec = $QuickTimeoutSec,
        [switch]$SoftFail
    )

    $Script:ChannelFailed = $false

    $headers = @{}

    if ($env:SENTINEL_BRIDGE_TOKEN) {
        $headers["X-Sentinel-Token"] = $env:SENTINEL_BRIDGE_TOKEN
    }

    try {
        if ($Body) {
            return Invoke-RestMethod -Uri $Uri -Method $Method -TimeoutSec $TimeoutSec `
                -ContentType "application/json; charset=utf-8" -Body $Body -Headers $headers
        }

        return Invoke-RestMethod -Uri $Uri -Method $Method -TimeoutSec $TimeoutSec -Headers $headers
    }
    catch {
        # Windows PowerShell 5.1 throws WebException for non-2xx responses;
        # PowerShell 7 throws HttpResponseException instead. Preserve one
        # code path for both editions and prefer ErrorDetails, where
        # Invoke-RestMethod normally stores the already-read response body.
        $caughtError = $_
        $errResponse = $caughtError.Exception.Response

        if ($caughtError.ErrorDetails -and $caughtError.ErrorDetails.Message) {
            try {
                return $caughtError.ErrorDetails.Message | ConvertFrom-Json
            }
            catch {
                # Fall through and try the response object. If that also
                # fails, rethrow the original HTTP error below.
            }
        }

        if ($null -eq $errResponse) {
            # No response object at all means the request never completed:
            # a connection-layer failure, not an HTTP error. Rethrowing
            # here printed a raw Invoke-RestMethod stack trace *and* left
            # the exit code at 0, so callers saw success on a dead channel.
            if ($SoftFail) {
                # The caller (wait) decides how long to keep trying, so it is
                # told rather than having the process ended under it.
                $Script:ChannelFailed = $true
                $Script:LastChannelError = $caughtError
                return $null
            }

            Exit-ChannelDown -ErrorRecord $caughtError -Uri $Uri -WaitedSec $TimeoutSec
        }

        $rawBody = $null

        if ($errResponse.PSObject.Methods.Name -contains "GetResponseStream") {
            $stream = $errResponse.GetResponseStream()
            if ($stream) {
                $reader = New-Object System.IO.StreamReader($stream)
                $rawBody = $reader.ReadToEnd()
                $reader.Close()
            }
        }
        elseif ($errResponse.Content) {
            $rawBody = $errResponse.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        }

        if ($rawBody) {
            try {
                return $rawBody | ConvertFrom-Json
            }
            catch {
                # The server did not return JSON; preserve the original
                # exception and its HTTP status for the caller.
            }
        }

        throw $caughtError
    }
}


switch ($Command) {

    "health" {
        $result = Invoke-SentinelApi -Uri "$BaseUrl/health"
        $result | ConvertTo-Json -Depth 10

        if (-not $result -or -not $result.ok) {
            exit 1
        }
    }

    "status" {
        $result = Invoke-SentinelApi -Uri "$BaseUrl/status$AgentQuery"

        if (-not $result.ok) {
            Write-Error $result.stderr
            exit 1
        }

        $result.stdout
    }

    "read" {
        $result = Invoke-SentinelApi -Uri "$BaseUrl/read$ReadQuery"

        # Said on stderr so the terminal text on stdout stays exactly the
        # terminal text. Without it a caller cannot tell it was handed the
        # visible screen in place of the history it asked for.
        if ($result.note) { [Console]::Error.WriteLine("[note] " + $result.note) }
        if ($result.hint) { [Console]::Error.WriteLine("[hint] " + $result.hint) }

        if (-not $result.ok) {
            Write-Error $result.stderr
            exit 1
        }

        $result.stdout
    }

    "agents" {
        $result = Invoke-SentinelApi -Uri "$BaseUrl/agents"
        $result | ConvertTo-Json -Depth 20

        if (-not $result.ok) {
            exit 1
        }
    }

    "quota" {
        $result = Invoke-SentinelApi -Uri "$BaseUrl/quota"
        $result | ConvertTo-Json -Depth 20

        if (-not $result.ok) {
            exit 1
        }
    }

    "quota-reset" {
        $payload = @{}
        if ($PSBoundParameters.ContainsKey("Agent")) {
            $payload["agent"] = $Agent
        }

        $body = $payload | ConvertTo-Json -Compress
        $result = Invoke-SentinelApi -Uri "$BaseUrl/quota/reset" -Method Post -Body $body
        $result | ConvertTo-Json -Depth 20

        if (-not $result.ok) {
            exit 1
        }
    }

    "prompt" {
        $task = Join-TaskText

        if (-not $task) {
            Write-Error "Prompt cannot be empty."
            exit 1
        }

        $payload = @{
            task       = $task
            timeout_ms = $TimeoutMs
        }

        if ($PSBoundParameters.ContainsKey("Agent")) {
            $payload["agent"] = $Agent
        }

        if ($PSBoundParameters.ContainsKey("SlurmPolicy")) {
            $payload["slurm_policy"] = $SlurmPolicy
        }

        $body = $payload | ConvertTo-Json -Compress

        # These legitimately run as long as the caller allowed, so the client has to
        # outlive the bridge's own timeout (plus slack) or it abandons work that
        # is proceeding normally and then misreports it as a dead channel.
        $Script:ActiveRequest = @{ Kind = "execute" }
        $result = Invoke-SentinelApi -Uri "$BaseUrl/prompt" -Method Post -Body $body `
            -TimeoutSec ([int][math]::Ceiling($TimeoutMs / 1000) + $AskGraceSec)
        $result | ConvertTo-Json -Depth 20

        if (-not $result.ok) {
            exit 1
        }
    }

    "ask" {
        $task = Join-TaskText

        if (-not $task) {
            Write-Error "Task cannot be empty."
            exit 1
        }

        $payload = @{
            task       = $task
            timeout_ms = $TimeoutMs
            lines      = 500
        }

        if ($PSBoundParameters.ContainsKey("Agent")) {
            $payload["agent"] = $Agent
        }

        if ($PSBoundParameters.ContainsKey("SlurmPolicy")) {
            $payload["slurm_policy"] = $SlurmPolicy
        }

        $body = $payload | ConvertTo-Json -Compress

        # These legitimately run as long as the caller allowed, so the client has to
        # outlive the bridge's own timeout (plus slack) or it abandons work that
        # is proceeding normally and then misreports it as a dead channel.
        $Script:ActiveRequest = @{ Kind = "execute" }
        $result = Invoke-SentinelApi -Uri "$BaseUrl/ask" -Method Post -Body $body `
            -TimeoutSec ([int][math]::Ceiling($TimeoutMs / 1000) + $AskGraceSec)
        $result | ConvertTo-Json -Depth 20

        if (-not $result.ok) {
            exit 1
        }
    }

    "delegate" {
        $task = Join-TaskText

        if (-not $task) {
            Write-Error "Task cannot be empty."
            exit 1
        }

        $payload = @{ task = $task }

        if ($PSBoundParameters.ContainsKey("TimeoutMs")) {
            $payload["timeout_ms"] = $TimeoutMs
        }

        if ($PSBoundParameters.ContainsKey("Agent")) {
            $payload["agent"] = $Agent
        }

        if ($PSBoundParameters.ContainsKey("SlurmPolicy")) {
            $payload["slurm_policy"] = $SlurmPolicy
        }

        # One key per logical delegate, reused by any retry. A caller who
        # supplies none still gets one, because the point is to be able to
        # print it when a reply is lost.
        $key = if ($PSBoundParameters.ContainsKey("IdempotencyKey")) { $IdempotencyKey } else { [guid]::NewGuid().ToString() }
        $payload["idempotency_key"] = $key
        $Script:ActiveRequest = @{ Kind = "delegate"; Key = $key }

        $body = $payload | ConvertTo-Json -Compress

        $result = Invoke-SentinelApi -Uri "$BaseUrl/delegate" -Method Post -Body $body
        $result | ConvertTo-Json -Depth 20

        if (-not $result.ok) {
            exit 1
        }
    }

    "ready" {
        $result = Invoke-SentinelApi -Uri "$BaseUrl/ready$AgentQuery"
        $result | ConvertTo-Json -Depth 10

        if (-not $result.ok) {
            exit 1
        }
    }

    "tasks" {
        $tasksQuery = @()
        if ($PSBoundParameters.ContainsKey("Status")) { $tasksQuery += "status=$([uri]::EscapeDataString($Status))" }
        if ($PSBoundParameters.ContainsKey("Limit")) { $tasksQuery += "limit=$Limit" }
        $tasksSuffix = if ($tasksQuery.Count -gt 0) { "?" + ($tasksQuery -join "&") } else { "" }

        $result = Invoke-SentinelApi -Uri "$BaseUrl/tasks$tasksSuffix"
        $result | ConvertTo-Json -Depth 20

        if (-not $result.ok) {
            exit 1
        }
    }

    "task" {
        $taskId = Join-TaskText

        if (-not $taskId) {
            Write-Error "Task id cannot be empty."
            exit 1
        }

        $encodedTaskId = [uri]::EscapeDataString($taskId)
        $result = Invoke-SentinelApi -Uri "$BaseUrl/tasks/$encodedTaskId"
        $result | ConvertTo-Json -Depth 20


        if (-not $result.ok) {
            exit 1
        }
    }

    "wait" {
        $taskId = Join-TaskText

        if (-not $taskId) {
            Write-Error "Task ID cannot be empty."
            exit 1
        }

        # Progress already shown, so each poll only prints what is new.
        # Written to stderr, not stdout: stdout carries the result and
        # may be piped somewhere. Write-Host looked like it would do --
        # until a test running the script as a child process showed the
        # progress landing in the captured stdout alongside the result.
        $shownProgress = ""
        $failingSince = $null

        while ($true) {
            try {
                $response = Invoke-SentinelApi -Uri "$BaseUrl/tasks/$taskId" -SoftFail
            }
            catch {
                Write-Error $_
                exit 1
            }

            if ($Script:ChannelFailed) {
                # One poll that got no answer says nothing about the task,
                # which is still running on the host. Keep going until the
                # outage has lasted long enough to be a real one.
                if ($null -eq $failingSince) { $failingSince = Get-Date }

                if (((Get-Date) - $failingSince).TotalSeconds -ge $WaitToleranceSec) {
                    $Script:ActiveRequest = @{ Kind = "wait"; TaskId = $taskId }
                    Exit-ChannelDown -ErrorRecord $Script:LastChannelError `
                        -Uri "$BaseUrl/tasks/$taskId" -WaitedSec $QuickTimeoutSec
                }

                # Jittered, so callers that all lost the channel together
                # do not all come back in the same instant.
                Start-Sleep -Milliseconds (1500 + (Get-Random -Minimum 0 -Maximum 1000))
                continue
            }

            $failingSince = $null

            if (-not $response.ok) {
                Write-Error ($response | ConvertTo-Json -Depth 20)
                exit 1
            }

            $task = $response.task

            switch ($task.status) {

                "queued" {
                    Start-Sleep -Seconds 3
                }

                "running" {
                    if ($task.progress -and $task.progress -ne $shownProgress) {
                        # Normally an append, so print the tail. Not
                        # assumed though: an agent that rewrites the file
                        # instead would otherwise blow up Substring, and
                        # guessing the shape of data we do not produce is
                        # how several bugs in this repo started.
                        if ($shownProgress -and $task.progress.StartsWith($shownProgress)) {
                            $fresh = $task.progress.Substring($shownProgress.Length)
                        }
                        else {
                            $fresh = $task.progress
                        }

                        $fresh = $fresh.Trim()
                        if ($fresh) {
                            [Console]::Error.WriteLine($fresh)
                        }

                        $shownProgress = $task.progress
                    }

                    Start-Sleep -Seconds 3
                }

                "done" {
                    Write-Output $task.result_text
                    exit 0
                }

                "error" {
                    Write-Error $task.error_text
                    exit 1
                }

                "orphaned" {
                    Write-Warning "Task state is orphaned."
                    Write-Warning $task.error_text

                    if ($task.result_text) {
                        Write-Output $task.result_text
                    }

                    exit 2
                }

                "quota_exhausted" {
                    Write-Error $task.error_text
                    exit 3
                }

                default {
                    Write-Error "Unknown task status: $($task.status)"
                    exit 1
                }
            }
        }
    }
}
