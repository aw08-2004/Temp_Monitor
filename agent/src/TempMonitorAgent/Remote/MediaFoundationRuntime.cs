using Vortice.MediaFoundation;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Process-lifetime Media Foundation startup/shutdown.
///
/// MFStartup/MFShutdown are refcounted per process, and MFShutdown invalidates every MF object
/// still alive -- not just the ones the caller created. When the encoder owned that pair, the
/// moment we started recreating encoders mid-session (which the desktop-switch and
/// resolution-change rebuilds require) disposing the OLD encoder tore Media Foundation down
/// underneath the NEW one, producing failures far away from the cause.
///
/// Rather than depend on dispose ordering, MF is started once when the helper starts and shut
/// down once when it exits. Encoders come and go freely in between.
/// </summary>
internal static class MediaFoundationRuntime
{
    private static readonly object Gate = new();
    private static bool _started;

    /// <summary>Start Media Foundation if it isn't already. Idempotent and thread-safe.</summary>
    public static void EnsureStarted()
    {
        lock (Gate)
        {
            if (_started) return;
            MediaFactory.MFStartup(false);
            _started = true;
        }
    }

    /// <summary>Shut Media Foundation down. Call once, on the way out of the process, after
    /// every encoder has been disposed.</summary>
    public static void Shutdown()
    {
        lock (Gate)
        {
            if (!_started) return;
            _started = false;
            try { MediaFactory.MFShutdown(); } catch { /* shutting down anyway */ }
        }
    }
}
