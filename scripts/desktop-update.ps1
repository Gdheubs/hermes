# desktop-update.ps1 -- repo-owned Windows Desktop update hand-off.
#
# WHY THIS EXISTS (the frozen-binary problem): the Desktop's Update button
# used to hand off exclusively to the staged Tauri binary
# (%HERMES_HOME%\hermes-setup.exe). That binary has no self-update path --
# copy_self_to_hermes_home deliberately no-ops during --update -- so every
# updater-side fix (cache refresh #67369, marker self-adopt #74782, straggler
# handling) only reaches users when a new installer is built, signed, and
# published. In practice binaries go months stale and users hit long-fixed
# bugs on every update (the 2026-08-09 incident chain).
#
# This script lives in the repo checkout, so EVERY `hermes update` refreshes
# the very code that drives the next update. The Desktop spawns it through a
# `cmd start` wrapper (see wrapHandoffForDetachedConsole in
# apps/desktop/electron/updater-process.ts -- a bare detached+hidden
# powershell dies before -File runs) and exits; only PowerShell itself -- an
# OS component -- is "frozen".
#
# CONTRACT (keep in sync with apps/desktop/electron/main.ts):
#   cmd /d /s /c start "" /min powershell -NoProfile -ExecutionPolicy Bypass
#     -File scripts\desktop-update.ps1
#     -InstallRoot <path>   repo checkout (HERMES_HOME\hermes-agent)
#     -Branch <ref>         branch to update against
#     -DesktopPid <pid>     the Electron main process to wait out
#     [-RelaunchExe <path>] Hermes.exe to start when done (omit = no relaunch)
#     [-NoUi]               headless (tests); default shows a progress window
#     [-NoMarkerCleanup]    leave .hermes-update-in-progress in place (tests)
#     [-SelfTestUi]         serve the progress UI with simulated phases and
#                           exit (manual QA / CI smoke; no update is run)
#
# PROGRESS UI: the primary surface is scripts/desktop-update-ui.html rendered
# in an Edge app-mode window (chromeless; msedge ships on every Win10/11 box,
# so no WebView2 SDK DLLs and no frozen binary). The script serves the page
# plus a /progress JSON endpoint from an in-process loopback TCP listener.
# When Edge or the listener is unavailable it degrades to the previous
# WinForms window, then to log-only — the update itself never depends on UI.
#
# SAFETY POSTURE: both preflight gates FAIL CLOSED. A Desktop that never
# exits, or a venv shim that never unlocks, aborts the hand-off without
# mutating the install -- a skipped update is recoverable, a half-updated
# venv is not. Every exit path (success, abort, crash) writes
# .hermes-update-result.json for the relaunched Desktop to surface, and
# relaunches the Desktop so the user is never left stranded.
#
# Marker: we claim HERMES_HOME\.hermes-update-in-progress with OUR pid as
# step 0 (the wrapper cmd.exe pid the Desktop saw is useless -- it exits
# immediately). hermes_cli/update_lock.py's ancestry rule lets our
# `hermes update` child adopt the claim; electron/update-marker.ts parks a
# relaunched Desktop on it. Cleanup only removes the marker while WE still
# own it (a handoff partner that rewrote it keeps its claim).

param(
    [string]$InstallRoot = "",
    [string]$Branch = "main",
    [int]$DesktopPid = 0,
    [string]$RelaunchExe = "",
    [switch]$NoUi,
    [switch]$NoMarkerCleanup,
    [switch]$SelfTestUi
)

if (-not $SelfTestUi -and -not $InstallRoot) {
    # Mandatory in spirit; relaxed in the signature only so -SelfTestUi can run
    # without a checkout (the UI smoke test needs no install to point at).
    throw "-InstallRoot is required"
}

$ErrorActionPreference = "Continue"
# Foreground helpers: the script is spawned via `cmd start /min`, so its
# WinForms window comes up backgrounded unless we explicitly claim focus --
# and after the update we must hand focus TO the relaunched Desktop (a
# WMI-spawned process starts unfocused). AllowSetForegroundWindow lets us
# pass our foreground right on to the new Hermes.exe pid.
try {
    Add-Type -Namespace HermesHandoff -Name Win32 -MemberDefinition @'
[DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr hWnd);
[DllImport("user32.dll")] public static extern bool AllowSetForegroundWindow(int dwProcessId);
[DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr hWnd, int nCmdShow);
'@ -ErrorAction Stop
    $script:Win32 = $true
} catch { $script:Win32 = $false }
# Render UTF-8 glyphs (checkmarks, arrows) correctly in our own console echo
# too; the legacy conhost default OEM codepage shows them as mojibake.
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}
$HermesHome = if ($InstallRoot) { Split-Path -Parent $InstallRoot } else { $env:TEMP }
$MarkerPath = Join-Path $HermesHome ".hermes-update-in-progress"
$LogDir = Join-Path $HermesHome "logs"
$LogPath = Join-Path $LogDir "desktop-update-handoff.log"
$ResultPath = Join-Path $HermesHome ".hermes-update-result.json"
$script:Ui = $null

# ── Progress UI: loopback server + Edge app-mode shell ─────────────────────
# State shared with the poller thread. Synchronized so the listener thread
# reads a consistent snapshot while the main thread appends lines.
$script:UiState = [hashtable]::Synchronized(@{
    status  = "running"      # running | done | error
    phase   = "prepare"      # prepare | wait-desktop | wait-venv | update | rebuild
    message = "Starting update..."
    lines   = [System.Collections.ArrayList]::Synchronized([System.Collections.ArrayList]::new())
})
$script:UiServer = $null     # @{ Listener; Runspace; PowerShell; Port; EdgeProc }

function Set-UiPhase([string]$Phase, [string]$Message) {
    $script:UiState.phase = $Phase
    if ($Message) { $script:UiState.message = $Message }
}

function Get-UiHtmlPath {
    # Lives next to this script in the checkout. Missing file = fall back to
    # WinForms (old checkouts mid-update, partial syncs).
    $p = Join-Path $PSScriptRoot "desktop-update-ui.html"
    if (Test-Path -LiteralPath $p) { return $p }
    return $null
}

function Find-EdgeExe {
    foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA)) {
        if (-not $root) { continue }
        $p = Join-Path $root "Microsoft\Edge\Application\msedge.exe"
        if (Test-Path -LiteralPath $p) { return $p }
    }
    return $null
}

function Start-UiServer([string]$HtmlPath) {
    # In-process HTTP on a loopback ephemeral port, served from a dedicated
    # runspace so the main thread never blocks on Accept. Plain TcpListener
    # instead of HttpListener: no URL ACL / netsh reservation semantics to
    # trip over, and two GET routes don't need more.
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
        $listener.Start()
        $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port

        $rs = [runspacefactory]::CreateRunspace()
        $rs.Open()
        $rs.SessionStateProxy.SetVariable("Listener", $listener)
        $rs.SessionStateProxy.SetVariable("State", $script:UiState)
        $rs.SessionStateProxy.SetVariable("HtmlBytes", [System.IO.File]::ReadAllBytes($HtmlPath))

        $ps = [powershell]::Create()
        $ps.Runspace = $rs
        [void]$ps.AddScript({
            function Send-Response($Stream, [string]$Status, [string]$ContentType, [byte[]]$Body) {
                $head = "HTTP/1.1 $Status`r`nContent-Type: $ContentType`r`nContent-Length: $($Body.Length)`r`nCache-Control: no-store`r`nConnection: close`r`n`r`n"
                $headBytes = [System.Text.Encoding]::ASCII.GetBytes($head)
                $Stream.Write($headBytes, 0, $headBytes.Length)
                $Stream.Write($Body, 0, $Body.Length)
                $Stream.Flush()
            }
            while ($true) {
                try { $client = $Listener.AcceptTcpClient() } catch { break }  # Stop() ends the loop
                try {
                    $client.ReceiveTimeout = 2000
                    $stream = $client.GetStream()
                    $reader = [System.IO.StreamReader]::new($stream, [System.Text.Encoding]::ASCII, $false, 1024, $true)
                    $request = $reader.ReadLine()
                    # Drain headers so the client doesn't see a reset mid-send.
                    while ($true) { $h = $reader.ReadLine(); if ($null -eq $h -or $h -eq "") { break } }
                    if ($request -match "^GET /progress") {
                        $snapshot = @{
                            status  = $State.status
                            phase   = $State.phase
                            message = $State.message
                            lines   = @($State.lines.ToArray())
                        } | ConvertTo-Json -Compress
                        Send-Response $stream "200 OK" "application/json; charset=utf-8" ([System.Text.Encoding]::UTF8.GetBytes($snapshot))
                    } elseif ($request -match "^GET / ") {
                        Send-Response $stream "200 OK" "text/html; charset=utf-8" $HtmlBytes
                    } else {
                        Send-Response $stream "404 Not Found" "text/plain" ([System.Text.Encoding]::ASCII.GetBytes("not found"))
                    }
                } catch {
                    # Per-connection failure: drop it, keep serving.
                } finally {
                    try { $client.Close() } catch {}
                }
            }
        })
        [void]$ps.BeginInvoke()

        return @{ Listener = $listener; Runspace = $rs; PowerShell = $ps; Port = $port; EdgeProc = $null }
    } catch {
        try { if ($listener) { $listener.Stop() } } catch {}
        return $null
    }
}

function Stop-UiServer {
    if (-not $script:UiServer) { return }
    try { $script:UiServer.Listener.Stop() } catch {}
    try { $script:UiServer.PowerShell.Stop() } catch {}
    try { $script:UiServer.Runspace.Close() } catch {}
    # Closing Edge is cosmetic; if the user closed it already this is a no-op,
    # and if it lingers the page shows the terminal state until they close it.
    try {
        if ($script:UiServer.EdgeProc -and -not $script:UiServer.EdgeProc.HasExited) {
            $script:UiServer.EdgeProc.CloseMainWindow() | Out-Null
        }
    } catch {}
    $script:UiServer = $null
}

function Write-HandoffLog([string]$Message) {
    $line = "{0:yyyy-MM-ddTHH:mm:ssK} {1}" -f (Get-Date), $Message
    try { Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8 } catch {}
    Write-Host $line
    # Feed the web UI regardless of which shell rendered (Edge polls /progress;
    # cap retained lines so a huge pip install can't grow the JSON unboundedly).
    try {
        [void]$script:UiState.lines.Add($Message)
        while ($script:UiState.lines.Count -gt 400) { $script:UiState.lines.RemoveAt(0) }
    } catch {}
    if ($script:Ui) {
        try {
            $script:Ui.Box.AppendText($Message + "`r`n")
            [System.Windows.Forms.Application]::DoEvents()
        } catch {}
    }
}

function Show-ProgressWindow {
    if ($NoUi) { return }

    # ── Primary: repo-owned HTML in an Edge app-mode window ────────────────
    # Chromeless (--app), so it reads as a native progress dialog, not a
    # browser. Serving over loopback HTTP (not file://) keeps fetch() inside
    # the page's origin without any browser flag overrides.
    $htmlPath = Get-UiHtmlPath
    $edge = Find-EdgeExe
    if ($htmlPath -and $edge) {
        $server = Start-UiServer $htmlPath
        if ($server) {
            try {
                # Dedicated tiny profile dir: guarantees a NEW WINDOW + process
                # we own (a default-profile launch delegates to an existing
                # Edge and returns instantly, leaving nothing to close), and
                # avoids touching the user's real browser profile.
                $edgeProfile = Join-Path $env:TEMP ("hermes-update-ui-{0}" -f $PID)
                $edgeArgs = @(
                    "--app=http://127.0.0.1:$($server.Port)/",
                    "--user-data-dir=$edgeProfile",
                    "--no-first-run", "--no-default-browser-check",
                    "--disable-features=msImplicitSignin",
                    "--window-size=760,480"
                )
                $server.EdgeProc = Start-Process -FilePath $edge -ArgumentList $edgeArgs -PassThru
                $script:UiServer = $server
                Write-HandoffLog "progress UI: Edge app window on 127.0.0.1:$($server.Port)"
                return
            } catch {
                try { $server.Listener.Stop() } catch {}
                # fall through to WinForms
            }
        }
    }

    # ── Fallback: legacy WinForms window (Edge missing/failed) ─────────────
    try {
        Add-Type -AssemblyName System.Windows.Forms | Out-Null
        Add-Type -AssemblyName System.Drawing | Out-Null
        $form = New-Object System.Windows.Forms.Form
        $form.Text = "Hermes Update"
        $form.Size = New-Object System.Drawing.Size(720, 420)
        $form.StartPosition = "CenterScreen"
        $form.ControlBox = $false
        $form.TopMost = $true
        $label = New-Object System.Windows.Forms.Label
        $label.Text = "Updating Hermes -- do not close this window. Hermes restarts automatically when the update finishes."
        $label.Dock = "Top"
        $label.Height = 34
        $label.Padding = New-Object System.Windows.Forms.Padding(8, 8, 8, 0)
        $bar = New-Object System.Windows.Forms.ProgressBar
        $bar.Style = "Marquee"
        $bar.MarqueeAnimationSpeed = 30
        $bar.Dock = "Top"
        $bar.Height = 18
        $box = New-Object System.Windows.Forms.TextBox
        $box.Multiline = $true
        $box.ReadOnly = $true
        $box.ScrollBars = "Vertical"
        $box.Dock = "Fill"
        $box.Font = New-Object System.Drawing.Font("Consolas", 9)
        $form.Controls.Add($box)
        $form.Controls.Add($bar)
        $form.Controls.Add($label)
        $form.Show()
        # `cmd start /min` spawned us backgrounded; TopMost keeps the window
        # above others but does not take activation. Claim it explicitly so
        # the progress window is what the user sees during the update.
        try {
            $form.Activate()
            if ($script:Win32) { [HermesHandoff.Win32]::SetForegroundWindow($form.Handle) | Out-Null }
        } catch {}
        [System.Windows.Forms.Application]::DoEvents()
        $script:Ui = [pscustomobject]@{ Form = $form; Box = $box }
    } catch {
        # Headless session / WinForms unavailable: degrade to log-only.
        $script:Ui = $null
    }
}

function Close-ProgressWindow {
    if ($script:UiServer) {
        # Let the page render the terminal state before the server dies: the
        # poller runs every 400ms, so two beats is enough. The page also
        # handles the server vanishing gracefully (shows last known state).
        Start-Sleep -Milliseconds 900
        Stop-UiServer
    }
    if ($script:Ui) {
        try { $script:Ui.Form.Close() } catch {}
        $script:Ui = $null
    }
}

function Write-Result([bool]$Ok, [int]$Code, [string]$Message) {
    # Consumed (read + deleted) by the relaunched Desktop on boot so the
    # user actually SEES how a detached update ended.
    try {
        $obj = @{
            ok         = $Ok
            exit_code  = $Code
            message    = $Message
            branch     = $Branch
            finished_at = [int][double]::Parse((Get-Date -UFormat %s), [System.Globalization.CultureInfo]::InvariantCulture)
        } | ConvertTo-Json -Compress
        [System.IO.File]::WriteAllText($ResultPath, $obj)
    } catch {}
}

function Remove-MarkerIfOwned {
    if ($NoMarkerCleanup) { return }
    try {
        if (Test-Path -LiteralPath $MarkerPath) {
            $firstLine = (Get-Content -LiteralPath $MarkerPath -TotalCount 1 -ErrorAction SilentlyContinue)
            if ("$firstLine".Trim() -eq "$PID") {
                Remove-Item -LiteralPath $MarkerPath -Force -ErrorAction SilentlyContinue
                Write-HandoffLog "removed update marker (owned)"
            } else {
                Write-HandoffLog "leaving update marker: owned by pid '$firstLine', not us ($PID)"
            }
        }
    } catch {}
}

function Start-DesktopRelaunch {
    if ($RelaunchExe -and (Test-Path -LiteralPath $RelaunchExe)) {
        Write-HandoffLog "relaunching desktop: $RelaunchExe"
        # DO NOT spawn Hermes.exe as our child: Electron/Chromium calls
        # AttachConsole(ATTACH_PARENT_PROCESS) at boot, so a Desktop launched
        # directly from this console PowerShell latches onto OUR console --
        # the console window then outlives the script (it can't close while
        # an attached process lives), and closing it kills the freshly
        # relaunched GUI with it. Create the process via WMI instead: the
        # parent becomes WmiPrvSE.exe and there is no console to inherit or
        # attach -- same detachment explorer.exe gives a normal launch.
        $spawned = $false
        try {
            $workDir = Split-Path -Parent $RelaunchExe
            $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
                CommandLine      = ('"{0}"' -f $RelaunchExe)
                CurrentDirectory = $workDir
            } -ErrorAction Stop
            if ($r -and $r.ReturnValue -eq 0) {
                Write-HandoffLog "desktop relaunched detached (pid $($r.ProcessId))"
                $spawned = $true
                # Hand our foreground rights to the new Desktop and focus its
                # main window once it exists. A WMI-spawned process starts
                # unfocused, and Windows only lets the CURRENT foreground
                # owner (us, while the progress window is up / just closed)
                # delegate that right. Poll briefly for the window: Electron
                # takes a couple seconds to create it.
                try {
                    if ($script:Win32) {
                        [HermesHandoff.Win32]::AllowSetForegroundWindow([int]$r.ProcessId) | Out-Null
                        $deadline = (Get-Date).AddSeconds(20)
                        while ((Get-Date) -lt $deadline) {
                            $hwnd = [System.IntPtr]::Zero
                            try {
                                $p = Get-Process -Id $r.ProcessId -ErrorAction Stop
                                $hwnd = $p.MainWindowHandle
                            } catch { break }  # process died; nothing to focus
                            if ($hwnd -ne [System.IntPtr]::Zero) {
                                [HermesHandoff.Win32]::ShowWindow($hwnd, 9) | Out-Null  # SW_RESTORE
                                [HermesHandoff.Win32]::SetForegroundWindow($hwnd) | Out-Null
                                Write-HandoffLog "focused relaunched desktop window"
                                break
                            }
                            Start-Sleep -Milliseconds 400
                        }
                    }
                } catch {
                    Write-HandoffLog "WARNING: could not focus relaunched desktop: $($_.Exception.Message)"
                }
            } else {
                Write-HandoffLog "WARNING: WMI relaunch returned $($r.ReturnValue); falling back"
            }
        } catch {
            Write-HandoffLog "WARNING: WMI relaunch failed: $($_.Exception.Message); falling back"
        }
        if (-not $spawned) {
            try {
                # Fallback keeps the old behavior (console tie-in and all) --
                # a tethered Desktop beats no Desktop.
                Start-Process -FilePath $RelaunchExe -WorkingDirectory (Split-Path -Parent $RelaunchExe) | Out-Null
            } catch {
                Write-HandoffLog "WARNING: desktop relaunch failed: $($_.Exception.Message)"
            }
        }
    }
}

function Invoke-StreamedHermes([string]$Exe, [string[]]$HermesArgs, [string]$Tag) {
    # Start-Process + output file + poll keeps the WinForms window pumping
    # during long silent stretches (pip installs); a blocking pipeline would
    # freeze the marquee. Returns @{ Code; Output }.
    $outFile = Join-Path $env:TEMP ("hermes-handoff-{0}-{1}.out" -f $Tag, $PID)
    $errFile = Join-Path $env:TEMP ("hermes-handoff-{0}-{1}.err" -f $Tag, $PID)
    Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue
    # System.Diagnostics.Process directly: Start-Process's .ExitCode is
    # unreliably $null under PS 5.1 even with the Handle-touch workaround.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    # .Arguments string (PS 5.1 / .NET Framework has no ArgumentList).
    # Args here are fixed flags + a branch ref; quote each defensively.
    $psi.Arguments = ($HermesArgs | ForEach-Object { '"{0}"' -f ($_ -replace '"', '\"') }) -join ' '
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    # hermes update prints UTF-8 (checkmarks, arrows, box glyphs). PS 5.1
    # defaults these readers to the OEM codepage, which mangles every
    # multi-byte glyph into mojibake in the console AND the progress box.
    $psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    # And ask the child to actually EMIT UTF-8: Python decides its stdio
    # encoding from the console codepage when attached to one.
    $psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8"
    $psi.EnvironmentVariables["PYTHONUTF8"] = "1"
    $psi.CreateNoWindow = $true
    $proc = [System.Diagnostics.Process]::Start($psi)
    $outWriter = [System.IO.File]::CreateText($outFile)
    $errWriter = [System.IO.File]::CreateText($errFile)
    # Pump synchronously in small reads so the UI stays alive; stderr is
    # drained at the end (hermes update is stdout-dominant).
    while (-not $proc.HasExited) {
        while (-not $proc.StandardOutput.EndOfStream) {
            $ln = $proc.StandardOutput.ReadLine()
            if ($null -ne $ln) {
                $outWriter.WriteLine($ln)
                if ($ln.Trim()) { Write-HandoffLog ("{0}| {1}" -f $Tag, $ln) }
            }
            if ($script:Ui) { [System.Windows.Forms.Application]::DoEvents() }
        }
        Start-Sleep -Milliseconds 150
        if ($script:Ui) { [System.Windows.Forms.Application]::DoEvents() }
    }
    while (-not $proc.StandardOutput.EndOfStream) {
        $ln = $proc.StandardOutput.ReadLine()
        if ($null -ne $ln) {
            $outWriter.WriteLine($ln)
            if ($ln.Trim()) { Write-HandoffLog ("{0}| {1}" -f $Tag, $ln) }
        }
    }
    $errText = $proc.StandardError.ReadToEnd()
    if ($errText) {
        $errWriter.Write($errText)
        foreach ($ln in ($errText -split "`r?`n")) {
            if ($ln.Trim()) { Write-HandoffLog ("{0}!| {1}" -f $Tag, $ln) }
        }
    }
    $outWriter.Close(); $errWriter.Close()
    $proc.WaitForExit()
    $code = $proc.ExitCode
    $all = ""
    try { $all = [System.IO.File]::ReadAllText($outFile) } catch {}
    if ($errText) { $all += "`n" + $errText }
    Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue
    return @{ Code = $code; Output = $all }
}

$finalCode = 1
$finalMsg = "update did not complete"

# ── -SelfTestUi: drive the progress UI through simulated phases ────────────
# Manual QA / smoke for the Edge shell without running a real update. Exits
# before the marker/desktop/venv machinery — touches nothing.
if ($SelfTestUi) {
    Show-ProgressWindow
    Write-HandoffLog "SELF-TEST: progress UI simulation (no update will run)"
    $simPhases = @(
        @{ id = "prepare";      msg = "Preparing update..." },
        @{ id = "wait-desktop"; msg = "Waiting for Hermes to close..." },
        @{ id = "wait-venv";    msg = "Waiting for the install lock..." },
        @{ id = "update";       msg = "Running hermes update..." },
        @{ id = "rebuild";      msg = "Rebuilding the desktop app..." }
    )
    foreach ($p in $simPhases) {
        Set-UiPhase $p.id $p.msg
        for ($i = 1; $i -le 6; $i++) {
            Write-HandoffLog ("{0}| simulated output line {1}" -f $p.id, $i)
            Start-Sleep -Milliseconds 500
        }
    }
    $script:UiState.status = "done"
    $script:UiState.message = "Self-test complete."
    Write-HandoffLog "SELF-TEST: terminal state rendered; closing in 5s"
    Start-Sleep -Seconds 5
    Stop-UiServer
    exit 0
}

try {
    New-Item -ItemType Directory -Path $LogDir -Force -ErrorAction SilentlyContinue | Out-Null
    Remove-Item -LiteralPath $ResultPath -Force -ErrorAction SilentlyContinue
    Show-ProgressWindow
    Write-HandoffLog "hand-off start: root=$InstallRoot branch=$Branch desktopPid=$DesktopPid pid=$PID"

    # -- 0. Claim the update marker with OUR pid ---------------------------
    try {
        $epoch = [int][double]::Parse((Get-Date -UFormat %s), [System.Globalization.CultureInfo]::InvariantCulture)
        # WriteAllText for byte-exact LF framing: Set-Content emits CRLF and
        # the marker contract (Rust/TS/Python readers) is "<pid>\n<ts>\n".
        [System.IO.File]::WriteAllText($MarkerPath, "$PID`n$epoch`n")
        Write-HandoffLog "claimed update marker (pid $PID)"
    } catch {
        Write-HandoffLog "WARNING: could not write update marker: $($_.Exception.Message)"
    }

    # -- 1. Wait for the Desktop to exit (FAIL CLOSED) ----------------------
    if ($DesktopPid -gt 0) {
        Set-UiPhase "wait-desktop" "Waiting for Hermes to close..."
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Date) -lt $deadline) {
            $proc = Get-Process -Id $DesktopPid -ErrorAction SilentlyContinue
            if (-not $proc) { break }
            Start-Sleep -Milliseconds 300
            if ($script:Ui) { [System.Windows.Forms.Application]::DoEvents() }
        }
        if (Get-Process -Id $DesktopPid -ErrorAction SilentlyContinue) {
            # A live Desktop means a live backend re-locking the venv at any
            # moment. Updating under it is how installs brick. Abort.
            $finalCode = 4
            $finalMsg = "Update aborted: the Hermes window (pid $DesktopPid) did not exit within 30s. Nothing was changed. Close Hermes fully and try again."
            Write-HandoffLog $finalMsg
            exit $finalCode
        }
        Write-HandoffLog "desktop exited"
    }

    # -- 2. Wait for the venv shim to unlock (FAIL CLOSED) ------------------
    Set-UiPhase "wait-venv" "Waiting for the install lock to release..."
    $shim = Join-Path $InstallRoot "venv\Scripts\hermes.exe"
    if (Test-Path -LiteralPath $shim) {
        $unlocked = $false
        $deadline = (Get-Date).AddSeconds(20)
        while ((Get-Date) -lt $deadline) {
            try {
                $fs = [System.IO.File]::Open($shim, 'Open', 'ReadWrite', 'None')
                $fs.Close()
                $unlocked = $true
                break
            } catch {
                Start-Sleep -Milliseconds 400
                if ($script:Ui) { [System.Windows.Forms.Application]::DoEvents() }
            }
        }
        if (-not $unlocked) {
            # Something still maps the venv. --force-ing past it guarantees a
            # half-updated venv (the exact 2026-08-09 Access-denied brick).
            $finalCode = 5
            $finalMsg = "Update aborted: another process is still holding the Hermes install open (venv\Scripts\hermes.exe locked after 20s). Nothing was changed. Close other Hermes windows/terminals and try again."
            Write-HandoffLog $finalMsg
            exit $finalCode
        }
        Write-HandoffLog "venv shim unlocked"
    }

    # -- 3. Run the update from the CURRENT checkout ------------------------
    # --force skips only the hermes.exe shim guard, which step 2 just PROVED
    # is unlocked; the venv-python holder guard (orphan reap included) stays
    # active. Our marker claim is adopted by the child via update_lock.py's
    # process-ancestry rule.
    $hermesExe = Join-Path $InstallRoot "venv\Scripts\hermes.exe"
    if (-not (Test-Path -LiteralPath $hermesExe)) {
        $finalCode = 3
        $finalMsg = "Update aborted: $hermesExe is missing. The install needs repair (run the Hermes installer or `hermes doctor`)."
        Write-HandoffLog $finalMsg
        exit $finalCode
    }
    $updateArgs = @("update", "--yes", "--gateway", "--force", "--branch", $Branch)
    Set-UiPhase "update" "Installing update (this can take a few minutes)..."
    Write-HandoffLog ("running: hermes " + ($updateArgs -join " "))
    $res = Invoke-StreamedHermes $hermesExe $updateArgs "update"
    Write-HandoffLog "hermes update exit code: $($res.Code)"

    if ($res.Code -ne 0 -and $res.Code -ne 2) {
        # One retry for the update-boundary class (fresh code on disk, stale
        # code in memory). Exit 2 ("close all Hermes windows") is not retryable.
        Write-HandoffLog "first attempt failed; retrying once (freshly pulled fix loads on the second run)"
        $res = Invoke-StreamedHermes $hermesExe $updateArgs "update"
        Write-HandoffLog "retry exit code: $($res.Code)"
    }

    # -- 4. Truthful completion: don't trust exit 0 -------------------------
    # `hermes update` treats a Desktop GUI build failure as NON-fatal (prints
    # a one-line warning, exits 0). For a Desktop-DRIVEN update that warning
    # is fatal: we would relaunch the old exe and call it success. Detect it,
    # retry the build once, and propagate honestly.
    $desktopBuildFailed = $false
    if ($res.Code -eq 0 -and $res.Output -match "Desktop build failed") {
        Write-HandoffLog "hermes update reported a desktop build failure (non-fatal there, fatal here); retrying build"
        Set-UiPhase "rebuild" "Rebuilding the desktop app..."
        $rebuild = Invoke-StreamedHermes $hermesExe @("desktop", "--force-build", "--build-only") "rebuild"
        Write-HandoffLog "desktop rebuild exit code: $($rebuild.Code)"
        if ($rebuild.Code -ne 0) { $desktopBuildFailed = $true }
    }

    if ($res.Code -eq 0 -and -not $desktopBuildFailed) {
        $finalCode = 0
        $finalMsg = "Update complete."
    } elseif ($desktopBuildFailed) {
        $finalCode = 6
        $finalMsg = "Code and dependencies updated, but the Desktop app REBUILD FAILED - you are running the previous build. Run `hermes desktop --force-build` from a terminal to retry."
    } else {
        $finalCode = $res.Code
        $finalMsg = "hermes update failed (exit $($res.Code)). See logs\desktop-update-handoff.log."
    }
    exit $finalCode
} finally {
    # Push the terminal state to the web UI before tearing it down so the
    # user sees "complete"/"failed", not a page that just vanishes.
    $script:UiState.status = if ($finalCode -eq 0) { "done" } else { "error" }
    $script:UiState.message = $finalMsg
    Write-Result ($finalCode -eq 0) $finalCode $finalMsg
    Remove-MarkerIfOwned
    Close-ProgressWindow
    Start-DesktopRelaunch
}
