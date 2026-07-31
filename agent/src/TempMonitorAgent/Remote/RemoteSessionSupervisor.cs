using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Keeps a remote session pointed at the desktop the operator is actually trying to reach,
/// across the events that would otherwise strand it: <b>somebody signing in, signing out, or
/// switching user</b>.
///
/// The problem this solves is specific and, without it, defeats the main new use case. An
/// operator opens a headless machine sitting at the logon screen, types a password, and signs
/// in. Signing in creates a NEW Windows session, and the old session -- the one the capture
/// helper was injected into -- is torn down, taking the helper with it. So the moment the remote
/// login succeeds, the remote view goes dead. The operator did everything right and got a black
/// screen for it.
///
/// Two mechanisms, because the transitions fail in two different ways:
///   * <b>The helper died</b> -- the session it lived in was destroyed (a sign-in from the logon
///     screen, a sign-out), or it crashed, or something killed it. Watching liveness rather than
///     hooking service session-change notifications covers all of those with one mechanism and
///     no dependence on the host's service lifetime plumbing.
///   * <b>The helper is alive in a session nobody is looking at</b> -- switch user, or an RDP
///     session taking the console's place. Nothing dies here: the old session lingers in
///     Disconnected with its desktop intact, so the helper keeps capturing and streaming a
///     desktop that is no longer on the screen. Only an <b>auto</b> session follows the move;
///     a session the operator pinned by hand is left exactly where they put it.
///
/// The contract is a <c>.live.json</c> record written when a session starts:
///   * the helper deletes it on a clean exit (operator stopped, hub ended, peer dropped), so a
///     deliberately-finished session is never resurrected;
///   * if the record outlives the process, the session ended involuntarily and is relaunched.
/// Relaunches are capped and expire, so a helper that crashes on startup cannot become a restart
/// loop.
///
/// This service also hosts the secure-attention pipe (see <see cref="SecureAttentionRelay"/>),
/// which must run in the real service for SendSAS to be honoured.
/// </summary>
public sealed class RemoteSessionSupervisor : BackgroundService
{
    /// <summary>How often to check whether a tracked helper is still running. Fast enough that
    /// the gap after a logon is barely visible, cheap enough to ignore.</summary>
    private static readonly TimeSpan PollInterval = TimeSpan.FromSeconds(3);

    /// <summary>A logon transition needs one relaunch. More than a handful means the helper is
    /// failing on startup, and relaunching harder will not fix it.</summary>
    private const int MaxRelaunches = 5;

    /// <summary>Deliberate moves (switch user, RDP taking the console) get their own budget: they
    /// are healthy, unlike the crash loop <see cref="MaxRelaunches"/> guards against, so an
    /// operator flipping between accounts must not exhaust it. Still capped, because a machine
    /// whose session selection oscillates should settle rather than thrash a live session.</summary>
    private const int MaxSessionMoves = 10;

    /// <summary>Ceiling on how long a record may keep resurrecting a helper. Deliberately
    /// shorter than the hub's session TTL: the hub's own sweep is the outer backstop.</summary>
    private static readonly TimeSpan RecordLifetime = TimeSpan.FromMinutes(30);

    private readonly ILogger<RemoteSessionSupervisor> _log;

    /// <summary>Session id -> the Windows session we have seen auto-selection point at but have
    /// not acted on yet. Windows spends a few seconds between sessions during a switch and the
    /// selection wobbles while it does, so a move must be seen on two consecutive passes before
    /// we tear a live helper down for it.</summary>
    private readonly Dictionary<string, uint> _pendingMove = new();

    public RemoteSessionSupervisor(ILogger<RemoteSessionSupervisor> log) => _log = log;

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        // The pipe server runs for the life of the service alongside the supervision loop.
        var sasServer = SecureAttentionRelay.RunServerAsync(_log, stoppingToken);

        while (!stoppingToken.IsCancellationRequested)
        {
            try { SuperviseOnce(); }
            catch (Exception e) { _log.LogWarning(e, "Remote session supervision pass failed"); }

            try { await Task.Delay(PollInterval, stoppingToken); }
            catch (OperationCanceledException) { break; }
        }

        try { await sasServer; } catch { /* cancelled with the host */ }
    }

    private void SuperviseOnce()
    {
        if (!Directory.Exists(AgentConfig.RemoteStateDir)) return;

        foreach (var path in Directory.EnumerateFiles(AgentConfig.RemoteStateDir, "*.live.json"))
        {
            LiveSessionRecord? record = Read(path);
            if (record is null) { TryDelete(path); continue; }

            if (DateTimeOffset.UtcNow - record.StartedAt > RecordLifetime)
            {
                _log.LogInformation(
                    "Remote session {SessionId}: supervision window elapsed; stopping tracking.",
                    record.SessionId);
                _pendingMove.Remove(record.SessionId);
                TryDelete(path);
                continue;
            }

            if (IsAlive(record.Pid)) { FollowInteractiveSession(path, record); continue; }

            if (record.Relaunches >= MaxRelaunches)
            {
                _log.LogWarning(
                    "Remote session {SessionId}: helper died {Count} times; giving up. The hub's " +
                    "TTL sweep will end the session.", record.SessionId, record.Relaunches);
                _pendingMove.Remove(record.SessionId);
                TryDelete(path);
                continue;
            }

            record.Relaunches++;
            Relaunch(path, record, "the previous one ended -- most likely somebody signed in");
        }
    }

    /// <summary>
    /// Move a live helper when "auto" now resolves to a different Windows session.
    ///
    /// This is the switch-user / sign-out case. Nothing died: the old session survives in
    /// Disconnected with its window station and desktop intact, so the helper keeps capturing
    /// and streaming it quite happily -- a live, moving picture of a desktop that is no longer
    /// on the monitor and no longer where the operator's keystrokes land. Liveness supervision
    /// cannot see that; only re-asking the question can.
    ///
    /// Pinned sessions are deliberately excluded. "Session 3" means session 3, including when it
    /// stops being the one at the console -- shadowing a specific user's session while somebody
    /// else uses the console is a legitimate thing to be doing.
    /// </summary>
    private void FollowInteractiveSession(string recordPath, LiveSessionRecord record)
    {
        uint target = record.Auto ? SessionInjector.AutoSelectSession()
                                  : SessionInjector.NoActiveSession;
        uint? pending = _pendingMove.TryGetValue(record.SessionId, out uint seen) ? seen : null;

        var decision = DecideMove(record.Auto, record.WindowsSession, target, pending,
                                  record.SessionMoves);
        if (decision == MoveDecision.Wait)
        {
            _pendingMove[record.SessionId] = target;
            return;
        }

        _pendingMove.Remove(record.SessionId);
        if (decision == MoveDecision.Stay) return;
        if (decision == MoveDecision.Capped)
        {
            _log.LogWarning(
                "Remote session {SessionId}: the interactive session has moved {Count} times; " +
                "leaving the helper in session {Session} rather than thrashing it.",
                record.SessionId, record.SessionMoves, record.WindowsSession);
            return;
        }

        _log.LogInformation(
            "Remote session {SessionId}: the interactive session moved {From} -> {To} " +
            "(sign-out, switch user, or a new console owner); moving the helper.",
            record.SessionId, record.WindowsSession, target);

        record.SessionMoves++;
        // Stop the old helper first. It is killed rather than asked to stop, because a clean
        // exit is what tells us a session is FINISHED -- the helper deletes this record on its
        // way out, and we would be relaunching into a record that no longer exists.
        Kill(record.Pid);
        Relaunch(recordPath, record, $"the interactive session moved to {target}");
    }

    /// <summary>What to do about the interactive session having (apparently) moved.</summary>
    internal enum MoveDecision
    {
        /// <summary>Leave the helper where it is, and forget any half-seen move.</summary>
        Stay,
        /// <summary>Remember this target and decide on the next pass.</summary>
        Wait,
        /// <summary>A move is due but the budget is spent.</summary>
        Capped,
        /// <summary>Stop the helper and re-inject it into the session auto now picks.</summary>
        Move,
    }

    /// <summary>
    /// The decision half of <see cref="FollowInteractiveSession"/>, kept pure so the rules can be
    /// tested without a Windows session, a helper process, or a state directory.
    /// </summary>
    /// <param name="auto">The operator left the session choice to the agent.</param>
    /// <param name="current">The Windows session the helper is in.</param>
    /// <param name="target">What auto-selection picks now.</param>
    /// <param name="pending">The target seen on the previous pass, if any.</param>
    /// <param name="moves">Moves already made for this session.</param>
    internal static MoveDecision DecideMove(
        bool auto, uint current, uint target, uint? pending, int moves)
    {
        // A pinned session means that session, including once it stops being the one at the
        // console. Following the console away from it would override the operator.
        if (!auto) return MoveDecision.Stay;
        // NoActiveSession is Windows mid-transition with nothing interactive at all -- a normal
        // beat or two during a sign-out, and never a reason to move anywhere.
        if (target == SessionInjector.NoActiveSession || target == current) return MoveDecision.Stay;
        // Seen once is not enough: the selection wobbles while Windows is between sessions, and
        // acting on the first sighting relaunches into a session about to be replaced.
        if (pending != target) return MoveDecision.Wait;
        return moves >= MaxSessionMoves ? MoveDecision.Capped : MoveDecision.Move;
    }

    /// <summary>Stop a helper we are about to replace. Best effort throughout: a helper that has
    /// already exited, or that we cannot open, is exactly the state we were trying to reach.</summary>
    private void Kill(int pid)
    {
        if (pid <= 0) return;
        try
        {
            using var process = System.Diagnostics.Process.GetProcessById(pid);
            process.Kill();
            process.WaitForExit(3000);
        }
        catch (Exception e)
        {
            _log.LogInformation("Could not stop helper pid {Pid}: {Msg}", pid, e.Message);
        }
    }

    /// <summary>Re-inject the helper for a session that lost its previous one. Deliberately does
    /// NOT pin the previous Windows session id: whether the helper died with its session or was
    /// stopped because the interactive session moved, auto-selection is the right answer -- it
    /// finds the session the operator just signed into, or switched to.</summary>
    private void Relaunch(string recordPath, LiveSessionRecord record, string why)
    {
        var exePath = Environment.ProcessPath;
        if (string.IsNullOrEmpty(exePath))
        {
            TryDelete(recordPath);
            return;
        }

        string sessionFile = StartRemoteSessionExecutor.SessionFilePath(record.SessionId);
        try
        {
            Directory.CreateDirectory(AgentConfig.RemoteStateDir);
            File.WriteAllText(sessionFile, record.Params);
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "Remote session {SessionId}: could not re-stage the session file",
                            record.SessionId);
            TryDelete(recordPath);
            return;
        }

        var result = SessionInjector.Launch(
            exePath, $"{AgentConfig.RemoteHelperArg} \"{sessionFile}\"");
        if (!result.Ok)
        {
            // Very common and expected for a beat or two mid-logon: no session is interactive
            // while Windows is between them. Keep the record and try again next tick.
            _log.LogInformation(
                "Remote session {SessionId}: relaunch not possible yet ({Error}); will retry.",
                record.SessionId, result.Error);
            TryDelete(sessionFile);
            return;
        }

        record.Pid = (int)result.Pid;
        record.WindowsSession = result.SessionId;
        Write(recordPath, record);
        _log.LogInformation(
            "Remote session {SessionId}: helper relaunched (pid {Pid}, Windows session {Session}, " +
            "relaunch {Relaunches}, move {Moves}) because {Why}.",
            record.SessionId, result.Pid, result.SessionId, record.Relaunches,
            record.SessionMoves, why);
    }

    private static bool IsAlive(int pid)
    {
        if (pid <= 0) return false;
        try
        {
            using var process = System.Diagnostics.Process.GetProcessById(pid);
            return !process.HasExited;
        }
        catch { return false; }
    }

    // ------------------------------------------------------------------ record I/O
    /// <summary>Path of the supervision record for a session.</summary>
    internal static string RecordPath(string sessionId) =>
        Path.Combine(AgentConfig.RemoteStateDir, sessionId + ".live.json");

    /// <summary>Start tracking a session. Called by the executor once the first launch succeeds.
    /// <paramref name="windowsSession"/> is where the helper actually landed and
    /// <paramref name="auto"/> whether the operator left the session choice to the agent -- only
    /// an auto session is allowed to follow the console to a different one later.</summary>
    internal static void Track(string sessionId, string paramsJson, uint pid,
                               uint windowsSession, bool auto)
    {
        try
        {
            Write(RecordPath(sessionId), new LiveSessionRecord
            {
                SessionId = sessionId,
                Params = paramsJson,
                Pid = (int)pid,
                WindowsSession = windowsSession,
                Auto = auto,
                StartedAt = DateTimeOffset.UtcNow,
            });
        }
        catch { /* supervision is a nicety; never fail a launch over it */ }
    }

    /// <summary>Stop tracking a session. Called by the helper on a clean exit, which is what
    /// distinguishes "finished" from "killed".</summary>
    internal static void Untrack(string sessionId)
    {
        try { TryDelete(RecordPath(sessionId)); } catch { }
    }

    private static LiveSessionRecord? Read(string path)
    {
        try { return JsonSerializer.Deserialize<LiveSessionRecord>(File.ReadAllText(path)); }
        catch { return null; }
    }

    private static void Write(string path, LiveSessionRecord record) =>
        File.WriteAllText(path, JsonSerializer.Serialize(record));

    private static void TryDelete(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); } catch { /* best effort */ }
    }

    internal sealed class LiveSessionRecord
    {
        [JsonPropertyName("session_id")] public string SessionId { get; set; } = "";
        /// <summary>The original session parameters, verbatim, so a relaunch does not have to
        /// re-derive them. Note the TURN credentials inside have their own expiry -- a relaunch
        /// long after the start may fall back to host/STUN candidates.</summary>
        [JsonPropertyName("params")] public string Params { get; set; } = "";
        [JsonPropertyName("pid")] public int Pid { get; set; }
        /// <summary>The Windows session the helper is in, so a move can be detected by comparing
        /// it against what auto-selection says now.</summary>
        [JsonPropertyName("windows_session")] public uint WindowsSession { get; set; }
        /// <summary>The operator left the session choice to the agent. Only these follow the
        /// interactive session when it moves; a pinned session stays pinned.</summary>
        [JsonPropertyName("auto")] public bool Auto { get; set; }
        [JsonPropertyName("started_at")] public DateTimeOffset StartedAt { get; set; }
        [JsonPropertyName("relaunches")] public int Relaunches { get; set; }
        /// <summary>Deliberate moves to a new interactive session, counted separately from
        /// <see cref="Relaunches"/> so a healthy switch-user does not spend the crash budget.</summary>
        [JsonPropertyName("session_moves")] public int SessionMoves { get; set; }
    }
}
