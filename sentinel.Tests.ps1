#Requires -Modules Pester

<#
Black-box tests for sentinel.ps1. Because the script calls `exit` on several
branches (ask/prompt/delegate/wait failure paths), it cannot be safely
dot-sourced into the test process -- `exit` there would kill the whole test
run, not just the script. Instead every test launches sentinel.ps1 as a real
child process (same powershell.exe as the test host) against a throwaway
System.Net.HttpListener stub bound to a random loopback port, pointed at via
SENTINEL_BRIDGE_URL (see sentinel.ps1's $BaseUrl). This mirrors how
test_bridge.py's `live_server` fixture tests bridge.py over real HTTP rather
than mocking internals.
#>

BeforeAll {
    $Script:ScriptPath = Join-Path $PSScriptRoot "sentinel.ps1"
    $Script:HostExe = (Get-Process -Id $PID).Path

    function New-LoopbackPort {
        $tcp = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
        $tcp.Start()
        $port = $tcp.LocalEndpoint.Port
        $tcp.Stop()
        return $port
    }

    function Start-StubListener {
        $port = New-LoopbackPort
        $listener = New-Object System.Net.HttpListener
        $listener.Prefixes.Add("http://127.0.0.1:$port/")
        $listener.Start()

        return [pscustomobject]@{
            Listener = $listener
            Port     = $port
            BaseUrl  = "http://127.0.0.1:$port"
        }
    }

    function Start-SentinelUnderTest {
        param(
            [Parameter(Mandatory = $true)][string[]]$ScriptArgs,
            [Parameter(Mandatory = $true)][string]$BaseUrl,
            [string]$Token
        )

        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $Script:HostExe
        $psi.WorkingDirectory = $PSScriptRoot

        $quotedArgs = $ScriptArgs | ForEach-Object { '"' + ($_ -replace '"', '""') + '"' }
        $psi.Arguments = "-NoProfile -NonInteractive -File `"$Script:ScriptPath`" " + ($quotedArgs -join " ")

        $psi.EnvironmentVariables["SENTINEL_BRIDGE_URL"] = $BaseUrl
        if ($Token) {
            $psi.EnvironmentVariables["SENTINEL_BRIDGE_TOKEN"] = $Token
        } else {
            $psi.EnvironmentVariables.Remove("SENTINEL_BRIDGE_TOKEN")
        }

        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.UseShellExecute = $false

        return [System.Diagnostics.Process]::Start($psi)
    }

    function Receive-StubRequest {
        param([Parameter(Mandatory = $true)]$Listener)

        $context = $Listener.GetContext()

        $body = $null
        if ($context.Request.HasEntityBody) {
            $reader = New-Object System.IO.StreamReader($context.Request.InputStream, [Text.Encoding]::UTF8)
            $raw = $reader.ReadToEnd()
            $reader.Close()
            if ($raw) { $body = $raw | ConvertFrom-Json }
        }

        return [pscustomobject]@{
            Context = $context
            Method  = $context.Request.HttpMethod
            Path    = $context.Request.Url.AbsolutePath
            Token   = $context.Request.Headers["X-Sentinel-Token"]
            Body    = $body
        }
    }

    function Send-StubResponse {
        param(
            [Parameter(Mandatory = $true)]$Context,
            [int]$Status = 200,
            [Parameter(Mandatory = $true)]$Payload
        )

        $json = $Payload | ConvertTo-Json -Depth 10 -Compress
        $bytes = [Text.Encoding]::UTF8.GetBytes($json)

        $Context.Response.StatusCode = $Status
        $Context.Response.ContentType = "application/json; charset=utf-8"
        $Context.Response.ContentLength64 = $bytes.Length
        $Context.Response.OutputStream.Write($bytes, 0, $bytes.Length)
        $Context.Response.OutputStream.Close()
    }

    function Wait-SentinelExit {
        param([Parameter(Mandatory = $true)]$Process, [int]$TimeoutMs = 10000)

        if (-not $Process.WaitForExit($TimeoutMs)) {
            $Process.Kill()
            throw "sentinel.ps1 under test did not exit within ${TimeoutMs}ms"
        }

        return [pscustomobject]@{
            ExitCode = $Process.ExitCode
            StdOut   = $Process.StandardOutput.ReadToEnd()
            StdErr   = $Process.StandardError.ReadToEnd()
        }
    }
}

Describe "Join-TaskText (via 'ask' argument joining)" {
    It "joins multiple positional words with a single space and trims" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl `
                -ScriptArgs @("ask", "check", "disk", "usage")

            $req = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req.Context -Status 200 -Payload @{
                ok = $true; task_id = "t1"; result = @{ text = "42% used" }
            }

            $result = Wait-SentinelExit -Process $proc

            $req.Body.task | Should -Be "check disk usage"
            $result.ExitCode | Should -Be 0
        }
        finally {
            $stub.Listener.Stop()
        }
    }
}

Describe "health" {
    It "prints the bridge's /health response and exits 0" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("health")

            $req = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req.Context -Status 200 -Payload @{
                ok = $true; service = "nesi-sentinel-bridge"; version = 3
                agent = "sentinel"; worker_alive = $true
            }

            $result = Wait-SentinelExit -Process $proc

            $req.Method | Should -Be "GET"
            $req.Path | Should -Be "/health"
            $result.ExitCode | Should -Be 0
            ($result.StdOut | ConvertFrom-Json).ok | Should -Be $true
        }
        finally {
            $stub.Listener.Stop()
        }
    }
}

Describe "PowerShell HTTP error compatibility" {
    It "parses a 404 JSON body and exits 1 under both Windows PowerShell and PowerShell Core" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl `
                -ScriptArgs @("task", "missing-task")

            $req = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req.Context -Status 404 -Payload @{
                ok = $false; error = "task not found"
            }

            $result = Wait-SentinelExit -Process $proc

            $result.ExitCode | Should -Be 1
            $result.StdErr | Should -Not -Match "Invoke-RestMethod"
            ($result.StdOut | ConvertFrom-Json).error | Should -Be "task not found"
        }
        finally {
            $stub.Listener.Stop()
        }
    }
}

Describe "auth token header" {
    It "sends X-Sentinel-Token when SENTINEL_BRIDGE_TOKEN is set" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -Token "s3cret" `
                -ScriptArgs @("health")

            $req = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req.Context -Status 200 -Payload @{ ok = $true }

            Wait-SentinelExit -Process $proc | Out-Null

            $req.Token | Should -Be "s3cret"
        }
        finally {
            $stub.Listener.Stop()
        }
    }

    It "sends no X-Sentinel-Token header when the env var is unset" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("health")

            $req = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req.Context -Status 200 -Payload @{ ok = $true }

            Wait-SentinelExit -Process $proc | Out-Null

            $req.Token | Should -BeNullOrEmpty
        }
        finally {
            $stub.Listener.Stop()
        }
    }
}

Describe "ask" {
    It "exits 1 and reports the error when the bridge returns ok:false (e.g. 409 busy)" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl `
                -ScriptArgs @("ask", "check", "disk")

            $req = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req.Context -Status 409 -Payload @{
                ok = $false; status = "busy"; agent_status = "working"
            }

            $result = Wait-SentinelExit -Process $proc

            $result.ExitCode | Should -Be 1
            ($result.StdOut | ConvertFrom-Json).status | Should -Be "busy"
        }
        finally {
            $stub.Listener.Stop()
        }
    }
}

Describe "delegate" {
    It "omits timeout_ms from the request body when -TimeoutMs is not passed" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl `
                -ScriptArgs @("delegate", "check", "disk")

            $req = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req.Context -Status 202 -Payload @{
                ok = $true; task_id = "t1"; status = "queued"
            }

            Wait-SentinelExit -Process $proc | Out-Null

            $req.Body.PSObject.Properties.Name | Should -Not -Contain "timeout_ms"
        }
        finally {
            $stub.Listener.Stop()
        }
    }

    It "includes timeout_ms in the request body when -TimeoutMs is passed explicitly" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl `
                -ScriptArgs @("delegate", "-TimeoutMs", "5000", "check", "disk")

            $req = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req.Context -Status 202 -Payload @{
                ok = $true; task_id = "t1"; status = "queued"
            }

            Wait-SentinelExit -Process $proc | Out-Null

            $req.Body.timeout_ms | Should -Be 5000
        }
        finally {
            $stub.Listener.Stop()
        }
    }
}

Describe "wait" {
    It "polls until status becomes done, then prints result_text and exits 0" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("wait", "t1")

            $req1 = Receive-StubRequest -Listener $stub.Listener
            $req1.Path | Should -Be "/tasks/t1"
            Send-StubResponse -Context $req1.Context -Status 200 -Payload @{
                ok = $true; task = @{ status = "running" }
            }

            $req2 = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req2.Context -Status 200 -Payload @{
                ok = $true; task = @{ status = "done"; result_text = "disk ok" }
            }

            $result = Wait-SentinelExit -Process $proc -TimeoutMs 15000

            $result.ExitCode | Should -Be 0
            $result.StdOut.Trim() | Should -Be "disk ok"
        }
        finally {
            $stub.Listener.Stop()
        }
    }

    It "exits 2 and warns when the task lands in orphaned state" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("wait", "t1")

            $req = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req.Context -Status 200 -Payload @{
                ok = $true
                task = @{ status = "orphaned"; error_text = "timed out waiting" }
            }

            $result = Wait-SentinelExit -Process $proc

            $result.ExitCode | Should -Be 2
        }
        finally {
            $stub.Listener.Stop()
        }
    }
}

Describe "agents" {
    It "GETs /agents and prints the parsed list" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("agents")

            $req = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req.Context -Status 200 -Payload @{
                ok = $true
                agents = @(
                    @{ name = "sentinel-opencode"; agent_status = "working" }
                    @{ name = "sentinel"; agent_status = "idle" }
                )
            }

            $result = Wait-SentinelExit -Process $proc

            $req.Method | Should -Be "GET"
            $req.Path | Should -Be "/agents"
            $result.ExitCode | Should -Be 0
            ($result.StdOut | ConvertFrom-Json).agents.Count | Should -Be 2
        }
        finally {
            $stub.Listener.Stop()
        }
    }
}

Describe "-Agent parameter" {
    It "includes agent in the /delegate request body when -Agent is passed" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl `
                -ScriptArgs @("delegate", "-Agent", "sentinel-opencode", "check", "disk")

            $req = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req.Context -Status 202 -Payload @{
                ok = $true; task_id = "t1"; status = "queued"
            }

            Wait-SentinelExit -Process $proc | Out-Null

            $req.Body.agent | Should -Be "sentinel-opencode"
        }
        finally {
            $stub.Listener.Stop()
        }
    }

    It "omits agent from the /delegate request body when -Agent is not passed" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl `
                -ScriptArgs @("delegate", "check", "disk")

            $req = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req.Context -Status 202 -Payload @{
                ok = $true; task_id = "t1"; status = "queued"
            }

            Wait-SentinelExit -Process $proc | Out-Null

            $req.Body.PSObject.Properties.Name | Should -Not -Contain "agent"
        }
        finally {
            $stub.Listener.Stop()
        }
    }

    It "appends ?agent=... to the /ready request when -Agent is passed" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl `
                -ScriptArgs @("ready", "-Agent", "sentinel-opencode")

            $req = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req.Context -Status 200 -Payload @{
                ok = $true; ready = $true; agent_status = "idle"
            }

            Wait-SentinelExit -Process $proc | Out-Null

            $req.Path | Should -Be "/ready"
            $req.Context.Request.Url.Query | Should -Be "?agent=sentinel-opencode"
        }
        finally {
            $stub.Listener.Stop()
        }
    }

    It "sends -Lines and -Agent as encoded /read query parameters" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl `
                -ScriptArgs @("read", "-Lines", "37", "-Agent", "agent one")

            $req = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req.Context -Status 200 -Payload @{
                ok = $true; stdout = "output"; stderr = ""
            }

            $result = Wait-SentinelExit -Process $proc

            $result.ExitCode | Should -Be 0
            $req.Path | Should -Be "/read"
            $req.Context.Request.QueryString["lines"] | Should -Be "37"
            $req.Context.Request.QueryString["agent"] | Should -Be "agent one"
        }
        finally {
            $stub.Listener.Stop()
        }
    }
}
