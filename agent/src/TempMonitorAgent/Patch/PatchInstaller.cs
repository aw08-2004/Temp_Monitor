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
            var result = InstallWingetPackage(update, Say, ct);
            anySucceeded |= result.Succeeded;
            // A winget package can need a restart just as an OS update can -- msiexec
            // returns 3010 through winget unchanged. Without this the executor's
            // `if_required` policy never schedules a reboot for a winget-only batch, and the
            // update sits half-applied with nothing left to finish it.
            reboot |= result.RebootRequired;
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

    private sealed record WingetOutcome(bool Succeeded, bool RebootRequired);

    /// <summary>Exit codes from `winget upgrade` that mean the package installed.
    ///
    /// winget passes the wrapped installer's code through unchanged, so this is the MSI
    /// dialect: 0 is success and <b>3010 is ERROR_SUCCESS_REBOOT_REQUIRED</b> — "installed,
    /// finishes on restart". Treating 3010 as failure marks a package that installed
    /// perfectly as a failed update and spends a retry on a machine with nothing wrong with
    /// it, which is the same trap packages.DEFAULT_SUCCESS_EXIT_CODES documents on the hub
    /// side. 1641 is ERROR_SUCCESS_REBOOT_INITIATED — the installer is restarting the machine
    /// itself — and is success for the same reason.</summary>
    internal static readonly HashSet<int> WingetSuccessCodes = [0, 3010, 1641];

    /// <summary>Exit codes that additionally mean a restart is owed. Kept separate from the
    /// success set because "it worked" and "it needs a reboot to finish" are different facts
    /// and the executor's reboot policy reads only the second.</summary>
    internal static readonly HashSet<int> WingetRebootCodes = [3010, 1641];

    private static WingetOutcome InstallWingetPackage(AvailableUpdate update,
                                                      Action<string> say, CancellationToken ct)
    {
        var winget = WingetLocator.Find();
        if (winget is null)
        {
            say(WingetLocator.NotFoundMessage);
            return new WingetOutcome(false, false);
        }
        // Refused rather than escaped. A winget package id is a dotted identifier; anything
        // else reaching here is a mis-parse of winget's own table output or a hostile package
        // source, and neither is worth running a command line for. This is the same class of
        // bug Files/OpenItemExecutor.cs guards with Quoted/CmdQuoted (it cites CVE-2024-27980
        // by name) -- the id is quoted below as well, but a value that cannot appear in a
        // legitimate id should not be quoted into a command, it should be rejected.
        if (!WingetPackageId.IsSafe(update.NativeId))
        {
            say($"Refusing to upgrade {update.Uid}: '{update.NativeId}' is not a usable "
                + "winget package id.");
            return new WingetOutcome(false, false);
        }
        var psi = new ProcessStartInfo(winget)
        {
            // --silent and --disable-interactivity for the same reason the scan uses them:
            // this runs as SYSTEM with no console, so a prompt is a hang. `--id ... --exact`
            // stops a package id being treated as a search term that matches two things.
            // Quoted as well as validated. IsSafePackageId already excludes everything
            // CommandLineToArgvW treats as significant, so the quotes cannot be escaped out
            // of -- they are here because this codebase quotes values it interpolates into a
            // command line (Files/OpenItemExecutor.cs), and diverging from that in a new file
            // is how the convention stops being one.
            Arguments = $"upgrade --id \"{update.NativeId}\" --exact --silent " +
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
                return new WingetOutcome(false, false);
            }
            var output = process.StandardOutput.ReadToEnd();
            if (!process.WaitForExit((int)WingetTimeout.TotalMilliseconds))
            {
                try { process.Kill(entireProcessTree: true); }
                catch (InvalidOperationException) { }
                say($"{update.NativeId} did not finish in time.");
                return new WingetOutcome(false, false);
            }
            var code = process.ExitCode;
            var ok = WingetSuccessCodes.Contains(code);
            var reboot = WingetRebootCodes.Contains(code);
            say($"{update.NativeId} exited {code}"
                + (reboot ? " (installed; a restart is required)." : "."));
            if (!ok && output.Length > 0) say(Truncate(output, 500));
            return new WingetOutcome(ok, reboot);
        }
        catch (Exception e) when (e is IOException or InvalidOperationException
                                       or UnauthorizedAccessException)
        {
            say($"{update.NativeId} could not be upgraded: {e.Message}");
            return new WingetOutcome(false, false);
        }
    }


    private static string Truncate(string text, int limit) =>
        text.Length <= limit ? text : text[..limit] + "...";
}
