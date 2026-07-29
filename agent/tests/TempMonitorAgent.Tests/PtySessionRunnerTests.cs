using System.Collections.Concurrent;
using System.Text;
using Microsoft.Extensions.Logging.Abstractions;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Shell;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The session loop, against a fake hub. PtySessionTests proves the pseudoconsole works;
/// these prove the thing driving it does the right bookkeeping -- which is where the quiet
/// bugs live, because every one of them looks like "the terminal just stopped responding"
/// rather than like a crash.
/// </summary>
public class PtySessionRunnerTests
{
    /// <summary>A hub that behaves like the real one: it hands out queued input once, tracks
    /// the agent's cursor, and records the VT stream it is posted.</summary>
    private sealed class FakeHub : IPtyChannel
    {
        private readonly ConcurrentQueue<PtyInputItem> _queued = new();
        private readonly StringBuilder _output = new();
        private readonly object _gate = new();
        private int _seq;

        public bool Closing;
        public bool Gone;
        public bool Reachable = true;
        public string? ClosedReason;
        public int PostCount;
        public readonly ConcurrentBag<int> PostedSeqs = new();

        public string Output { get { lock (_gate) return _output.ToString(); } }

        public void Type(string data)
        {
            _queued.Enqueue(new PtyInputItem(_seq++, "data", data, 0, 0));
        }

        public void Resize(short cols, short rows)
        {
            _queued.Enqueue(new PtyInputItem(_seq++, "resize", "", cols, rows));
        }

        public Task<PtyInputBatch> PullPtyInputAsync(string sessionId, int afterSeq, CancellationToken ct)
        {
            if (Gone)
                return Task.FromResult(new PtyInputBatch(true, Array.Empty<PtyInputItem>(), afterSeq, true, true));
            if (!Reachable)
                return Task.FromResult(new PtyInputBatch(false, Array.Empty<PtyInputItem>(), afterSeq, false, false));

            var items = new List<PtyInputItem>();
            while (_queued.TryDequeue(out var item)) items.Add(item);
            var next = items.Count > 0 ? items[^1].Seq + 1 : afterSeq + 1;
            return Task.FromResult(new PtyInputBatch(true, items, next, Closing, false));
        }

        public Task<bool> PostPtyOutputAsync(string sessionId, int seq, string chunk, CancellationToken ct)
        {
            if (!Reachable) return Task.FromResult(false);
            PostCount++;
            PostedSeqs.Add(seq);
            lock (_gate) _output.Append(chunk);
            return Task.FromResult(true);
        }

        public Task ReportPtyClosedAsync(string sessionId, string reason, CancellationToken ct)
        {
            ClosedReason = reason;
            return Task.CompletedTask;
        }

        public async Task<bool> WaitForOutput(string needle, int timeoutMs = 20_000)
        {
            var deadline = DateTime.UtcNow.AddMilliseconds(timeoutMs);
            while (DateTime.UtcNow < deadline)
            {
                if (Output.Contains(needle)) return true;
                await Task.Delay(50);
            }
            return Output.Contains(needle);
        }
    }

    private static PtySessionRunner NewRunner(FakeHub hub) =>
        new(Guid.NewGuid().ToString("N"), hub, NullLogger.Instance);

    [Fact]
    public async Task TypedInput_ReachesTheShellAndItsOutputReachesTheHub()
    {
        var hub = new FakeHub();
        using var runner = NewRunner(hub);
        var run = runner.RunAsync("cmd", 100, 30, CancellationToken.None);

        Assert.True(await hub.WaitForOutput(">"), $"no prompt; got: {hub.Output}");
        hub.Type("echo round-trip-ok\r");
        Assert.True(await hub.WaitForOutput("round-trip-ok"), $"got: {hub.Output}");

        hub.Closing = true;
        var reason = await run;
        Assert.Equal("closed by the operator", reason);
        Assert.Equal("closed by the operator", hub.ClosedReason);
    }

    [Fact]
    public async Task Input_KeepsFlowingAfterTheQueueDrains()
    {
        // The agent-side half of the seq-rewind bug guarded in tests/test_terminal.py: the
        // cursor must only ever advance, and only when something actually arrived. Advancing
        // it on an EMPTY batch would push it past input that hadn't been queued yet.
        var hub = new FakeHub();
        using var runner = NewRunner(hub);
        var run = runner.RunAsync("cmd", 100, 30, CancellationToken.None);
        Assert.True(await hub.WaitForOutput(">"));

        hub.Type("echo first\r");
        Assert.True(await hub.WaitForOutput("first"), $"got: {hub.Output}");
        await Task.Delay(600);              // several polls with nothing queued
        hub.Type("echo second\r");
        Assert.True(await hub.WaitForOutput("second"), $"got: {hub.Output}");

        hub.Closing = true;
        await run;
    }

    [Fact]
    public async Task OutputSequence_IsGaplessAndMonotonic()
    {
        // The hub keys chunks on seq and the console concatenates them in order, so a gap or
        // a repeat lands as corruption in the middle of an escape sequence.
        var hub = new FakeHub();
        using var runner = NewRunner(hub);
        var run = runner.RunAsync("cmd", 100, 30, CancellationToken.None);
        Assert.True(await hub.WaitForOutput(">"));
        hub.Type("echo a & echo b & echo c\r");
        Assert.True(await hub.WaitForOutput("c"));

        hub.Closing = true;
        await run;

        var seqs = hub.PostedSeqs.OrderBy(x => x).ToList();
        Assert.Equal(Enumerable.Range(0, seqs.Count), seqs);
    }

    [Fact]
    public async Task ShellExiting_EndsTheSession()
    {
        var hub = new FakeHub();
        using var runner = NewRunner(hub);
        var run = runner.RunAsync("cmd", 100, 30, CancellationToken.None);
        Assert.True(await hub.WaitForOutput(">"));

        hub.Type("exit\r");

        var reason = await run.WaitAsync(TimeSpan.FromSeconds(30));
        Assert.Equal("the shell exited", reason);
        Assert.Equal("the shell exited", hub.ClosedReason);
    }

    [Fact]
    public async Task HubForgettingTheSession_EndsIt()
    {
        var hub = new FakeHub();
        using var runner = NewRunner(hub);
        var run = runner.RunAsync("cmd", 100, 30, CancellationToken.None);
        Assert.True(await hub.WaitForOutput(">"));

        hub.Gone = true;   // reaped, or the hub's DB was reset

        var reason = await run.WaitAsync(TimeSpan.FromSeconds(30));
        Assert.Equal("the hub no longer has this session", reason);
    }

    [Fact]
    public async Task Cancellation_TearsTheSessionDownCleanly()
    {
        var hub = new FakeHub();
        using var cts = new CancellationTokenSource();
        using var runner = NewRunner(hub);
        var run = runner.RunAsync("cmd", 100, 30, cts.Token);
        Assert.True(await hub.WaitForOutput(">"));

        // What a self-update or a service stop does. It must not hang, and the console must
        // be told why rather than being left polling a terminal that no longer exists.
        cts.Cancel();

        var reason = await run.WaitAsync(TimeSpan.FromSeconds(30));
        Assert.Equal("the agent is shutting down", reason);
        Assert.Equal("the agent is shutting down", hub.ClosedReason);
    }

    [Fact]
    public async Task Resize_IsForwardedToThePty()
    {
        var hub = new FakeHub();
        using var runner = NewRunner(hub);
        var run = runner.RunAsync("cmd", 80, 24, CancellationToken.None);
        Assert.True(await hub.WaitForOutput(">"));

        hub.Resize(137, 41);
        await Task.Delay(400);
        hub.Type("mode con\r");

        // `mode con` reports the console's real dimensions, so this asserts the resize
        // reached the pseudoconsole rather than just being consumed by the runner.
        Assert.True(await hub.WaitForOutput("137"), $"resize was not applied; got: {hub.Output}");

        hub.Closing = true;
        await run;
    }

    [Fact]
    public async Task AnUnreachableHub_EndsTheSessionRatherThanTypingIntoTheVoid()
    {
        var hub = new FakeHub();
        using var runner = NewRunner(hub);
        var run = runner.RunAsync("cmd", 100, 30, CancellationToken.None);
        Assert.True(await hub.WaitForOutput(">"));

        hub.Reachable = false;

        // OfflineGraceSeconds is 45s, so this is a long wait -- but the alternative to
        // asserting it is shipping a terminal that buffers forever while an operator types
        // into a shell nobody is watching.
        var reason = await run.WaitAsync(TimeSpan.FromSeconds(90));
        Assert.Equal("lost contact with the hub", reason);
    }
}
