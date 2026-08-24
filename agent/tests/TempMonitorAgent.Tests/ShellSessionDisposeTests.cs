using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet.Shell;

namespace TempMonitorAgent.Tests;

/// <summary>
/// ShellSession.DisposeAsync used to swallow every failure, including a kill that did not
/// take -- which left "the agent believes it killed a shell and did not" unobservable, since
/// the caller's own try/catch cannot see a failure the callee already ate. It now warns on
/// that one step while still never throwing, because three call sites (GetOrCreateAsync,
/// ResetAsync, ShellSessionManager.DisposeAsync) tear sessions down on paths that cannot
/// handle an exception.
///
/// The risk in adding a warning there is noise, not silence: the idle reaper disposes
/// sessions routinely, so a warning on ordinary teardown would be a line every reap. That is
/// what these pin, across both teardown shapes -- a shell still running, and one that has
/// already exited.
///
/// Two branches are deliberately NOT claimed as covered here, because a test that cannot fail
/// is worse than an honest gap:
///   * a Kill that genuinely fails needs a process this test user may not terminate;
///   * the InvalidOperationException race (exited between HasExited and Kill) is a timing
///     window, not something a test can force -- disabling that catch leaves every test below
///     still passing, which is exactly why it is not asserted here.
/// Both are covered by inspection instead.
/// </summary>
public class ShellSessionDisposeTests
{
    [Fact]
    public async Task Tearing_down_a_live_shell_is_quiet()
    {
        var log = new RecordingLogger();
        var session = await ShellSession.StartAsync("cmd", log, CancellationToken.None);

        await session.DisposeAsync();

        Assert.False(session.IsAlive);
        Assert.DoesNotContain(log.Entries, e => e.Level >= LogLevel.Warning);
    }

    /// <summary>The other teardown shape: the operator ran `exit`, so the shell is already
    /// gone and there is nothing to kill. Reaping a dead session is routine -- the reaper
    /// explicitly collects sessions that are no longer alive -- so it must stay as quiet as
    /// reaping a live one.</summary>
    [Fact]
    public async Task Tearing_down_a_shell_that_already_exited_is_quiet()
    {
        var log = new RecordingLogger();
        var session = await ShellSession.StartAsync("cmd", log, CancellationToken.None);
        // `exit` ends cmd itself rather than the called script, so the submission's sentinel
        // never arrives and this comes back as a timeout -- the shell being dead IS the result
        // being set up here.
        await session.RunAsync("exit", timeoutSeconds: 5, _ => { }, CancellationToken.None);
        Assert.False(session.IsAlive);

        await session.DisposeAsync();

        Assert.DoesNotContain(log.Entries, e => e.Level >= LogLevel.Warning);
    }

    /// <summary>Teardown is a sweep: every later step still has to run even if an earlier one
    /// failed, and no caller is in a position to handle an exception from it.</summary>
    [Fact]
    public async Task Teardown_never_throws()
    {
        var session = await ShellSession.StartAsync("powershell", NullLogger.Instance,
                                                    CancellationToken.None);

        var ex = await Record.ExceptionAsync(async () => await session.DisposeAsync());

        Assert.Null(ex);
    }
}

/// <summary>Captures what was logged so a test can assert on absence of noise as well as
/// presence of a message. NullLogger reports IsEnabled(false), which would let a caller skip
/// formatting entirely -- this one is always enabled so nothing is lost.</summary>
internal sealed class RecordingLogger : ILogger
{
    internal readonly record struct Entry(LogLevel Level, string Message, Exception? Error);

    private readonly List<Entry> _entries = new();

    internal IReadOnlyList<Entry> Entries
    {
        get { lock (_entries) return _entries.ToArray(); }
    }

    public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;
    public bool IsEnabled(LogLevel logLevel) => true;

    public void Log<TState>(LogLevel logLevel, EventId eventId, TState state,
                            Exception? exception, Func<TState, Exception?, string> formatter)
    {
        lock (_entries) _entries.Add(new Entry(logLevel, formatter(state, exception), exception));
    }
}
