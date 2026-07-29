using System.Collections.Concurrent;
using Microsoft.Extensions.Logging;

namespace TempMonitorAgent.Fleet.Shell;

/// <summary>
/// Registry of the interactive terminals currently open on this machine. Deliberately much
/// thinner than <see cref="ShellSessionManager"/>: a pty session is owned end-to-end by its
/// <see cref="PtySessionRunner"/> and identified by a hub-issued session id, so there is no
/// per-operator keying to do here and nothing to look up between commands.
///
/// It exists for the two things a registry is actually needed for:
///
///  * A CAP. Each session is a real SYSTEM console; an operator reopening a tab in a loop,
///    or a hub bug re-issuing shell_open, must not be able to fill the machine with them.
///  * SHUTDOWN. Disposed by the DI container at host shutdown, which tears every terminal
///    down. (ConPtyProcess also enlists each child in the kill-on-close job, so even the
///    hard Environment.Exit a self-update uses can't orphan one -- this is the graceful
///    half of that pair.)
/// </summary>
public sealed class PtySessionManager : IDisposable
{
    private readonly ILogger<PtySessionManager> _log;
    private readonly ConcurrentDictionary<string, PtySessionRunner> _runners = new();

    public PtySessionManager(ILogger<PtySessionManager> log) => _log = log;

    /// <summary>True while any terminal is open. Terminals do not use the command channel
    /// once opened -- they run their own loops against /api/agent/pty/* -- so nothing needs
    /// to change cadence for them; this is here for diagnostics and for callers that only
    /// want to know whether the machine has a live console.</summary>
    public bool AnyOpen => !_runners.IsEmpty;

    public int OpenCount => _runners.Count;

    /// <summary>Register a runner for the duration of its session. Returns false when the
    /// machine is already at <see cref="AgentConfig.MaxPtySessions"/>.</summary>
    public bool TryAdd(PtySessionRunner runner)
    {
        if (_runners.Count >= AgentConfig.MaxPtySessions)
        {
            _log.LogWarning("Refusing terminal {Id}: already at {Max} open sessions",
                runner.SessionId, AgentConfig.MaxPtySessions);
            return false;
        }
        return _runners.TryAdd(runner.SessionId, runner);
    }

    public void Remove(string sessionId) => _runners.TryRemove(sessionId, out _);

    public void Dispose()
    {
        foreach (var (id, runner) in _runners.ToArray())
        {
            _runners.TryRemove(id, out _);
            try { runner.Dispose(); } catch { /* shutting down anyway */ }
        }
    }
}
