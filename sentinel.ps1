param(
    [Parameter(Mandatory=$true, Position=0)]
    [ValidateSet("ask", "prompt", "status", "read", "health")]
    [string]$Command,

    [Parameter(Position=1, ValueFromRemainingArguments=$true)]
    [string[]]$Text,

    [int]$Lines = 80,

    [int]$TimeoutMs = 120000
)

$BaseUrl = "http://127.0.0.1:8765"


function Join-TaskText {
    return ($Text -join " ").Trim()
}


switch ($Command) {

    "health" {
        $result = Invoke-RestMethod "$BaseUrl/health"
        $result | ConvertTo-Json -Depth 10
    }

    "status" {
        $result = Invoke-RestMethod "$BaseUrl/status"

        if (-not $result.ok) {
            Write-Error $result.stderr
            exit 1
        }

        $result.stdout
    }

    "read" {
        $result = Invoke-RestMethod "$BaseUrl/read"

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

        $result = Invoke-RestMethod `
            -Uri "$BaseUrl/prompt" `
            -Method Post `
            -ContentType "application/json; charset=utf-8" `
            -Body $body

        if (-not $result.ok) {
            Write-Error $result.stderr
            exit 1
        }

        $result.stdout
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

        $result = Invoke-RestMethod `
            -Uri "$BaseUrl/ask" `
            -Method Post `
            -ContentType "application/json; charset=utf-8" `
            -Body $body

        if (-not $result.ok) {
            Write-Error "Sentinel task failed."
            $result | ConvertTo-Json -Depth 10
            exit 1
        }

        $result | ConvertTo-Json -Depth 20
    }
}
