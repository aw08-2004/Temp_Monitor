using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using Serilog;
using SIPSorcery.Net;
using TempMonitorAgent.State;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Entry point for the session-injected helper (roadmap #2). The service launches THIS SAME
/// BINARY with <see cref="AgentConfig.RemoteHelperArg"/> as SYSTEM inside the interactive
/// console session (see <see cref="SessionInjector"/>); <see cref="Program"/> branches here
/// before the Windows Service host is ever built.
///
/// Phase 1 is deliberately just a skeleton: it proves the injection landed by recording where
/// it is running -- session id, identity, and window station\desktop, which is what tells you
/// the token retargeting worked and the process is somewhere a desktop actually exists. The
/// DXGI capture, H.264 encode, and WebRTC pipeline hang off this in later phases.
///
/// It logs to its own file (<see cref="AgentConfig.RemoteHelperLogPath"/>), not companion.log,
/// because it runs in a different session and its diagnostics should be legible on their own.
/// </summary>
public static class RemoteHelper
{
    // Streaming defaults. Deliberately modest for the software encoder baseline; tuned (and
    // raised on a hardware encoder) during the on-hardware debugging pass.
    private const int RemoteFps = 15;
    private const int RemoteBitrateBps = 4_000_000;
    // How often the helper polls the hub for the console's answer + trickled ICE + status.
    private const int PollIntervalMs = 800;

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

    /// <summary>Runs the capture self-test:
    /// <c>--remote-capture-test [outputPath] [seconds] [monitor] [fps] [bitrateKbps]</c>.</summary>
    public static int RunCaptureSelfTest(string[] rest)
    {
        string outPath = rest.Length > 0 && rest[0].Length > 0
            ? rest[0]
            : Path.Combine(AgentConfig.ProgramDataDir, "remote-capture-test.h264");
        int seconds = rest.Length > 1 && int.TryParse(rest[1], out var s) ? s : 5;
        int monitor = rest.Length > 2 && int.TryParse(rest[2], out var m) ? m : 0;
        int fps = rest.Length > 3 && int.TryParse(rest[3], out var f) ? f : 15;
        int kbps = rest.Length > 4 && int.TryParse(rest[4], out var k) ? k : 4000;

        void Say(string msg) => Console.WriteLine("[remote-capture-test] " + msg);
        Say($"output={outPath} seconds={seconds} monitor={monitor} fps={fps} bitrate={kbps}kbps");
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(outPath))!);
            int frames = CaptureEncodePipeline.RunToFile(outPath, seconds, monitor, fps, kbps * 1000, Say);
            Say(frames > 0 ? $"done, {frames} frames. Play with: ffplay \"{outPath}\"" : "no frames produced");
            return frames > 0 ? 0 : 3;
        }
        catch (Exception e)
        {
            Say("FAILED: " + e);
            return 1;
        }
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
                "Session {SessionId}: monitor={Monitor} consent={Consent} issued_by={IssuedBy} ice_servers={Ice}",
                session.SessionId, session.Monitor, session.ConsentMode, session.IssuedBy,
                session.IceServers.Count);

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
            Log.CloseAndFlush();
        }
    }

    /// <summary>Run one remote session: create the WebRTC offer, stream captured H.264 to the
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
            bool approved = await Task.Run(
                () => ConsentBanner.RequestConsent(AgentConfig.MachineName, session.IssuedBy));
            if (!approved)
            {
                Log.Information("Consent denied or timed out; ending session.");
                try { await signaling.ReportEndedAsync("consent denied", CancellationToken.None); }
                catch { /* the TTL sweep is the backstop */ }
                return 0;
            }
            Log.Information("Consent granted.");
        }

        using var peer = new RemotePeer(session.IceServers, msg => Log.Information("{Msg}", msg));

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

        // Input control: the browser drives the desktop over a "control" DataChannel, which we
        // turn into real SendInput on the captured monitor (roadmap #2, phase 5).
        var input = new InputInjector(session.Monitor);
        peer.OnControlMessage += msg =>
        {
            try { input.Apply(msg); }
            catch (Exception e) { Log.Warning("input event failed: {Msg}", e.Message); }
        };
        await peer.EnableControlChannelAsync();

        // Offer first, then start streaming, then poll for the answer + remote ICE.
        var offer = await peer.CreateOfferAsync();
        await signaling.PostSignalAsync("offer", offer, cts.Token);
        Log.Information("Posted offer; starting capture and awaiting the console's answer.");

        var captureTask = Task.Run(() =>
        {
            try
            {
                CaptureEncodePipeline.RunToPeer(
                    peer, session.Monitor, RemoteFps, RemoteBitrateBps, cts.Token,
                    m => Log.Information("{Msg}", m));
            }
            catch (Exception e)
            {
                Log.Error(e, "capture/encode loop failed");
                cts.Cancel();
            }
        }, cts.Token);

        int afterSeq = 0;
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
        // Authoritatively end the hub session so it doesn't linger until the TTL sweep; the
        // browser sees status "ended" on its next poll and tears down.
        try { await signaling.ReportEndedAsync("agent teardown", CancellationToken.None); } catch { }
        peer.Close();
        try { await captureTask; } catch { /* already logged / cancelled */ }
        Log.Information("Remote session {SessionId} ended.", session.SessionId);
        return 0;
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
            .WriteTo.File(
                AgentConfig.RemoteHelperLogPath,
                rollOnFileSizeLimit: true,
                fileSizeLimitBytes: 1_000_000,
                retainedFileCountLimit: 3,
                shared: true,
                outputTemplate:
                    "{Timestamp:yyyy-MM-dd HH:mm:ss} {Level:u3} {Message:lj}{NewLine}{Exception}")
            .CreateLogger();
    }

    /// <summary>Read the session file and remove it so its (future) single-use secrets do not
    /// linger. A missing/garbled file returns null with a note.</summary>
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

    /// <summary>A one-line description of where this process is actually running -- the whole
    /// point of the phase-1 skeleton.</summary>
    private static string Describe()
    {
        var sb = new StringBuilder();
        sb.Append("pid=").Append(Environment.ProcessId);
        sb.Append(" identity=").Append(Environment.UserDomainName).Append('\\').Append(Environment.UserName);
        if (ProcessIdToSessionId((uint)Environment.ProcessId, out uint sid))
            sb.Append(" session=").Append(sid);
        sb.Append(" desktop=").Append(CurrentDesktopName() ?? "?");
        return sb.ToString();
    }

    private static string? CurrentDesktopName()
    {
        IntPtr desktop = GetThreadDesktop(GetCurrentThreadId());
        if (desktop == IntPtr.Zero) return null;
        var buffer = new byte[256];
        if (GetUserObjectInformationW(desktop, UoiName, buffer, buffer.Length, out int needed))
            return Encoding.Unicode.GetString(buffer, 0, Math.Max(0, needed - 2)); // strip trailing NUL
        return null;
    }

    private const int UoiName = 2;

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool ProcessIdToSessionId(uint processId, out uint sessionId);

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr GetThreadDesktop(uint threadId);

    [DllImport("user32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool GetUserObjectInformationW(
        IntPtr obj, int index, byte[] info, int length, out int lengthNeeded);
}
