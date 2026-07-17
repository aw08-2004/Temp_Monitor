using Microsoft.Extensions.Logging.Abstractions;
using TempMonitorAgent;
using TempMonitorAgent.Fleet;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Covers OutputStreamer's contract with the hub. These are the properties the hub's
/// idempotency relies on, and every one of them is silently breakable by a plausible
/// refactor, so they're pinned here rather than left to review.
/// </summary>
public class OutputStreamerTests
{
    /// <summary>Records every post. Exists because FleetClient owns a real HttpClient
    /// and can't be pointed at a test double.</summary>
    private sealed class FakeSink : IOutputSink
    {
        private readonly object _gate = new();
        public List<(int Seq, string Text)> Posts { get; } = new();
        public Func<int, int, OutputPostResult>? Respond { get; set; }  // (seq, attempt) => result
        private readonly Dictionary<int, int> _attempts = new();

        public Task<OutputPostResult> PostOutputAsync(string commandId, int seq, string text, CancellationToken ct)
        {
            lock (_gate)
            {
                _attempts[seq] = _attempts.GetValueOrDefault(seq) + 1;
                Posts.Add((seq, text));
                var r = Respond?.Invoke(seq, _attempts[seq])
                        ?? new OutputPostResult(Ok: true, Truncated: false);
                return Task.FromResult(r);
            }
        }

        public string AllText() { lock (_gate) { return string.Concat(Posts.Select(p => p.Text)); } }
    }

    private static OutputStreamer NewStreamer(IOutputSink sink) =>
        new(sink, "cmd-1", NullLogger.Instance);

    [Fact]
    public async Task CompleteAsync_FlushesBufferedLines_AsOnePost()
    {
        var sink = new FakeSink();
        var s = NewStreamer(sink);
        s.Add("one");
        s.Add("two");
        s.Add("three");
        await s.CompleteAsync(CancellationToken.None);

        // Coalescing is the point: three lines inside one flush window must not become
        // three POSTs.
        Assert.Single(sink.Posts);
        Assert.Equal(0, sink.Posts[0].Seq);
        Assert.Equal("one\ntwo\nthree\n", sink.Posts[0].Text);
    }

    [Fact]
    public async Task SeqStartsAtZeroAndIncrements()
    {
        var sink = new FakeSink();
        var s = NewStreamer(sink);

        // Force a second chunk by exceeding the per-chunk cap.
        s.Add(new string('a', AgentConfig.StreamMaxChunkChars));
        s.Add(new string('b', AgentConfig.StreamMaxChunkChars));
        await s.CompleteAsync(CancellationToken.None);

        Assert.True(sink.Posts.Count >= 2);
        Assert.Equal(Enumerable.Range(0, sink.Posts.Count).ToList(), sink.Posts.Select(p => p.Seq).ToList());
    }

    [Fact]
    public async Task NoChunkExceedsTheHubsPerChunkCap()
    {
        var sink = new FakeSink();
        var s = NewStreamer(sink);
        s.Add(new string('x', AgentConfig.StreamMaxChunkChars * 3));
        await s.CompleteAsync(CancellationToken.None);

        // The hub 400s anything larger, so splitting must happen agent-side.
        Assert.All(sink.Posts, p => Assert.True(p.Text.Length <= AgentConfig.StreamMaxChunkChars));
    }

    [Fact]
    public async Task FailedPost_RetriesTheSameSeq()
    {
        var sink = new FakeSink();
        // Fail the first attempt at seq 0, succeed after.
        sink.Respond = (seq, attempt) =>
            new OutputPostResult(Ok: attempt > 1, Truncated: false);

        var s = NewStreamer(sink);
        s.Add("hello");
        await s.CompleteAsync(CancellationToken.None);

        // Reusing the seq is what makes the hub's INSERT OR IGNORE dedupe a retry of a
        // POST that actually landed. A fresh seq would duplicate the chunk instead.
        Assert.True(sink.Posts.Count >= 2);
        Assert.All(sink.Posts, p => Assert.Equal(0, p.Seq));
        Assert.All(sink.Posts, p => Assert.Equal("hello\n", p.Text));
    }

    [Fact]
    public async Task PermanentlyFailingPost_GivesUpAndDoesNotStall()
    {
        var sink = new FakeSink();
        sink.Respond = (_, _) => new OutputPostResult(Ok: false, Truncated: false);

        var s = NewStreamer(sink);
        s.Add("lost");
        // Must return rather than retry forever: a dead hub cannot be allowed to wedge
        // the command. Losing scrollback is fine, the result still carries the full text.
        await s.CompleteAsync(CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(10));

        Assert.Equal(AgentConfig.StreamPostRetries, sink.Posts.Count);
    }

    [Fact]
    public async Task Truncated_StopsAllFurtherPosts()
    {
        var sink = new FakeSink();
        sink.Respond = (_, _) => new OutputPostResult(Ok: true, Truncated: true);

        var s = NewStreamer(sink);
        s.Add("first");
        await s.CompleteAsync(CancellationToken.None);
        var afterFirst = sink.Posts.Count;

        s.Add("should be dropped");
        await s.CompleteAsync(CancellationToken.None);

        Assert.Equal(1, afterFirst);
        Assert.Equal(afterFirst, sink.Posts.Count);
    }

    [Fact]
    public async Task ConcurrentAdds_LoseNoLines()
    {
        var sink = new FakeSink();
        var s = NewStreamer(sink);

        // ProcessRunner raises stdout and stderr on separate threadpool threads, so Add
        // is genuinely concurrent in production.
        await Task.WhenAll(Enumerable.Range(0, 8).Select(t => Task.Run(() =>
        {
            for (var i = 0; i < 50; i++) s.Add($"t{t}-line{i}");
        })));
        await s.CompleteAsync(CancellationToken.None);

        var text = sink.AllText();
        for (var t = 0; t < 8; t++)
            for (var i = 0; i < 50; i++)
                Assert.Contains($"t{t}-line{i}\n", text);
    }

    [Fact]
    public async Task AddAfterComplete_IsIgnored()
    {
        var sink = new FakeSink();
        var s = NewStreamer(sink);
        await s.CompleteAsync(CancellationToken.None);

        s.Add("too late");
        await s.CompleteAsync(CancellationToken.None);

        Assert.Empty(sink.Posts);
    }

    [Fact]
    public async Task SinkThatThrows_DoesNotEscape()
    {
        var sink = new ThrowingSink();
        var s = NewStreamer(sink);
        s.Add("boom");
        // A broken sink must degrade to "no live output", never fail the command.
        await s.CompleteAsync(CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(10));
        Assert.True(sink.Called > 0);
    }

    private sealed class ThrowingSink : IOutputSink
    {
        public int Called;
        public Task<OutputPostResult> PostOutputAsync(string commandId, int seq, string text, CancellationToken ct)
        {
            Interlocked.Increment(ref Called);
            throw new InvalidOperationException("sink is broken");
        }
    }
}
