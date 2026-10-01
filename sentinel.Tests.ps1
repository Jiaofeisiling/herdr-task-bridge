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
            [string]$Token,
            [hashtable]$Env = @{}
        )

        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $Script:HostExe
        $psi.WorkingDirectory = $PSScriptRoot

        $quotedArgs = $ScriptArgs | ForEach-Object { '"' + ($_ -replace '"', '""') + '"' }
        $psi.Arguments = "-NoProfile -NonInteractive -File `"$Script:ScriptPath`" " + ($quotedArgs -join " ")

        $psi.EnvironmentVariables["SENTINEL_BRIDGE_URL"] = $BaseUrl
        foreach ($name in $Env.Keys) {
            $psi.EnvironmentVariables[$name] = [string]$Env[$name]
        }
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
        # TimeoutMs bounds the wait. GetContext() alone blocks forever, so a
        # test expecting a request that never comes -- precisely what
        # happens against a client that has the bug under test -- would
        # hang the whole suite rather than fail. Returns $null on timeout.
        param([Parameter(Mandatory = $true)]$Listener, [int]$TimeoutMs = 0)

        if ($TimeoutMs -gt 0) {
            $pending = $Listener.GetContextAsync()
            if (-not $pending.Wait($TimeoutMs)) { return $null }
            $context = $pending.Result
        }
        else {
            $context = $Listener.GetContext()
        }

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

    It "shows only newly appended progress while the task is still running" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("wait", "t1")

            $req1 = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req1.Context -Status 200 -Payload @{
                ok = $true; task = @{ status = "running"; progress = "submitted job 9213894" }
            }

            $req2 = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req2.Context -Status 200 -Payload @{
                ok = $true
                task = @{ status = "running"; progress = "submitted job 9213894`nnow RUNNING" }
            }

            $req3 = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req3.Context -Status 200 -Payload @{
                ok = $true; task = @{ status = "done"; result_text = "finished" }
            }

            $result = Wait-SentinelExit -Process $proc -TimeoutMs 20000

            $result.ExitCode | Should -Be 0
            # Progress goes to the host, not stdout: stdout carries the
            # result and may be piped somewhere.
            $result.StdOut.Trim() | Should -Be "finished"
        }
        finally {
            $stub.Listener.Stop()
        }
    }

    It "survives a progress file that was rewritten rather than appended to" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("wait", "t1")

            $req1 = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req1.Context -Status 200 -Payload @{
                ok = $true; task = @{ status = "running"; progress = "step one" }
            }

            # Not a superset of what came before. Assuming append-only
            # would take Substring past the end here, and guessing the
            # shape of data the client does not produce is how several
            # bugs in this repo started.
            $req2 = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req2.Context -Status 200 -Payload @{
                ok = $true; task = @{ status = "running"; progress = "x" }
            }

            $req3 = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req3.Context -Status 200 -Payload @{
                ok = $true; task = @{ status = "done"; result_text = "finished" }
            }

            $result = Wait-SentinelExit -Process $proc -TimeoutMs 20000

            $result.ExitCode | Should -Be 0
            $result.StdOut.Trim() | Should -Be "finished"
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

    It "exits 3 when all eligible agents are quota exhausted" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("wait", "t1")

            $req = Receive-StubRequest -Listener $stub.Listener
            Send-StubResponse -Context $req.Context -Status 200 -Payload @{
                ok = $true
                task = @{
                    status = "quota_exhausted"
                    error_text = "all eligible fallback agents are quota exhausted"
                }
            }

            $result = Wait-SentinelExit -Process $proc

            $result.ExitCode | Should -Be 3
            $result.StdErr | Should -Match "quota exhausted"
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

Describe "channel failures" {
    # A caller reported the remote as "not recovered" while the bridge was
    # answering fine. Chasing that found the client's side of it: a
    # connection failure printed a raw Invoke-RestMethod stack trace and
    # still exited 0, so there was nothing to act on and nothing for a
    # script to branch on either.

    It "exits 4 rather than 0 when nothing is listening" {
        $proc = Start-SentinelUnderTest -BaseUrl "http://127.0.0.1:9" -ScriptArgs @("health")
        $result = Wait-SentinelExit -Process $proc -TimeoutMs 30000

        # Exiting 0 on a failed connection tells every caller it worked.
        $result.ExitCode | Should -Be 4
    }

    It "says what to do instead of printing a PowerShell stack trace" {
        $proc = Start-SentinelUnderTest -BaseUrl "http://127.0.0.1:9" -ScriptArgs @("health")
        $result = Wait-SentinelExit -Process $proc -TimeoutMs 30000

        $output = "$($result.StdOut)$($result.StdErr)"

        # The forward is the thing that breaks, and reconnecting is the
        # fix; a stack trace pointing at Invoke-RestMethod is neither.
        $output | Should -Match "VS Code|forward|tunnel"
        $output | Should -Not -Match "Invoke-RestMethod -Uri"
    }

    It "does not blame the bridge for a channel that is down" {
        $proc = Start-SentinelUnderTest -BaseUrl "http://127.0.0.1:9" -ScriptArgs @("health")
        $result = Wait-SentinelExit -Process $proc -TimeoutMs 30000

        $output = "$($result.StdOut)$($result.StdErr)"

        # Nothing was listening, so the bridge process was never reached
        # and its health is simply unknown. Saying otherwise is what sent
        # the caller off reporting a remote outage that had not happened.
        $output | Should -Match "not reached|unknown"
    }
}


Describe "bounded client calls" {
    # Found by reading what a real caller actually received. Across 3,396
    # recorded calls, health took 1.9 s at the median; every empty result
    # took 30.2 s -- exactly the caller's own yield time, i.e. the command
    # had not returned when the caller gave up and reported it blocked.
    # They clustered: 61% fell in three hours, so these were outages and
    # not noise. The cause was that sentinel.ps1 set no timeout at all.
    # PowerShell 7's default is infinite, so a forward that still accepts
    # connections but no longer reaches the host hung the client until it
    # was killed -- and the CHANNEL DOWN message that was supposed to
    # explain it could never be reached in time to be read.

    BeforeAll {
        # Small so the tests are quick; the real default is larger.
        $Script:Fast = @{ SENTINEL_CLIENT_TIMEOUT_SEC = "2" }
    }

    It "health returns within a bound when nothing ever answers it" {
        $stub = Start-StubListener
        try {
            $clock = [System.Diagnostics.Stopwatch]::StartNew()
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("health") -Env $Script:Fast

            # Accept the request and say nothing: the half-dead forward.
            $null = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000

            $result = Wait-SentinelExit -Process $proc -TimeoutMs 20000
            $clock.Stop()

            $result.ExitCode | Should -Be 4
            $clock.Elapsed.TotalSeconds | Should -BeLessThan 12
        }
        finally {
            $stub.Listener.Stop()
        }
    }

    It "says so plainly instead of leaving the caller with an empty result" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("health") -Env $Script:Fast
            $null = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000

            $result = Wait-SentinelExit -Process $proc -TimeoutMs 20000
            $output = "$($result.StdOut)$($result.StdErr)"

            $output | Should -Match "CHANNEL DOWN"
            $output | Should -Match "VS Code"
        }
        finally {
            $stub.Listener.Stop()
        }
    }

    It "tells a stalled request apart from a dead channel by probing /health" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("ready") -Env $Script:Fast

            # The request stalls...
            $null = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000

            # ...but the bridge's own liveness endpoint still answers.
            $probe = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000
            $probe.Path | Should -Be "/health"
            Send-StubResponse -Context $probe.Context -Status 200 -Payload @{ ok = $true; version = 1 }

            $result = Wait-SentinelExit -Process $proc -TimeoutMs 20000
            $output = "$($result.StdOut)$($result.StdErr)"

            # Exit 5, not 4. Reporting this as a dead channel would send the
            # reader to reconnect a tunnel that is working; the bridge is up
            # and one request behind it is stuck.
            $result.ExitCode | Should -Be 5
            $output | Should -Match "NO REPLY"
            $output | Should -Not -Match "CHANNEL DOWN"
        }
        finally {
            $stub.Listener.Stop()
        }
    }

    It "reports a dead channel when the probe is silent too" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("ready") -Env $Script:Fast
            $null = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000
            $null = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000   # the /health probe, ignored too

            $result = Wait-SentinelExit -Process $proc -TimeoutMs 20000

            $result.ExitCode | Should -Be 4
        }
        finally {
            $stub.Listener.Stop()
        }
    }

    It "does not cut a legitimately slow ask off at the quick-call bound" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl `
                -ScriptArgs @("ask", "-TimeoutMs", "30000", "check disk") -Env $Script:Fast

            $req = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000

            # Four seconds against a two-second quick bound. ask is allowed
            # to take as long as the caller said it may, and the client has
            # to outlive the bridge's own timeout or it abandons work that
            # is proceeding normally.
            Start-Sleep -Seconds 4
            Send-StubResponse -Context $req.Context -Status 200 -Payload @{
                ok = $true; result = @{ text = "disk ok" }
            }

            $result = Wait-SentinelExit -Process $proc -TimeoutMs 20000

            $result.ExitCode | Should -Be 0
        }
        finally {
            $stub.Listener.Stop()
        }
    }
}

Describe "retrying a request that may already have run" {
    # The previous change told callers to "retry once" when a request got no
    # reply. For a read that is right. For delegate it is a trap: a timeout
    # cannot say whether the task was queued -- the request may have landed
    # and only the reply been lost -- so retrying can enqueue it twice, and
    # with Slurm submission allowed by default that is a duplicate job.

    BeforeAll {
        $Script:Fast = @{ SENTINEL_CLIENT_TIMEOUT_SEC = "2" }
    }

    It "sends an idempotency key with every delegate" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("delegate", "check disk")
            $req = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000
            Send-StubResponse -Context $req.Context -Status 202 -Payload @{ ok = $true; task_id = "t1"; status = "queued" }
            $null = Wait-SentinelExit -Process $proc -TimeoutMs 15000

            $req.Body.idempotency_key | Should -Not -BeNullOrEmpty
        }
        finally { $stub.Listener.Stop() }
    }

    It "uses the key the caller supplies, so a retry can reuse it" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl `
                -ScriptArgs @("delegate", "-IdempotencyKey", "op-42", "check disk")
            $req = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000
            Send-StubResponse -Context $req.Context -Status 202 -Payload @{ ok = $true; task_id = "t1"; status = "queued" }
            $null = Wait-SentinelExit -Process $proc -TimeoutMs 15000

            $req.Body.idempotency_key | Should -Be "op-42"
        }
        finally { $stub.Listener.Stop() }
    }

    It "tells a caller whose delegate got no reply how to retry safely" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl `
                -ScriptArgs @("delegate", "-IdempotencyKey", "op-42", "check disk") -Env $Script:Fast
            $null = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000      # stalls
            $probe = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000     # /health probe
            Send-StubResponse -Context $probe.Context -Status 200 -Payload @{ ok = $true }

            $result = Wait-SentinelExit -Process $proc -TimeoutMs 20000
            $output = "$($result.StdOut)$($result.StdErr)"

            $result.ExitCode | Should -Be 5
            # The key, and the instruction to reuse it...
            $output | Should -Match "op-42"
            $output | Should -Match "MAY HAVE BEEN QUEUED"
            $output | Should -Match "-IdempotencyKey"
            # ...and no longer the blanket advice that is unsafe here.
            $output | Should -Not -Match "Retry once"
        }
        finally { $stub.Listener.Stop() }
    }

    It "warns that ask may already have run, instead of inviting a retry" {
        $stub = Start-StubListener
        try {
            # TimeoutMs of 1 s keeps the client bound short for the test.
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl `
                -ScriptArgs @("ask", "-TimeoutMs", "1000", "check disk") `
                -Env @{ SENTINEL_CLIENT_TIMEOUT_SEC = "2"; SENTINEL_ASK_GRACE_SEC = "1" }
            $null = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000
            $probe = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000
            Send-StubResponse -Context $probe.Context -Status 200 -Payload @{ ok = $true }

            $result = Wait-SentinelExit -Process $proc -TimeoutMs 30000
            $output = "$($result.StdOut)$($result.StdErr)"

            $output | Should -Match "MAY HAVE ALREADY RUN"
            $output | Should -Not -Match "Retry once"
        }
        finally { $stub.Listener.Stop() }
    }

    It "does not put a delegate's key on ask, which has no use for one" {
        # Caught while writing this change: a patch anchored on a substring
        # inserted the key into the wrong command. Pinned so it cannot
        # quietly return.
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("ask", "check disk")
            $req = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000
            Send-StubResponse -Context $req.Context -Status 200 -Payload @{ ok = $true; result = @{ text = "ok" } }
            $null = Wait-SentinelExit -Process $proc -TimeoutMs 15000

            $req.Body.PSObject.Properties.Name | Should -Not -Contain "idempotency_key"
        }
        finally { $stub.Listener.Stop() }
    }

    It "still says a read-only command is safe to retry" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("ready") -Env $Script:Fast
            $null = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000
            $probe = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000
            Send-StubResponse -Context $probe.Context -Status 200 -Payload @{ ok = $true }

            $result = Wait-SentinelExit -Process $proc -TimeoutMs 20000

            "$($result.StdOut)$($result.StdErr)" | Should -Match "Retry once"
        }
        finally { $stub.Listener.Stop() }
    }
}

Describe "wait rides out a transient channel failure" {
    # wait polls for as long as a task takes, which can be hours. With
    # the new bound, a single poll that got no answer made it exit 4 -- so a
    # five-second tunnel blip abandoned the wait for a task that was still
    # running perfectly well. Before the bound it hung instead; neither is
    # right for something that is meant to outlast brief outages.

    It "keeps polling after one poll gets no reply" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("wait", "t1") `
                -Env @{ SENTINEL_CLIENT_TIMEOUT_SEC = "2"; SENTINEL_WAIT_TOLERANCE_SEC = "30" }

            $null = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000    # stalls

            $next = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 20000    # the re-poll
            $next.Path | Should -Be "/tasks/t1"
            Send-StubResponse -Context $next.Context -Status 200 -Payload @{
                ok = $true; task = @{ status = "done"; result_text = "finished" }
            }

            $result = Wait-SentinelExit -Process $proc -TimeoutMs 30000

            $result.ExitCode | Should -Be 0
            $result.StdOut.Trim() | Should -Be "finished"
        }
        finally { $stub.Listener.Stop() }
    }

    It "gives up with exit 4 once the outage outlasts the tolerance" {
        $stub = Start-StubListener
        try {
            # Nothing is ever received or answered: a channel that stays dead.
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("wait", "t1") `
                -Env @{ SENTINEL_CLIENT_TIMEOUT_SEC = "2"; SENTINEL_WAIT_TOLERANCE_SEC = "4" }

            $result = Wait-SentinelExit -Process $proc -TimeoutMs 40000
            $output = "$($result.StdOut)$($result.StdErr)"

            $result.ExitCode | Should -Be 4
            # The task was never touched, and the message has to say so --
            # otherwise the caller assumes the work was lost.
            $output | Should -Match "still"
            $output | Should -Match "wait t1|task id"
        }
        finally { $stub.Listener.Stop() }
    }
}

Describe "a bridge that answers with an error is still a bridge that answers" {
    # /health now returns 503 when the bridge's worker thread has died. The
    # liveness probe treated every exception as "no answer", so a bridge that
    # was up but unhealthy would have been reported as a dead channel -- and
    # the reader sent to reconnect a tunnel that was working perfectly.

    It "does not call it a dead channel when the probe gets a 503" {
        $stub = Start-StubListener
        try {
            $proc = Start-SentinelUnderTest -BaseUrl $stub.BaseUrl -ScriptArgs @("ready") `
                -Env @{ SENTINEL_CLIENT_TIMEOUT_SEC = "2" }

            $null = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000      # stalls
            $probe = Receive-StubRequest -Listener $stub.Listener -TimeoutMs 15000     # /health
            Send-StubResponse -Context $probe.Context -Status 503 -Payload @{
                ok = $false; reason = "worker_dead"; worker_alive = $false
            }

            $result = Wait-SentinelExit -Process $proc -TimeoutMs 20000
            $output = "$($result.StdOut)$($result.StdErr)"

            # Exit 5: the bridge was reached. Not 4, which says it was not.
            $result.ExitCode | Should -Be 5
            $output | Should -Not -Match "CHANNEL DOWN"
            # And it says what is actually wrong, rather than guessing a herdr call.
            $output | Should -Match "worker_dead"
            $output | Should -Match "restart"
        }
        finally { $stub.Listener.Stop() }
    }
}
