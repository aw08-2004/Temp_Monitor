using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Keeps a remote session alive across the event that would otherwise end it: <b>somebody
/// signing in</b>.
///
/// The problem this solves is specific and, without it, defeats the main new use case. An
/// operator opens a headless machine sitting at the logon screen, types a password, and signs
/// in. Signing in creates a NEW Windows session, and the old session -- the one the capture
/// helper was injected into -- is torn down, taking the helper with it. So the moment the remote
/// login succeeds, the remote view goes dead. The operator did everything right and got a black
/// screen for it.
///
/// Rather than hook service session-change notifications, this watches something simpler and
/// strictly more general: whether the helper process is still alive. That covers the logon
/// transition, a helper crash, and a helper killed by anything else, with one mechanism and no
/// dependence on the host's service lifetime plumbing.
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

    /// <summary>Ceiling on how long a record may keep resurrecting a helper. Deliberately
    /// shorter than the hub's session TTL: the hub's own sweep is the outer backstop.</summary>
    private static readonly TimeSpan RecordLifetime = TimeSpan.FromMinutes(30);

    private readonly ILogger<RemoteSessionSupervisor> _log;

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
                TryDelete(path);
                continue;
            }

            if (IsAlive(record.Pid)) continue;

            if (record.Relaunches >= MaxRelaunches)
            {
                _log.LogWarning(
                    "Remote session {SessionId}: helper died {Count} times; giving up. The hub's " +
                    "TTL sweep will end the session.", record.SessionId, record.Relaunches);
                TryDelete(path);
                continue;
            }

            Relaunch(path, record);
        }
    }

    /// <summary>Re-inject the helper for a session whose helper vanished. Deliberately does NOT
    /// pin the previous Windows session id: the usual reason we are here is that that session no
    /// longer exists, so auto-selection is the right behaviour -- it will now find the session
    /// the operator just signed into.</summary>
    private void Relaunch(string recordPath, LiveSessionRecord record)
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
        record.Relaunches++;
        Write(recordPath, record);
        _log.LogInformation(
            "Remote session {SessionId}: helper relaunched (pid {Pid}, Windows session {Session}, " +
            "attempt {Attempt}) after the previous one ended -- most likely somebody signed in.",
            record.SessionId, result.Pid, result.SessionId, record.Relaunches);
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

    /// <summary>Start tracking a session. Called by the executor once the first launch
    /// succeeds.</summary>
    internal static void Track(string sessionId, string paramsJson, uint pid)
    {
        try
        {
            Write(RecordPath(sessionId), new LiveSessionRecord
            {
                SessionId = sessionId,
                Params = paramsJson,
                Pid = (int)pid,
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
        [JsonPropertyName("started_at")] public DateTimeOffset StartedAt { get; set; }
        [JsonPropertyName("relaunches")] public int Relaunches { get; set; }
    }
}
