using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using Serilog;
using Serilog.Extensions.Logging;
using SIPSorcery.Net;
using TempMonitorAgent.State;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Entry point for the session-injected helper (roadmap #2). The service launches THIS SAME
/// BINARY with <see cref="AgentConfig.RemoteHelperArg"/> as SYSTEM inside the interactive
/// session (see <see cref="SessionInjector"/>); <see cref="Program"/> branches here before the
/// Windows Service host is ever built.
///
/// <b>Threading is load-bearing here, not incidental.</b> Capture and input both need the
/// calling thread attached to the current input desktop (see <see cref="InputDesktopWatcher"/>),
/// and <c>SetThreadDesktop</c> is per-thread and refuses any thread that owns a window. So:
///   * capture runs on a dedicated <c>remote-capture</c> thread with its own binder;
///   * input runs on a dedicated <c>remote-input</c> thread with its own binder, fed by an
///     <see cref="InputQueue"/> because the control-channel callback fires on a SIPSorcery
///     thread we neither own nor may rebind;
///   * the consent prompt gets its own throwaway thread, because showing it permanently
///     disqualifies that thread from ever attaching to a desktop again.
/// None of these may be thread-pool threads: a pool thread poisoned by one session would come
/// back to break the next.
///
/// It logs to its own file (<see cref="AgentConfig.RemoteHelperLogPath"/>), not companion.log,
/// because it runs in a different session and its diagnostics should be legible on their own.
/// </summary>
public static class RemoteHelper
{
    /// <summary>How often the helper polls the hub for the console's answer + trickled ICE +
    /// status.</summary>
    private const int PollIntervalMs = 800;

    /// <summary>How often we re-check which desktop is the input desktop. Fast enough that the
    /// operator does not watch a frozen frame through a lock transition, cheap enough to ignore.</summary>
    private const int DesktopPollMs = 250;

    /// <summary>If this process was launched as the remote helper, return the session-file
    /// path that followed <see cref="AgentConfig.RemoteHelperArg"/> (empty string if the flag
    /// was passed with no value). Returns null for a normal service launch, so Program.cs can
    /// tell the two apart before building the service host.</summary>
    public static string? TryGetSessionFileArg(string[] args)
    {
        for (int i = 0; i < args.Length; i++)
            if (string.Equals(args[i], AgentConfig.RemoteHelperArg, StringComparison.Ordinal))
                return i + 1 < args.Length ? args[i + 1] : "";
        return null;
    }

    /// <summary>If this process was launched as the capture self-test, return the arguments
    /// that followed <c>--remote-capture-test</c>; else null. The self-test writes an Annex-B
    /// .h264 file so the capture + encode pipeline can be validated on a real machine with no
    /// hub involved.</summary>
    public static string[]? TryGetCaptureTestArgs(string[] args)
    {
        for (int i = 0; i < args.Length; i++)
            if (string.Equals(args[i], "--remote-capture-test", StringComparison.Ordinal))
                return args[(i + 1)..];
        return null;
    }

    /// <summary>True if this process was launched as the desktop-tracking diagnostic.</summary>
    public static bool IsDesktopProbe(string[] args) =>
        args.Any(a => string.Equals(a, "--desktop-probe", StringComparison.Ordinal));

    /// <summary>Runs the capture self-test:
    /// <c>--remote-capture-test [outputPath] [seconds] [monitor] [fps] [bitrateKbps] [encoder]</c>,
    /// where <c>encoder</c> is auto (default), hardware or software.</summary>
    public static int RunCaptureSelfTest(string[] rest)
    {
        string outPath = rest.Length > 0 && rest[0].Length > 0
            ? rest[0]
            : Path.Combine(AgentConfig.ProgramDataDir, "remote-capture-test.h264");
        int seconds = rest.Length > 1 && int.TryParse(rest[1], out var s) ? s : 5;
        int monitor = rest.Length > 2 && int.TryParse(rest[2], out var m) ? m : 0;
        int fps = rest.Length > 3 && int.TryParse(rest[3], out var f) ? f : 15;
        int kbps = rest.Length > 4 && int.TryParse(rest[4], out var k) ? k : 4000;
        var preference = rest.Length > 5 ? rest[5].ToLowerInvariant() switch
        {
            "hardware" => EncoderPreference.Hardware,
            "software" => EncoderPreference.Software,
            _ => EncoderPreference.Auto,
        } : EncoderPreference.Auto;

        void Say(string msg) => Console.WriteLine("[remote-capture-test] " + msg);
        Say($"output={outPath} seconds={seconds} monitor={monitor} fps={fps} bitrate={kbps}kbps " +
            $"encoder={preference.ToString().ToLowerInvariant()}");
        var displays = DisplayProbe.ProbeFromSession();
        Say($"displays: {displays.ActiveOutputs} output(s) " +
            $"[{string.Join(", ", displays.OutputNames)}], " +
            $"{displays.PhysicalMonitors} physical monitor(s), " +
            $"virtual display {(displays.VirtualDisplayPresent ? "present" : "absent")}");
        if (displays.Headless)
            Say("WARNING: this machine reports no monitors and no virtual display. Expect a " +
                "black capture until a virtual display is installed.");
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(outPath))!);
            int frames = CaptureEncodePipeline.RunToFile(
                outPath, seconds, monitor, fps, kbps * 1000, Say, preference);
            Say(frames > 0 ? $"done, {frames} frames. Play with: ffplay \"{outPath}\"" : "no frames produced");
            return frames > 0 ? 0 : 3;
        }
        catch (Exception e)
        {
            Say("FAILED: " + e);
            return 1;
        }
        finally
        {
            MediaFoundationRuntime.Shutdown();
        }
    }

    /// <summary>
    /// Runs the desktop-tracking diagnostic: <c>--desktop-probe [seconds]</c>.
    ///
    /// This exists because everything about capturing the lock screen rests on one assumption --
    /// that this process can see and follow the switch to the Winlogon desktop -- and that
    /// assumption is cheap to test and expensive to debug through a full remote session. Run it,
    /// press Win+L, trigger a UAC prompt, and watch the reported desktop change.
    ///
    /// Run it session-injected (the service does that) to test the real conditions; run it
    /// directly from a console to see what an ordinary user-token process is allowed to observe.
    /// </summary>
    public static int RunDesktopProbe(string[] args)
    {
        int seconds = 60;
        for (int i = 0; i < args.Length - 1; i++)
            if (args[i] == "--desktop-probe" && int.TryParse(args[i + 1], out var s)) seconds = s;

        void Say(string msg) =>
            Console.WriteLine($"[desktop-probe {DateTime.Now:HH:mm:ss}] {msg}");

        Say(Describe());
        Say($"watching the input desktop for {seconds}s -- lock the screen (Win+L) and trigger a " +
            "UAC prompt to exercise the transitions.");

        using var watcher = new InputDesktopWatcher(DesktopPollMs, Say);
        watcher.Changed += (from, to) =>
            Say($"INPUT DESKTOP CHANGED: {(from.Length == 0 ? "(none)" : from)} -> {to}");
        watcher.Start();

        // Bind this thread too, so the probe also reports whether attaching actually WORKS --
        // observing the switch is necessary but not sufficient.
        using var binder = new ThreadDesktopBinder("probe", Say);
        var deadline = DateTime.UtcNow.AddSeconds(seconds);
        string? lastReport = null;
        while (DateTime.UtcNow < deadline)
        {
            if (binder.SyncTo(watcher))
                Say($"attached to desktop '{binder.AttachedName}' " +
                    $"(outputs visible: {DisplayProbe.OutputCount()})");
            string report = $"input='{watcher.Name}' attached='{binder.AttachedName}' " +
                            $"failures={binder.ConsecutiveFailures}";
            if (report != lastReport) { Say(report); lastReport = report; }
            Thread.Sleep(DesktopPollMs);
        }
        Say("done.");
        return 0;
    }

    /// <summary>Run the helper. <paramref name="sessionFilePath"/> is the file the executor
    /// wrote the session parameters to; it is read then deleted (single use). Returns a
    /// process exit code.</summary>
    public static int Run(string? sessionFilePath)
    {
        ConfigureLogger();
        try
        {
            var self = Describe();
            Log.Information("Remote helper started. {Self}", self);

            var session = LoadSession(sessionFilePath, out var loadNote);
            if (loadNote is { Length: > 0 }) Log.Warning("{Note}", loadNote);
            if (session is null)
            {
                Log.Error("No usable session parameters; exiting.");
                return 2;
            }

            Log.Information(
                "Session {SessionId}: monitor={Monitor} consent={Consent} issued_by={IssuedBy} " +
                "ice_servers={Ice} codec={Codec} encoder={Encoder} {Fps}fps {Kbps}kbps scale={Scale}%",
                session.SessionId, session.Monitor, session.ConsentMode, session.IssuedBy,
                session.IceServers.Count, session.Codec, session.Encoder,
                session.Fps, session.BitrateKbps, session.Scale);

            // The helper signals as the enrolled agent, reusing the identity the service wrote
            // (agent.json). Without it there is no way to authenticate to the signaling relay.
            var identity = new AgentState().LoadIdentity();
            if (!identity.IsEnrolled)
            {
                Log.Error("Agent is not enrolled; cannot reach the signaling relay. Exiting.");
                return 3;
            }

            return RunSessionAsync(session, identity.BearerValue).GetAwaiter().GetResult();
        }
        catch (Exception e)
        {
            Log.Error(e, "Remote helper terminated unexpectedly");
            return 1;
        }
        finally
        {
            // After every encoder is gone -- MF is refcounted per process, so this is the one
            // place it may be torn down.
            MediaFoundationRuntime.Shutdown();
            Log.CloseAndFlush();
        }
    }

    /// <summary>Run one remote session: create the WebRTC offer, stream captured video to the
    /// peer, and relay signaling with the console until the session ends (the hub reports it
    /// ended/expired, the console sends bye, or the peer connection drops).</summary>
    private static async Task<int> RunSessionAsync(RemoteSessionParams session, string bearer)
    {
        using var cts = new CancellationTokenSource();
        using var signaling = new RemoteSignalingClient(session.SessionId, bearer);

        // Attended consent: the logged-in user must approve before anything is captured or sent.
        // Unattended (the default) skips this. A denial or timeout fails closed.
        if (string.Equals(session.ConsentMode, "attended", StringComparison.OrdinalIgnoreCase))
        {
            Log.Information("Attended consent required; prompting the logged-in user.");
            // Its own thread, not the pool: the prompt creates a window, and a thread that owns
            // a window can never attach to another desktop again.
            bool approved = await ConsentBanner.RequestConsentAsync(
                AgentConfig.MachineName, session.IssuedBy);
            if (!approved)
            {
                Log.Information("Consent denied or timed out; ending session.");
                // A denial FINISHES the session, so clear the supervision record on the way out
                // exactly as the normal teardown below does. Without this the record outlives the
                // process, the supervisor reads "the helper died involuntarily", and re-injects a
                // fresh helper that prompts again -- the user presses Deny and is asked again a
                // few seconds later, up to MaxRelaunches times. Telling the hub is not enough:
                // the replacement helper prompts before it ever polls, so it never learns the
                // session it is asking about is already ended.
                RemoteSessionSupervisor.Untrack(session.SessionId);
                try { await signaling.ReportEndedAsync("consent denied", CancellationToken.None); }
                catch { /* the TTL sweep is the backstop */ }
                return 0;
            }
            Log.Information("Consent granted.");
        }

        var settings = new LiveStreamSettings(session.ToStreamSettings());

        // Re-fetch the ICE servers from the hub before building the peer: the list in the start
        // command was chosen for wherever the OPERATOR was sitting, and the credential in it is
        // as old as the command (which matters for a helper the supervisor relaunched into a new
        // Windows session an hour into a four-hour session). A hub too old to answer, or any
        // transport failure, keeps the command's copy -- see GetIceServersAsync.
        var iceServers = await signaling.GetIceServersAsync(cts.Token) ?? session.IceServers;
        if (!ReferenceEquals(iceServers, session.IceServers))
            Log.Information("Hub re-issued {Count} ICE server(s) for this machine's vantage.",
                            iceServers.Count);
        foreach (var s in iceServers)
            Log.Information("ICE server: {Urls}{Auth}", string.Join(", ", s.Urls),
                            string.IsNullOrEmpty(s.Username) ? "" : " (credentialed)");

        using var peer = new RemotePeer(iceServers, msg => Log.Information("{Msg}", msg),
                                        session.ParsedCodec);

        peer.OnConnectionStateChange += state =>
        {
            if (state is RTCPeerConnectionState.failed or RTCPeerConnectionState.closed
                      or RTCPeerConnectionState.disconnected)
            {
                Log.Information("Peer {State}; ending session.", state);
                cts.Cancel();
            }
        };
        peer.OnLocalIceCandidate += payload =>
        {
            // Fire-and-forget: an HTTP POST must not block SIPSorcery's ICE-gathering thread.
            _ = Task.Run(async () =>
            {
                try { await signaling.PostSignalAsync("ice", payload, cts.Token); }
                catch (Exception e) { Log.Warning("posting local ICE failed: {Msg}", e.Message); }
            });
        };

        // One watcher for the process; each desktop-bound thread gets its own binder.
        using var desktops = new InputDesktopWatcher(DesktopPollMs, m => Log.Information("{Msg}", m));
        desktops.Changed += (from, to) =>
            Log.Information("Input desktop changed: {From} -> {To}",
                            from.Length == 0 ? "(none)" : from, to);
        desktops.Start();

        using var inputQueue = new InputQueue(log: m => Log.Warning("{Msg}", m));
        peer.OnControlMessage += msg =>
        {
            // Runs on a SIPSorcery thread: enqueue only. Live settings changes are handled on
            // the input thread so every Win32 call stays on a desktop-attached thread.
            inputQueue.Enqueue(msg);
        };
        await peer.EnableControlChannelAsync();

        // Offer first, then start streaming, then poll for the answer + remote ICE.
        var offer = await peer.CreateOfferAsync();
        int offerSeq = await signaling.PostSignalAsync("offer", offer, cts.Token);
        Log.Information("Posted offer (seq {Seq}); starting capture and awaiting the console's answer.",
                        offerSeq);

        // Geometry the capture loop last reported, read by the input thread so its coordinate
        // mapping follows the stream. A plain volatile cell rather than a callback: resolving
        // monitor geometry must happen ON the input thread, never on the capture thread.
        var pendingGeometry = new GeometrySignal();

        var captureThread = StartThread("remote-capture", () =>
        {
            using var binder = new ThreadDesktopBinder("capture", m => Log.Information("{Msg}", m));
            CaptureEncodePipeline.RunToPeer(
                peer, settings, cts.Token, m => Log.Information("{Msg}", m),
                desktops, binder,
                onGeometry: g =>
                {
                    pendingGeometry.Set(g);
                    peer.SendControl(JsonSerializer.Serialize(new
                    {
                        t = "geom",
                        w = g.Width,
                        h = g.Height,
                        desktop = g.Desktop,
                        encoder = g.Encoder,
                        monitor = g.Monitor,
                        monitors = g.MonitorCount,
                    }));
                },
                onStall: desktop =>
                {
                    Log.Warning("Capture has produced nothing for several seconds on desktop {Desktop}",
                                string.IsNullOrEmpty(desktop) ? "?" : desktop);
                    peer.SendControl(JsonSerializer.Serialize(new
                    {
                        t = "capture",
                        state = "stalled",
                        desktop,
                    }));
                });
        }, cts);

        var inputThread = StartThread("remote-input",
            () => RunInputLoop(inputQueue, settings, pendingGeometry, desktops, cts.Token), cts);

        // Start reading AFTER our own offer, not from the beginning. The hub keeps every signal
        // for the life of the session, and this helper may be a replacement for one the
        // supervisor stopped or that died with its Windows session (sign-in, sign-out, switch
        // user). Those signals answer the PREVIOUS peer: applying that stale answer to this
        // brand-new peer sets the wrong ICE credentials and DTLS fingerprint on it, and the real
        // answer that follows is then rejected as a second remote description -- a reconnect
        // that hangs forever rather than one that fails visibly.
        int afterSeq = offerSeq;
        while (!cts.IsCancellationRequested)
        {
            RemoteSignalingClient.PollResult poll;
            try { poll = await signaling.PollAsync(afterSeq, cts.Token); }
            catch (OperationCanceledException) { break; }
            catch (Exception e) { Log.Warning("signaling poll failed: {Msg}", e.Message); await Delay(cts.Token); continue; }

            afterSeq = poll.NextSeq;
            foreach (var sig in poll.Signals) HandleSignal(peer, sig, cts);
            if (poll.Status is "ended" or "expired")
            {
                Log.Information("Session {Status} by the hub; tearing down.", poll.Status);
                break;
            }
            await Delay(cts.Token);
        }

        cts.Cancel();
        inputQueue.Complete();
        // Authoritatively end the hub session so it doesn't linger until the TTL sweep; the
        // browser sees status "ended" on its next poll and tears down.
        try { await signaling.ReportEndedAsync("agent teardown", CancellationToken.None); } catch { }
        peer.Close();
        JoinQuietly(captureThread);
        JoinQuietly(inputThread);
        // Reaching here means the session finished on purpose. Clearing the supervision record
        // is what tells the service not to resurrect the helper -- if we are killed instead
        // (the operator signs in and this Windows session is destroyed), the record survives and
        // the supervisor brings us back in the new session.
        RemoteSessionSupervisor.Untrack(session.SessionId);
        Log.Information("Remote session {SessionId} ended.", session.SessionId);
        return 0;
    }

    /// <summary>
    /// Drains input events and applies them, on a thread bound to the current input desktop.
    ///
    /// This thread also owns live settings changes from the viewer, even though they are not
    /// input: they arrive on the same control channel, and handling them here keeps every
    /// message from that channel on one thread with one ordering.
    /// </summary>
    private static void RunInputLoop(
        InputQueue queue, LiveStreamSettings settings, GeometrySignal geometry,
        InputDesktopWatcher desktops, CancellationToken ct)
    {
        using var binder = new ThreadDesktopBinder("input", m => Log.Information("{Msg}", m));
        var input = new InputInjector(settings.Current.Monitor);

        while (!ct.IsCancellationRequested)
        {
            // Re-resolve geometry after a desktop switch: EnumDisplayMonitors reports the
            // CALLING thread's desktop, and the lock screen can be a different resolution.
            bool switched = binder.SyncTo(desktops);
            bool moved = geometry.TryTake(out var g);
            if (switched || moved)
                input.RefreshGeometry(moved ? g.Monitor : null);

            if (!queue.TryTake(out var msg, 100)) continue;
            try
            {
                if (TryApplyConfig(msg, settings)) continue;
                input.Apply(msg);
            }
            catch (Exception e) { Log.Warning("input event failed: {Msg}", e.Message); }
        }
    }

    /// <summary>Handle a <c>{"t":"cfg", ...}</c> message from the viewer -- a live quality
    /// change. Returns true if the message was config (and therefore not input).
    ///
    /// Only fps, bitrate, scale and monitor are live. Codec and encoder choice are fixed at
    /// session start because they are negotiated in the SDP / decide which encoder object
    /// exists, so a "live" change would have to renegotiate the whole peer connection.</summary>
    private static bool TryApplyConfig(string json, LiveStreamSettings settings)
    {
        JsonElement e;
        try { e = JsonDocument.Parse(json).RootElement; }
        catch { return false; }
        if (e.ValueKind != JsonValueKind.Object) return false;
        if (!e.TryGetProperty("t", out var t) || t.GetString() != "cfg") return false;

        var applied = settings.Update(current => current with
        {
            Fps = Int(e, "fps") ?? current.Fps,
            BitrateBps = Int(e, "bitrate_kbps") is { } kbps ? kbps * 1000 : current.BitrateBps,
            ScalePercent = Int(e, "scale") ?? current.ScalePercent,
            Monitor = Int(e, "monitor") ?? current.Monitor,
        });
        Log.Information("Viewer changed stream settings: monitor={Monitor} {Fps}fps " +
                        "{Kbps}kbps scale={Scale}%",
                        applied.Monitor, applied.Fps, applied.BitrateBps / 1000, applied.ScalePercent);
        return true;
    }

    private static int? Int(JsonElement e, string key) =>
        e.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Number
            ? v.GetInt32() : null;

    /// <summary>Start a dedicated MTA background thread. Never the thread pool -- see the class
    /// remarks. A throw inside cancels the session rather than dying silently.</summary>
    private static Thread StartThread(string name, Action body, CancellationTokenSource cts)
    {
        var thread = new Thread(() =>
        {
            try { body(); }
            catch (OperationCanceledException) { /* normal teardown */ }
            catch (Exception e)
            {
                Log.Error(e, "{Name} thread failed", name);
                cts.Cancel();
            }
        })
        {
            Name = name,
            IsBackground = true,
        };
        thread.SetApartmentState(ApartmentState.MTA); // D3D11 / Media Foundation
        thread.Start();
        return thread;
    }

    private static void JoinQuietly(Thread thread)
    {
        try { thread.Join(3000); } catch { }
    }

    /// <summary>One-slot handoff of the latest capture geometry from the capture thread to the
    /// input thread.</summary>
    private sealed class GeometrySignal
    {
        private CaptureEncodePipeline.Geometry _value;
        private int _pending;

        public void Set(CaptureEncodePipeline.Geometry value)
        {
            _value = value;
            Volatile.Write(ref _pending, 1);
        }

        public bool TryTake(out CaptureEncodePipeline.Geometry value)
        {
            if (Interlocked.Exchange(ref _pending, 0) == 0)
            {
                value = default;
                return false;
            }
            value = _value;
            return true;
        }
    }

    private static async Task Delay(CancellationToken ct)
    {
        try { await Task.Delay(PollIntervalMs, ct); } catch (OperationCanceledException) { }
    }

    private static void HandleSignal(
        RemotePeer peer, RemoteSignalingClient.SignalMessage sig, CancellationTokenSource cts)
    {
        try
        {
            switch (sig.Kind)
            {
                case "answer":
                    if (sig.Payload.TryGetProperty("sdp", out var sdp) &&
                        peer.ApplyAnswer(sdp.GetString() ?? ""))
                        Log.Information("Applied the console's answer.");
                    break;
                case "ice":
                    string? cand = sig.Payload.TryGetProperty("candidate", out var c) ? c.GetString() : null;
                    string? mid = sig.Payload.TryGetProperty("sdpMid", out var m) ? m.GetString() : null;
                    ushort mline = 0;
                    if (sig.Payload.TryGetProperty("sdpMLineIndex", out var idx) &&
                        idx.ValueKind == JsonValueKind.Number)
                        mline = (ushort)idx.GetInt32();
                    peer.AddRemoteIce(cand, mid, mline);
                    break;
                case "bye":
                    Log.Information("Console sent bye; ending.");
                    cts.Cancel();
                    break;
            }
        }
        catch (Exception e)
        {
            Log.Warning("handling a {Kind} signal failed: {Msg}", sig.Kind, e.Message);
        }
    }

    private static void ConfigureLogger()
    {
        Directory.CreateDirectory(AgentConfig.ProgramDataDir);
        Log.Logger = new LoggerConfiguration()
            .MinimumLevel.Information()
            // SIPSorcery's own diagnostics are noisy at Debug and are the ONLY account of why an
            // ICE negotiation failed, so they are admitted at Warning and above and given their
            // own level floor rather than the blanket minimum.
            .MinimumLevel.Override("SIPSorcery", Serilog.Events.LogEventLevel.Warning)
            .WriteTo.File(
                AgentConfig.RemoteHelperLogPath,
                rollOnFileSizeLimit: true,
                fileSizeLimitBytes: 1_000_000,
                retainedFileCountLimit: 3,
                shared: true,
                outputTemplate:
                    "{Timestamp:yyyy-MM-dd HH:mm:ss} {Level:u3} {Message:lj}{NewLine}{Exception}")
            .CreateLogger();

        // Without this SIPSorcery logs to a null factory and the helper's account of a failed
        // session is three lines long: offer posted, answer applied, "peer connection state:
        // failed" sixteen seconds later. Everything that would say WHY -- which ICE server
        // answered, which allocation was refused, which candidate pair timed out -- is written by
        // SIPSorcery and was being thrown away.
        try { SIPSorcery.LogFactory.Set(new SerilogLoggerFactory(Log.Logger)); }
        catch (Exception e) { Log.Warning("Could not route SIPSorcery logs: {Msg}", e.Message); }
    }

    /// <summary>Read the session file and remove it so its single-use secrets do not linger. A
    /// missing/garbled file returns null with a note.</summary>
    private static RemoteSessionParams? LoadSession(string? path, out string? note)
    {
        note = null;
        if (string.IsNullOrWhiteSpace(path))
        {
            note = "no session file path passed on the command line";
            return null;
        }
        try
        {
            var json = File.ReadAllText(path);
            var session = RemoteSessionParams.FromJson(json);
            try { File.Delete(path); }
            catch (Exception e) { note = $"could not delete session file {path}: {e.Message}"; }
            if (session is null) note = $"session file {path} did not parse";
            return session;
        }
        catch (Exception e)
        {
            note = $"could not read session file {path}: {e.Message}";
            return null;
        }
    }

    /// <summary>A one-line description of where this process is actually running -- session id
    /// and desktop are what tell you the token retargeting worked.</summary>
    private static string Describe()
    {
        var sb = new StringBuilder();
        sb.Append("pid=").Append(Environment.ProcessId);
        sb.Append(" identity=").Append(Environment.UserDomainName).Append('\\').Append(Environment.UserName);
        if (ProcessIdToSessionId((uint)Environment.ProcessId, out uint sid))
            sb.Append(" session=").Append(sid);
        sb.Append(" desktop=").Append(Desktops.CurrentThreadDesktopName() ?? "?");
        return sb.ToString();
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool ProcessIdToSessionId(uint processId, out uint sessionId);
}
