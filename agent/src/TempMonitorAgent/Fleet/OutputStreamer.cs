using System.Text;
using Microsoft.Extensions.Logging;

namespace TempMonitorAgent.Fleet;

/// <summary>
/// Streams one running command's output to the hub so the console terminal shows progress
/// instead of a spinner. One instance per executing command.
///
/// Design notes, in rough order of how easy they are to break:
///
///  * ORDERING IS STRUCTURAL, not hoped for. A single flush loop owns _seq and never has
///    two POSTs in flight, so chunks can't arrive out of order or race each other for a
///    number. Don't "optimise" this into parallel posts.
///  * A retry reuses the SAME seq. The hub keys on (command_id, seq) with INSERT OR
///    IGNORE, so a POST that actually landed before timing out is a free no-op. Allocating
///    a fresh seq on retry would duplicate the chunk instead.
///  * Add() is called from ProcessRunner's stdout/stderr threads and must never block them
///    or throw — the command's real work is more important than its narration.
///  * Losing output is acceptable; stalling is not. After StreamPostRetries the chunk is
///    dropped and seq advances, because a permanently unreachable hub must not wedge the
///    stream forever. The full text still reaches command_results at completion, so a
///    dropped chunk costs live scrollback, not the record.
/// </summary>
public sealed class OutputStreamer : IAsyncDisposable
{
    private readonly IOutputSink _sink;
    private readonly string _commandId;
    private readonly ILogger _log;
    private readonly CancellationTokenSource _stop = new();
    private readonly SemaphoreSlim _wake = new(0);
    private readonly Task _pump;
    private readonly object _gate = new();

    private StringBuilder _buffer = new();
    private int _seq;
    private bool _stopped;      // hub said truncated => stop streaming this command
    private bool _completed;

    public OutputStreamer(IOutputSink sink, string commandId, ILogger log)
    {
        _sink = sink;
        _commandId = commandId;
        _log = log;
        _pump = Task.Run(PumpAsync);
    }

    /// <summary>Buffer a chunk of RAW output text. Thread-safe, non-blocking, never throws.
    ///
    /// The text is appended verbatim -- no trailing '\n' is added, because the interactive
    /// shell delivers partial lines (a prompt with no newline is the whole point) and adding
    /// one per call would corrupt them. Line-oriented producers (ProcessRunner) append their
    /// own newline before calling this.</summary>
    public void Add(string text)
    {
        lock (_gate)
        {
            if (_stopped || _completed) return;
            _buffer.Append(text);
            // Only nudge the pump when we're already at the size threshold; otherwise let
            // the timer coalesce, which is the whole point of buffering.
            if (_buffer.Length < AgentConfig.StreamMaxChunkChars) return;
        }
        try { _wake.Release(); } catch (SemaphoreFullException) { /* already awake */ }
    }

    /// <summary>Flush whatever is left and stop. MUST be awaited before reporting the
    /// command's result: the console stops polling once the command reaches a terminal
    /// status, so a result that lands ahead of the final chunk silently truncates the
    /// tail the operator sees.</summary>
    public async Task CompleteAsync(CancellationToken ct)
    {
        lock (_gate) { _completed = true; }
        _stop.Cancel();
        try { _wake.Release(); } catch (SemaphoreFullException) { }
        try { await _pump.WaitAsync(ct); } catch (OperationCanceledException) { }
        await FlushOnceAsync(CancellationToken.None);
    }

    private async Task PumpAsync()
    {
        while (!_stop.IsCancellationRequested)
        {
            try
            {
                await _wake.WaitAsync(AgentConfig.StreamFlushMillis, _stop.Token);
            }
            catch (OperationCanceledException)
            {
                return; // CompleteAsync does the final flush
            }
            await FlushOnceAsync(_stop.Token);
        }
    }

    private async Task FlushOnceAsync(CancellationToken ct)
    {
        while (true)
        {
            string payload;
            int seq;
            lock (_gate)
            {
                if (_stopped || _buffer.Length == 0) return;

                // Split oversized buffers so the hub never has to reject one. Take whole
                // characters only; the boundary is cosmetic since the console concatenates.
                var take = Math.Min(_buffer.Length, AgentConfig.StreamMaxChunkChars);
                payload = _buffer.ToString(0, take);
                _buffer.Remove(0, take);
                seq = _seq++;
            }

            var delivered = await PostWithRetriesAsync(seq, payload, ct);
            if (!delivered) return;   // stopped or gave up; nothing more to do this pass
        }
    }

    /// <summary>Post one chunk, retrying the same seq. Returns false when the caller
    /// should stop flushing (hub truncated, or we gave up on this chunk).</summary>
    private async Task<bool> PostWithRetriesAsync(int seq, string payload, CancellationToken ct)
    {
        for (var attempt = 1; attempt <= AgentConfig.StreamPostRetries; attempt++)
        {
            OutputPostResult result;
            try
            {
                result = await _sink.PostOutputAsync(_commandId, seq, payload, ct);
            }
            catch (Exception e)
            {
                _log.LogDebug("Output post threw for {Id} seq {Seq}: {Msg}", _commandId, seq, e.Message);
                result = new OutputPostResult(Ok: false, Truncated: false);
            }

            if (result.Truncated)
            {
                lock (_gate) { _stopped = true; _buffer.Clear(); }
                _log.LogDebug("Hub capped output for {Id}; stopping stream", _commandId);
                return false;
            }
            if (result.Ok) return true;

            if (attempt < AgentConfig.StreamPostRetries)
            {
                try { await Task.Delay(200 * attempt, ct); }
                catch (OperationCanceledException) { return false; }
            }
        }

        // Dropped. seq has already advanced, so the hub sees a gap rather than a stall.
        _log.LogDebug("Dropped output chunk {Seq} for {Id} after {N} attempts",
            seq, _commandId, AgentConfig.StreamPostRetries);
        return true;
    }

    public async ValueTask DisposeAsync()
    {
        if (!_completed) await CompleteAsync(CancellationToken.None);
        _stop.Dispose();
        _wake.Dispose();
    }
}
