param(
    [Parameter(Mandatory=$true, Position=0)]
    [ValidateSet(
        "ask",
        "prompt",
        "delegate",
        "task",
        "tasks",
        "status",
        "ready",
        "read",
        "health"
    )]
    [string]$Command,

    [Parameter(Position=1, ValueFromRemainingArguments=$true)]
    [string[]]$Text,

    [int]$Lines = 80,

    [int]$TimeoutMs = 120000
)

$Utf8 = New-Object System.Text.UTF8Encoding($false)

[Console]::InputEncoding  = $Utf8
[Console]::OutputEncoding = $Utf8
$OutputEncoding           = $Utf8

$BaseUrl = "http://127.0.0.1:8765"


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
    catch [System.Net.WebException] {
        $errResponse = $_.Exception.Response

        if ($null -eq $errResponse) {
            throw
        }

        # In Windows PowerShell 5.1, Invoke-RestMethod has already
        # consumed the response stream by the time control reaches this
        # catch block -- re-reading $errResponse.GetResponseStream() here
        # reliably returns an empty string (confirmed against a live 409:
        # the whole function returned nothing, silently). PowerShell
        # already captured the body for us in $_.ErrorDetails.Message,
        # so use that instead of re-reading the (already-drained) stream.
        if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
            return $_.ErrorDetails.Message | ConvertFrom-Json
        }

        # Fallback for the rare case ErrorDetails wasn't populated.
        $stream = $errResponse.GetResponseStream()
        $reader = New-Object System.IO.StreamReader($stream)
        $rawBody = $reader.ReadToEnd()
        $reader.Close()

        return $rawBody | ConvertFrom-Json
    }
}


switch ($Command) {

    "health" {
        $result = Invoke-SentinelApi -Uri "$BaseUrl/health"
        $result | ConvertTo-Json -Depth 10
    }

    "status" {
        $result = Invoke-SentinelApi -Uri "$BaseUrl/status"

        if (-not $result.ok) {
            Write-Error $result.stderr
            exit 1
        }

        $result.stdout
    }

    "read" {
        $result = Invoke-SentinelApi -Uri "$BaseUrl/read"

        if (-not $result.ok) {
            Write-Error $result.stderr
            exit 1
        }

        $result.stdout
    }

    "prompt" {
        $task = Join-TaskText

        if (-not $task) {
            Write-Error "Prompt cannot be empty."
            exit 1
        }

        $body = @{
            task       = $task
            timeout_ms = $TimeoutMs
        } | ConvertTo-Json -Compress

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

        $body = @{
            task       = $task
            timeout_ms = $TimeoutMs
            lines      = 500
        } | ConvertTo-Json -Compress

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

        $body = $payload | ConvertTo-Json -Compress

        $result = Invoke-SentinelApi -Uri "$BaseUrl/delegate" -Method Post -Body $body
        $result | ConvertTo-Json -Depth 20

        if (-not $result.ok) {
            exit 1
        }
    }

    "ready" {
        $result = Invoke-SentinelApi -Uri "$BaseUrl/ready"
        $result | ConvertTo-Json -Depth 10
    }

    "tasks" {
        $result = Invoke-SentinelApi -Uri "$BaseUrl/tasks"
        $result | ConvertTo-Json -Depth 20
    }

    "task" {
        $taskId = Join-TaskText

        if (-not $taskId) {
            Write-Error "Task id cannot be empty."
            exit 1
        }

        $result = Invoke-SentinelApi -Uri "$BaseUrl/tasks/$taskId"
        $result | ConvertTo-Json -Depth 20
    }
}
