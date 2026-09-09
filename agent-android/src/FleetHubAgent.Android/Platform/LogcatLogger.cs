using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Android.Platform;

/// <summary>
/// Sends the agent's logs to logcat under one tag.
///
/// **Why not Serilog, which both other agents use.** The Windows agent writes a rolling file
/// because Windows has nowhere to put a service's stdout; the Linux agent writes stdout because
/// systemd journals, rotates and expires it. Android has logcat, which does the same job as the
/// journal -- a ring buffer the platform owns, that `adb logcat` reads and every bug-report
/// capture includes. A file under the app's data directory would be a second copy that nothing
/// rotates, that no support tool collects, and that an operator would have to root the device to
/// read.
///
/// One tag, "FleetHubAgent", so `adb logcat -s FleetHubAgent:*` is the whole story.
///
/// **Known limitation, stated rather than fixed:** logcat is a ring buffer, so on a chatty
/// device the agent's history is measured in minutes. That is fine for diagnosing a device in
/// hand and useless for "why did this tablet stop reporting on Tuesday" -- for which the answer
/// has to come from the hub's own side. Recorded in ROADMAP.MD #23.
/// </summary>
internal sealed class LogcatLoggerProvider(LogLevel minimum) : ILoggerProvider
{
    private const string Tag = "FleetHubAgent";

    public ILogger CreateLogger(string categoryName) =>
        new LogcatLogger(ShortCategory(categoryName), minimum);

    /// <summary>The class name without its namespace. logcat lines are read on a phone screen,
    /// and "FleetHubAgent.Fleet.FleetClient" spends half a line saying what the tag already
    /// said.</summary>
    private static string ShortCategory(string category)
    {
        var dot = category.LastIndexOf('.');
        return dot >= 0 && dot < category.Length - 1 ? category[(dot + 1)..] : category;
    }

    public void Dispose() { }

    private sealed class LogcatLogger(string category, LogLevel minimum) : ILogger
    {
        public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;

        public bool IsEnabled(LogLevel logLevel) => logLevel >= minimum && logLevel != LogLevel.None;

        public void Log<TState>(
            LogLevel logLevel, EventId eventId, TState state, Exception? exception,
            Func<TState, Exception?, string> formatter)
        {
            if (!IsEnabled(logLevel)) return;

            var message = $"[{category}] {formatter(state, exception)}";
            if (exception is not null) message += global::System.Environment.NewLine + exception;

            switch (logLevel)
            {
                case LogLevel.Critical:
                case LogLevel.Error:
                    global::Android.Util.Log.Error(Tag, message);
                    break;
                case LogLevel.Warning:
                    global::Android.Util.Log.Warn(Tag, message);
                    break;
                case LogLevel.Information:
                    global::Android.Util.Log.Info(Tag, message);
                    break;
                default:
                    global::Android.Util.Log.Debug(Tag, message);
                    break;
            }
        }
    }
}
