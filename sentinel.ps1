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
        "agents"
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
    [string]$Agent
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
            throw $caughtError
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

                default {
                    Write-Error "Unknown task status: $($task.status)"
                    exit 1
                }
            }
        }
    }
}
