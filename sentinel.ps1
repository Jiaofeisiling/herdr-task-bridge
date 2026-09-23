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
    [string]$SlurmPolicy
)

$Utf8 = New-Object System.Text.UTF8Encoding($false)

[Console]::InputEncoding  = $Utf8
[Console]::OutputEncoding = $Utf8
$OutputEncoding           = $Utf8

$BaseUrl = if ($env:SENTINEL_BRIDGE_URL) { $env:SENTINEL_BRIDGE_URL } else { "http://127.0.0.1:8765" }

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
$ReadQuery = "?" + ($ReadQueryParts -join "&")


function Join-TaskText {
    return ($Text -join " ").Trim()
}


# Classify a connection-layer failure without reading the exception text.
# That text is localised -- on a Chinese Windows it reads 由于目标计算机积极
# 拒绝 -- so matching on it would work on one machine and quietly fail on
# the next. SocketError and WebExceptionStatus are stable and language
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
function Exit-ChannelDown {
    param($ErrorRecord, [string]$Uri)

    $kind = Get-ChannelFailureKind -ErrorRecord $ErrorRecord

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
                "CHANNEL DOWN: connected to $Uri but it never replied.",
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

    foreach ($line in $lines) {
        [Console]::Error.WriteLine($line)
    }

    exit 4
}

function Invoke-SentinelApi {
    param(
        [Parameter(Mandatory=$true)][string]$Uri,
        [string]$Method = "Get",
        [string]$Body = $null
    )

    $headers = @{}

    if ($env:SENTINEL_BRIDGE_TOKEN) {
        $headers["X-Sentinel-Token"] = $env:SENTINEL_BRIDGE_TOKEN
    }

    try {
        if ($Body) {
            return Invoke-RestMethod -Uri $Uri -Method $Method `
                -ContentType "application/json; charset=utf-8" -Body $Body -Headers $headers
        }

        return Invoke-RestMethod -Uri $Uri -Method $Method -Headers $headers
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
            Exit-ChannelDown -ErrorRecord $caughtError -Uri $Uri
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

        $result = Invoke-SentinelApi -Uri "$BaseUrl/prompt" -Method Post -Body $body
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

        $result = Invoke-SentinelApi -Uri "$BaseUrl/ask" -Method Post -Body $body
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
        $result = Invoke-SentinelApi -Uri "$BaseUrl/tasks"
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

        while ($true) {
            try {
                $response = Invoke-SentinelApi -Uri "$BaseUrl/tasks/$taskId"
            }
            catch {
                Write-Error $_
                exit 1
            }

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
