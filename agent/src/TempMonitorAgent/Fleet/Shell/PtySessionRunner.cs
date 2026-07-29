using System.Text;
using Microsoft.Extensions.Logging;

namespace TempMonitorAgent.Fleet.Shell;

/// <summary>
/// Drives one interactive terminal for its whole life: opens the pseudoconsole, pumps the
/// operator's keystrokes into it, pumps its VT output back to the hub, and shuts everything
/// down exactly once when any of the several things that can end a session does.
///
/// THE CADENCE IS THE FEATURE. A fleet command polls every 1-10s, which is fine for "run
/// this script" and hopeless for a terminal -- nobody types into something that echoes a
/// second and a half later. So once the `shell_open` command is claimed, this stops using
/// the command queue entirely and runs its own two loops against the pty endpoints:
///
///   * input  -- poll every PtyInputPollMillis (~150ms) while the operator is typing,
///               backing off to PtyInputPollIdleMillis after PtyIdleAfterMillis of silence.
///               A terminal left open on a second monitor should cost about a heartbeat.
///   * output -- coalesce for PtyOutputFlushMillis (~40ms) and post. `dir` on a big folder
///               is dozens of tiny console writes; one POST each would be pure overhead,
///               and 40ms is well under the threshold where a human sees stutter.
///
/// WAYS A SESSION ENDS, all of which must converge on one clean teardown:
///   1. the operator types `exit` (or the shell crashes)  -> the pty stream EOFs
///   2. the operator closes the tab                        -> hub reports `closing`
///   3. the hub forgets the session (reaped, DB reset)     -> input poll 404s
///   4. the hub decides nobody came back                   -> hub reports `closing` (2)
///      ...or PtyIdleTimeoutSeconds elapses                -> agent-side backstop reap
///   5. the hub becomes unreachable                        -> see OfflineGraceSeconds
///   6. the agent shuts down / self-updates                -> cancellation token
/// Case 5 is the one worth being deliberate about: a terminal the hub cannot reach is not a
/// terminal, and buffering hopefully while an operator keeps typing into the void is worse
/// than saying so. After OfflineGraceSeconds with no successful exchange in EITHER direction
/// the session ends and says why.
/// </summary>
public sealed class PtySessionRunner : IDisposable
{
    private readonly string _sessionId;
    private readonly IPtyChannel _channel;
    private readonly ILogger _log;

    private readonly object _bufferGate = new();
    private StringBuilder _buffer = new();

    private PtySession? _session;
    private int _outSeq;
    private int _inCursor = -1;
    private DateTime _lastInputUtc = DateTime.UtcNow;
    private DateTime _lastHubOkUtc = DateTime.UtcNow;
    private volatile bool _disposed;

    /// <summary>How long the hub may stay unreachable before we call the session dead.
    ///
    /// Measured from the last SUCCESSFUL exchange in either direction, not from the last
    /// output post. An idle prompt posts nothing for minutes at a time, so keying this on
    /// output alone would reap a perfectly healthy terminal the moment its operator stopped
    /// typing -- while the input poll was still succeeding every 150ms.</summary>
    private const int OfflineGraceSeconds = 45;

    /// <summary>Cap on unposted output. Past this the oldest bytes are dropped: the screen
    /// is already going to be wrong if we get here, and unbounded growth on a machine we
    /// are not babysitting is worse than a corrupted redraw.</summary>
    private const int MaxBufferedChars = 512 * 1024;

    public string SessionId => _sessionId;

    public PtySessionRunner(string sessionId, IPtyChannel channel, ILogger log)
    {
        _sessionId = sessionId;
        _channel = channel;
        _log = log;
    }

    /// <summary>Run the session to completion. Returns a short human-readable reason, which
    /// becomes both the hub's close_reason and the shell_open command's result text (the
    /// audit record of what the operator's terminal did).</summary>
    public async Task<string> RunAsync(string shell, short cols, short rows, CancellationToken ct)
    {
        try
        {
            _session = PtySession.Start(_sessionId, shell, cols, rows, Append, _log);
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "Could not open a pseudoconsole for session {Id}", _sessionId);
            var failure = $"could not open a terminal: {e.Message}";
            await _channel.ReportPtyClosedAsync(_sessionId, failure, CancellationToken.None);
            return failure;
        }

        using var stop = CancellationTokenSource.CreateLinkedTokenSource(ct);
        var outputPump = Task.Run(() => PumpOutputAsync(stop.Token), CancellationToken.None);

        var reason = "the shell exited";
        try
        {
            reason = await PumpInputAsync(stop.Token);
        }
        catch (OperationCanceledException)
        {
            reason = "the agent is shutting down";
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "Terminal session {Id} failed", _sessionId);
            reason = $"the terminal failed: {e.Message}";
        }
        finally
        {
            // Close the pty FIRST so the shell stops producing, then let the output pump
            // drain what is already buffered -- the operator should see the shell's parting
            // words ("goodbye", an error) rather than lose the last screenful.
            _session?.Dispose();
            await Task.Delay(AgentConfig.PtyOutputFlushMillis * 2, CancellationToken.None);
            await FlushAsync(CancellationToken.None);
            stop.Cancel();
            try { await outputPump; } catch { /* pump exits on cancellation */ }
        }

        await _channel.ReportPtyClosedAsync(_sessionId, reason, CancellationToken.None);
        _log.LogInformation("Terminal session {Id} ended: {Reason}", _sessionId, reason);
        return reason;
    }

    /// <summary>Called from PtySession's read pump. Must not block or throw.</summary>
    private void Append(string text)
    {
        if (_disposed || text.Length == 0) return;
        lock (_bufferGate)
        {
            _buffer.Append(text);
            if (_buffer.Length > MaxBufferedChars)
                _buffer.Remove(0, _buffer.Length - MaxBufferedChars);
        }
    }

    /// <summary>Keystrokes in, plus every "is this session still wanted?" check. This loop's
    /// exit is the session's exit.</summary>
    private async Task<string> PumpInputAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            if (_session is null || _session.Exited)
                return "the shell exited";

            var batch = await _channel.PullPtyInputAsync(_sessionId, _inCursor, ct);
            if (batch.Gone) return "the hub no longer has this session";
            if (batch.Ok)
            {
                _lastHubOkUtc = DateTime.UtcNow;
                foreach (var item in batch.Items)
                {
                    if (item.Kind == "resize") _session.Resize(item.Cols, item.Rows);
                    else _session.Write(item.Data);   // verbatim: see PtySession.Write
                }
                if (batch.Items.Count > 0)
                {
                    _lastInputUtc = DateTime.UtcNow;
                    _inCursor = batch.NextSeq - 1;
                }
                if (batch.Closing) return "closed by the operator";
            }

            // A hub we cannot reach means nobody is watching this terminal, and buffering
            // hopefully while an operator types into the void is worse than saying so.
            if ((DateTime.UtcNow - _lastHubOkUtc).TotalSeconds > OfflineGraceSeconds)
                return "lost contact with the hub";

            // The agent's backstop reap (see AgentConfig.PtyIdleTimeoutSeconds -- the hub
            // normally decides this and tells us via `closing`). Measured on the SESSION's
            // own traffic, not on keystrokes alone: a build that prints for an hour with
            // nobody typing is not idle.
            var quietFor = DateTime.UtcNow - _session.LastActivityUtc;
            if (quietFor.TotalSeconds > AgentConfig.PtyIdleTimeoutSeconds)
                return "idle timeout";

            var typingRecently = (DateTime.UtcNow - _lastInputUtc).TotalMilliseconds < AgentConfig.PtyIdleAfterMillis;
            var delay = typingRecently ? AgentConfig.PtyInputPollMillis : AgentConfig.PtyInputPollIdleMillis;
            try { await Task.Delay(delay, ct); }
            catch (OperationCanceledException) { break; }
        }
        return "the agent is shutting down";
    }

    private async Task PumpOutputAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try { await Task.Delay(AgentConfig.PtyOutputFlushMillis, ct); }
            catch (OperationCanceledException) { return; }
            await FlushAsync(ct);
        }
    }

    private async Task FlushAsync(CancellationToken ct)
    {
        while (true)
        {
            string payload;
            lock (_bufferGate)
            {
                if (_buffer.Length == 0) return;
                var take = Math.Min(_buffer.Length, AgentConfig.PtyMaxChunkChars);
                payload = _buffer.ToString(0, take);
                _buffer.Remove(0, take);
            }

            var posted = await _channel.PostPtyOutputAsync(_sessionId, _outSeq, payload, ct);
            if (posted)
            {
                // Only advance on success. Retrying the SAME seq is what makes a post that
                // actually landed before timing out a free no-op on the hub; a fresh seq
                // would splice duplicate bytes into the middle of an escape sequence.
                _outSeq++;
                _lastHubOkUtc = DateTime.UtcNow;
                continue;
            }

            // Put it back at the front and let the next tick retry. PumpInputAsync is
            // watching _lastHubOkUtc and will end the session if this never recovers.
            lock (_bufferGate)
            {
                var pending = _buffer.ToString();
                _buffer = new StringBuilder(payload.Length + pending.Length);
                _buffer.Append(payload).Append(pending);
                if (_buffer.Length > MaxBufferedChars)
                    _buffer.Remove(0, _buffer.Length - MaxBufferedChars);
            }
            return;
        }
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        _session?.Dispose();
    }
}
