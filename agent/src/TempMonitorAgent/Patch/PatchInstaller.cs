using System.Diagnostics;
using System.Runtime.InteropServices;
using TempMonitorAgent.Fleet;

namespace TempMonitorAgent.Patch;

/// <summary>
/// Installs a named set of updates, through whichever source each one came from.
///
/// <para><b>Nothing here decides what "worked" means, and that is the point.</b> This class
/// reports what the install APIs said and whether a restart is now pending. It never claims an
/// update is installed — the hub decides that, later, by observing that the machine has
/// stopped offering it (hub/patches.py confirm_from_inventory). A Windows Update install that
/// returns <c>orcSucceeded</c> before its restart is genuinely not installed in any sense an
/// operator cares about, and treating that result as the answer is the failure mode this whole
/// feature is arranged to avoid.</para>
///
/// <para><b>Failures are per-update, not per-run.</b> One update that will not download must
/// not stop the other eleven: a patch night that stops at the first bad KB leaves a fleet in a
/// worse state than one that installs everything else and says which one failed.</para>
/// </summary>
public static class PatchInstaller
{
    private static readonly TimeSpan WingetTimeout = TimeSpan.FromMinutes(30);

    public sealed record Outcome(
        IReadOnlyList<string> Attempted, bool RebootRequired, string Output, bool AnySucceeded);

    /// <summary>Install every update in <paramref name="uids"/> that this machine still offers.
    ///
    /// The uid list comes from the hub, which resolved it against this machine's own last
    /// report — but that report may be minutes old, so the set is re-resolved against a live
    /// scan here. An update the machine no longer offers is skipped rather than demanded:
    /// asking Windows Update for a KB that is already installed produces a failure that reads
    /// exactly like a broken patch.</summary>
    public static Outcome Install(IReadOnlyCollection<string> uids, Action<string>? onOutput,
                                  CancellationToken ct)
    {
        var log = new List<string>();
        void Say(string line) { log.Add(line); onOutput?.Invoke(line); }

        var wanted = new HashSet<string>(uids, StringComparer.OrdinalIgnoreCase);
        var scan = PatchScanner.Read();
        var targets = scan.Updates.Where(u => wanted.Contains(u.Uid)).ToList();

        var missing = wanted.Count - targets.Count;
        if (missing > 0)
        {
            Say($"{missing} requested update(s) are no longer offered by this machine and " +
                $"were skipped.");
        }
        if (targets.Count == 0)
        {
            return new Outcome([], false, string.Join(Environment.NewLine, log), false);
        }

        var attempted = new List<string>();
        var anySucceeded = false;
        var reboot = false;

        var windows = targets.Where(u => u.Source == PatchSources.WindowsUpdate).ToList();
        if (windows.Count > 0)
        {
            var result = InstallWindowsUpdates(windows, Say, ct);
            attempted.AddRange(windows.Select(u => u.Uid));
            anySucceeded |= result.AnySucceeded;
            reboot |= result.RebootRequired;
        }

        foreach (var update in targets.Where(u => u.Source == PatchSources.Winget))
        {
            ct.ThrowIfCancellationRequested();
            attempted.Add(update.Uid);
            if (InstallWingetPackage(update, Say, ct)) anySucceeded = true;
        }

        return new Outcome(attempted, reboot, string.Join(Environment.NewLine, log),
                           anySucceeded);
    }

    // ------------------------------------------------------------------ Windows Update

    private sealed record WindowsOutcome(bool AnySucceeded, bool RebootRequired);

    /// <summary>Download and install through the Windows Update Agent, late-bound for the
    /// reason <c>WindowsUpdateApi</c> gives.
    ///
    /// The whole batch goes through one downloader and one installer rather than one each: the
    /// WUA serialises installs anyway, and a single call is what lets Windows order updates
    /// that depend on each other (a servicing-stack update before the cumulative that needs
    /// it). Per-update results are read back off the result object afterwards.</summary>
    private static WindowsOutcome InstallWindowsUpdates(
        IReadOnlyList<AvailableUpdate> updates, Action<string> say, CancellationToken ct)
    {
        object? session = null;
        try
        {
            var sessionType = Type.GetTypeFromProgID("Microsoft.Update.Session");
            var collectionType = Type.GetTypeFromProgID("Microsoft.Update.UpdateColl");
            if (sessionType is null || collectionType is null)
            {
                say("This machine has no Windows Update Agent.");
                return new WindowsOutcome(false, false);
            }
            session = Activator.CreateInstance(sessionType);
            var searcher = WindowsUpdateApi.Invoke(session, "CreateUpdateSearcher");
            var found = WindowsUpdateApi.Invoke(
                searcher, "Search", "IsInstalled=0 AND IsHidden=0");
            var available = found is null ? null : WindowsUpdateApi.Get(found, "Updates");
            if (available is null) return new WindowsOutcome(false, false);

            // Re-match by uid against the live search, rather than carrying COM objects out of
            // the scan: a COM update object belongs to the search that produced it, and the
            // scan above ran through a different session.
            var wanted = new HashSet<string>(updates.Select(u => u.Uid),
                                             StringComparer.OrdinalIgnoreCase);
            var batch = Activator.CreateInstance(collectionType);
            var count = WindowsUpdateApi.ToInt(WindowsUpdateApi.Get(available, "Count"));
            var chosen = 0;
            for (var i = 0; i < count; i++)
            {
                ct.ThrowIfCancellationRequested();
                var item = WindowsUpdateApi.Index(available, i);
                if (item is null) continue;
                var mapped = WindowsUpdateApi.Map(item);
                if (mapped is null || !wanted.Contains(mapped.Uid)) continue;
                // An update whose licence has not been accepted cannot be installed, and
                // accepting is the operator's approval expressed through the hub.
                try { WindowsUpdateApi.Invoke(item, "AcceptEula"); }
                catch (COMException) { /* most updates have no EULA */ }
                WindowsUpdateApi.Invoke(batch, "Add", item);
                chosen++;
            }
            if (chosen == 0) return new WindowsOutcome(false, false);

            say($"Downloading {chosen} update(s) through Windows Update...");
            var downloader = WindowsUpdateApi.Invoke(session, "CreateUpdateDownloader");
            WindowsUpdateApi.Set(downloader, "Updates", batch);
            var downloadResult = WindowsUpdateApi.Invoke(downloader, "Download");
            var downloadCode = WindowsUpdateApi.ToInt(
                WindowsUpdateApi.Get(downloadResult, "ResultCode"));
            say($"Download finished with result code {downloadCode}.");

            say($"Installing {chosen} update(s)...");
            var installer = WindowsUpdateApi.Invoke(session, "CreateUpdateInstaller");
            WindowsUpdateApi.Set(installer, "Updates", batch);
            var installResult = WindowsUpdateApi.Invoke(installer, "Install");
            var installCode = WindowsUpdateApi.ToInt(
                WindowsUpdateApi.Get(installResult, "ResultCode"));
            var reboot = ToBool(WindowsUpdateApi.Get(installResult, "RebootRequired"));

            // orcSucceeded = 2, orcSucceededWithErrors = 3. Both mean at least something
            // installed; anything else did not. Reported as-is rather than interpreted --
            // "succeeded" here still only means staged, see the class remarks.
            var ok = installCode is 2 or 3;
            say($"Install finished with result code {installCode}" +
                (reboot ? "; a restart is required." : "."));
            return new WindowsOutcome(ok, reboot);
        }
        catch (COMException e)
        {
            say($"Windows Update refused the install: 0x{e.HResult:X8}");
            return new WindowsOutcome(false, false);
        }
        finally
        {
            if (session is not null && Marshal.IsComObject(session))
                Marshal.ReleaseComObject(session);
        }
    }

    private static bool ToBool(object? value)
    {
        if (value is null) return false;
        try { return Convert.ToBoolean(value); }
        catch (Exception e) when (e is FormatException or InvalidCastException) { return false; }
    }

    // ------------------------------------------------------------------ winget

    private static bool InstallWingetPackage(AvailableUpdate update, Action<string> say,
                                             CancellationToken ct)
    {
        var winget = WingetLocator.Find();
        if (winget is null)
        {
            say(WingetLocator.NotFoundMessage);
            return false;
        }
        var psi = new ProcessStartInfo(winget)
        {
            // --silent and --disable-interactivity for the same reason the scan uses them:
            // this runs as SYSTEM with no console, so a prompt is a hang. `--id ... --exact`
            // stops a package id being treated as a search term that matches two things.
            Arguments = $"upgrade --id {update.NativeId} --exact --silent " +
                        "--disable-interactivity --accept-source-agreements " +
                        "--accept-package-agreements",
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        try
        {
            using var process = Process.Start(psi);
            if (process is null)
            {
                say($"winget did not start for {update.NativeId}.");
                return false;
            }
            var output = process.StandardOutput.ReadToEnd();
            if (!process.WaitForExit((int)WingetTimeout.TotalMilliseconds))
            {
                try { process.Kill(entireProcessTree: true); }
                catch (InvalidOperationException) { }
                say($"{update.NativeId} did not finish in time.");
                return false;
            }
            var ok = process.ExitCode == 0;
            say($"{update.NativeId} exited {process.ExitCode}.");
            if (!ok && output.Length > 0) say(Truncate(output, 500));
            return ok;
        }
        catch (Exception e) when (e is IOException or InvalidOperationException
                                       or UnauthorizedAccessException)
        {
            say($"{update.NativeId} could not be upgraded: {e.Message}");
            return false;
        }
    }

    private static string Truncate(string text, int limit) =>
        text.Length <= limit ? text : text[..limit] + "...";
}
