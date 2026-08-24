using System.Collections.Concurrent;
using Microsoft.Extensions.Logging;

namespace TempMonitorAgent.Fleet.Shell;

/// <summary>
/// Owns the persistent shells, one per (operator, shell type). Registered as a singleton so
/// the shells outlive individual commands; disposed by the DI container at host shutdown,
/// which tears every shell down (belt-and-braces with the kill-on-close job in ProcessTree).
///
/// Keyed by the operator's email as delivered in the claimed command's issued_by -- which the
/// hub sets from the trusted session, never a client body. That is the whole isolation story:
/// an operator can only ever address their own shell, because they can only ever issue
/// commands under their own identity.
/// </summary>
public sealed class ShellSessionManager : IAsyncDisposable
{
    private readonly ILogger<ShellSessionManager> _log;
    private readonly ConcurrentDictionary<string, ShellSession> _sessions = new();
    private readonly SemaphoreSlim _createLock = new(1, 1);
    private readonly Timer _reaper;
    private volatile bool _disposed;

    public ShellSessionManager(ILogger<ShellSessionManager> log)
    {
        _log = log;
        _reaper = new Timer(_ => ReapIdle(), null,
            TimeSpan.FromMinutes(1), TimeSpan.FromMinutes(1));
    }

    private static string Key(string email, string shell)
    {
        var e = (email ?? "").Trim().ToLowerInvariant();
        var s = (shell ?? "powershell").Trim().ToLowerInvariant();
        if (s is "batch" or "bat") s = "cmd";
        if (s != "cmd") s = "powershell";
        return e + "\0" + s;
    }

    /// <summary>True if any operator's shell is mid-submission -- the Worker polls faster then
    /// so typed input isn't stuck behind the slow command cadence.</summary>
    public bool AnyActiveSubmission => _sessions.Values.Any(s => s.HasActiveSubmission);

    /// <summary>Get the operator's shell, creating it (or replacing a dead one) on demand.</summary>
    public async Task<ShellSession> GetOrCreateAsync(string email, string shell, CancellationToken ct)
    {
        var key = Key(email, shell);
        if (_sessions.TryGetValue(key, out var existing) && existing.IsAlive) return existing;

        await _createLock.WaitAsync(ct);
        try
        {
            if (_sessions.TryGetValue(key, out existing))
            {
                if (existing.IsAlive) return existing;
                _sessions.TryRemove(key, out _);
                await existing.DisposeAsync();   // reap the dead one (operator typed `exit`, etc.)
            }

            EvictIfOverCap();
            var shellType = key.EndsWith("\0cmd") ? "cmd" : "powershell";
            var session = await ShellSession.StartAsync(shellType, _log, ct);
            _sessions[key] = session;
            _log.LogInformation("Opened {Shell} shell session for {Op}", shellType, OperatorTag.For(EmailOf(key)));
            return session;
        }
        finally { _createLock.Release(); }
    }

    /// <summary>Find an existing session without creating one (for shell_input / shell_signal,
    /// which are meaningless if there's no live session).</summary>
    public ShellSession? TryGet(string email, string shell)
        => _sessions.TryGetValue(Key(email, shell), out var s) && s.IsAlive ? s : null;

    /// <summary>Dispose and forget the operator's shell so the next run starts fresh.</summary>
    public async Task ResetAsync(string email, string shell)
    {
        if (_sessions.TryRemove(Key(email, shell), out var s))
        {
            await s.DisposeAsync();
            _log.LogInformation("Reset {Shell} shell session for {Op}", s.Shell, OperatorTag.For(email));
        }
    }

    private void EvictIfOverCap()
    {
        if (_sessions.Count < AgentConfig.MaxShellSessions) return;
        // Prefer an idle victim; the oldest by last activity. If everything is busy, let the
        // new one push us one over -- refusing a command outright is worse UX than a transient
        // extra shell, and the reaper will trim back down.
        var victim = _sessions
            .Where(kv => !kv.Value.HasActiveSubmission)
            .OrderBy(kv => kv.Value.LastActivityUtc)
            .Select(kv => (KeyValuePair<string, ShellSession>?)kv)
            .FirstOrDefault();
        if (victim is { } v && _sessions.TryRemove(v.Key, out var s))
        {
            _log.LogInformation("Evicting idle shell for {Op} (session cap reached)", OperatorTag.For(EmailOf(v.Key)));
            TearDownInBackground(s, v.Key);
        }
    }

    /// <summary>
    /// Tear a shell down from a caller that cannot await: the reaper's timer callback and the
    /// eviction path above. Fire-and-forget by necessity, but observed -- discarding the
    /// ValueTask swallows whatever DisposeAsync threw, and here that would be a shell process
    /// we believe we killed and did not, with nothing in the log to say so. Task.Run also
    /// keeps the kill-the-process-tree part off the timer thread, which DisposeAsync runs
    /// synchronously before it reaches its first await.
    /// </summary>
    private void TearDownInBackground(ShellSession session, string key)
    {
        _ = Task.Run(async () =>
        {
            try
            {
                await session.DisposeAsync();
            }
            catch (Exception e)
            {
                _log.LogWarning(e, "Tearing down the shell for {Op} failed", OperatorTag.For(EmailOf(key)));
            }
        });
    }

    private void ReapIdle()
    {
        if (_disposed) return;
        var cutoff = DateTime.UtcNow - TimeSpan.FromSeconds(AgentConfig.ShellIdleTimeoutSeconds);
        foreach (var (key, session) in _sessions.ToArray())
        {
            if (session.HasActiveSubmission) continue;
            if (session.LastActivityUtc > cutoff && session.IsAlive) continue;
            if (_sessions.TryRemove(key, out var s))
            {
                _log.LogInformation("Reaping idle/dead shell for {Op}", OperatorTag.For(EmailOf(key)));
                TearDownInBackground(s, key);
            }
        }
    }

    private static string EmailOf(string key)
    {
        var nul = key.IndexOf('\0');
        return nul < 0 ? key : key[..nul];
    }

    public async ValueTask DisposeAsync()
    {
        _disposed = true;
        await _reaper.DisposeAsync();
        foreach (var (key, session) in _sessions.ToArray())
        {
            _sessions.TryRemove(key, out _);
            await session.DisposeAsync();
        }
        _createLock.Dispose();
    }
}
