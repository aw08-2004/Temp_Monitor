using Microsoft.Extensions.Logging.Abstractions;
using FleetHubAgent.Fleet;

namespace FleetHubAgent.Tests;

/// <summary>
/// Covers what the operator loses when the output stream is wrong, none of which looks like
/// an error anywhere.
///
/// Three silent failures, in the order they would bite:
///
///   * **A dropped tail.** The console stops polling for output the moment a command reaches
///     a terminal status, so a final flush that happens after the result is a run whose last
///     lines -- usually the ones saying what went wrong -- simply are not there.
///   * **A duplicated chunk.** The hub keys a chunk on (command, seq) with INSERT OR IGNORE,
///     so a retry that allocated a FRESH seq would print the same lines twice rather than
///     being the free no-op it is designed to be.
///   * **A wedged stream.** A hub that never answers must cost the chunk, not the command:
///     giving up and moving on is what keeps a script's narration from blocking forever.
/// </summary>
public class OutputStreamingTests
{
    /// <summary>A sink that records everything, and can be told to fail or to truncate.</summary>
    private sealed class FakeSink : IOutputSink
    {
        public readonly List<(int Seq, string Text)> Posts = new();
        public int FailFirst;          // fail this many posts before accepting anything
        public bool Truncate;          // answer "the hub has heard enough"
        private readonly object _gate = new();

        public Task<OutputPostResult> PostOutputAsync(
            string commandId, int seq, string text, CancellationToken ct)
        {
            lock (_gate)
            {
                Posts.Add((seq, text));
                if (Truncate) return Task.FromResult(new OutputPostResult(true, true));
                if (FailFirst > 0)
                {
                    FailFirst--;
                    return Task.FromResult(new OutputPostResult(false, false));
                }
                return Task.FromResult(new OutputPostResult(true, false));
            }
        }

        public string Delivered
        {
            get
            {
                lock (_gate)
                {
                    // What the console would render: each seq once, in order.
                    return string.Concat(Posts
                        .GroupBy(p => p.Seq)
                        .OrderBy(g => g.Key)
                        .Select(g => g.First().Text));
                }
            }
        }
    }

    private static OutputStreamer New(FakeSink sink) =>
        new(sink, "cmd-1", NullLogger.Instance);

    [Fact]
    public async Task Completing_flushes_what_has_not_been_posted_yet()
    {
        // The buffer is well under the flush threshold and the pump's timer has not fired, so
        // everything here exists only because CompleteAsync flushed it. That is the call the
        // Worker makes BEFORE reporting the result.
        var sink = new FakeSink();
        var streamer = New(sink);
        streamer.Add("step one\n");
        streamer.Add("step two\n");

        await streamer.CompleteAsync(CancellationToken.None);

        Assert.Equal("step one\nstep two\n", sink.Delivered);
    }

    [Fact]
    public async Task Sequence_numbers_start_at_zero_and_never_skip()
    {
        var sink = new FakeSink();
        var streamer = New(sink);
        // Two flushes' worth, forced by exceeding the chunk ceiling.
        streamer.Add(new string('a', AgentConfig.StreamMaxChunkChars + 10));
        await streamer.CompleteAsync(CancellationToken.None);

        var seqs = sink.Posts.Select(p => p.Seq).Distinct().OrderBy(s => s).ToArray();
        Assert.Equal(Enumerable.Range(0, seqs.Length), seqs);
        Assert.Equal(AgentConfig.StreamMaxChunkChars + 10, sink.Delivered.Length);
    }

    [Fact]
    public async Task A_retry_reuses_the_same_sequence_number()
    {
        // The hub's INSERT OR IGNORE makes a retry free ONLY if the seq is the same. A fresh
        // one would print the chunk twice in the console.
        var sink = new FakeSink { FailFirst = 2 };
        var streamer = New(sink);
        streamer.Add("once\n");

        await streamer.CompleteAsync(CancellationToken.None);

        Assert.Equal(3, sink.Posts.Count);                       // two refusals, then success
        Assert.All(sink.Posts, p => Assert.Equal(0, p.Seq));
        Assert.Equal("once\n", sink.Delivered);
    }

    [Fact]
    public async Task A_hub_that_never_answers_costs_the_chunk_and_not_the_command()
    {
        var sink = new FakeSink { FailFirst = 99 };
        var streamer = New(sink);
        streamer.Add("nobody will see this\n");

        // The point is that this RETURNS. A streamer that retried forever would hold the
        // command's result behind a hub that is down.
        await streamer.CompleteAsync(CancellationToken.None);

        Assert.Equal(AgentConfig.StreamPostRetries, sink.Posts.Count);
    }

    [Fact]
    public async Task A_truncating_hub_stops_the_stream_for_good()
    {
        var sink = new FakeSink { Truncate = true };
        var streamer = New(sink);
        streamer.Add("first\n");
        await streamer.CompleteAsync(CancellationToken.None);
        var afterFirst = sink.Posts.Count;

        streamer.Add("more, which must not be posted\n");
        await streamer.CompleteAsync(CancellationToken.None);

        Assert.Equal(1, afterFirst);
        Assert.Equal(afterFirst, sink.Posts.Count);
    }

    [Fact]
    public async Task Adding_after_completion_is_ignored_rather_than_throwing()
    {
        // ProcessRunner's drain callbacks can outlive the await that completed the command,
        // and an exception on that thread would stop the child's pipe being drained.
        var sink = new FakeSink();
        var streamer = New(sink);
        await streamer.CompleteAsync(CancellationToken.None);

        streamer.Add("late\n");
        await streamer.DisposeAsync();

        Assert.Empty(sink.Posts);
    }
}
