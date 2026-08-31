using System.Diagnostics;
using TempMonitorAgent.Fleet;

namespace TempMonitorAgent.Patch;

/// <summary>
/// Reads what this machine currently has available to install, from both sources.
///
/// <para><b>A source that fails contributes nothing and does not stop the other.</b> Windows
/// Server has no winget at all and never will (no Store, no App Installer — see
/// <c>WingetLocator.NotFoundMessage</c>), and a machine pointed at an unreachable WSUS cannot
/// search Windows Update. Either is normal somewhere in a fleet, and treating one as fatal
/// would mean a server reports no OS updates because it has no winget.</para>
///
/// <para><b>An EMPTY result is a real answer and must be reported as one.</b> That is the
/// single most important thing this class produces: an update that stops being offered is the
/// only honest evidence it installed, so a fully patched machine reporting nothing is what
/// closes out a patch run. See <c>PatchInventoryReporter</c>, which sends the empty payload,
/// and <c>hub/patches.py confirm_from_inventory</c>, which consumes the absence.</para>
/// </summary>
public static class PatchScanner
{
    /// <summary>How long winget gets before it is abandoned. `winget upgrade` refreshes its
    /// sources over the network on first use, which on a cold machine is slow but bounded;
    /// past this the answer is not worth a scan thread.</summary>
    private static readonly TimeSpan WingetTimeout = TimeSpan.FromMinutes(3);

    public sealed record Scan(IReadOnlyList<AvailableUpdate> Updates, string Error);

    public static Scan Read()
    {
        var updates = new List<AvailableUpdate>();
        var problems = new List<string>();

        var windows = WindowsUpdateApi.Search();
        updates.AddRange(windows.Updates);
        if (windows.Error.Length > 0) problems.Add(windows.Error);

        try
        {
            var winget = ReadWinget(out var wingetError);
            updates.AddRange(winget);
            if (wingetError.Length > 0) problems.Add(wingetError);
        }
        catch (Exception e) when (e is IOException or InvalidOperationException
                                       or UnauthorizedAccessException)
        {
            problems.Add($"winget could not be run: {e.Message}");
        }

        // Two sources can name the same thing (a store-delivered driver, say). The hub keys
        // on uid and would collapse them anyway; doing it here keeps the payload honest about
        // how many updates this machine actually has.
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var deduped = new List<AvailableUpdate>(updates.Count);
        foreach (var update in updates)
        {
            if (seen.Add(update.Uid)) deduped.Add(update);
        }

        return new Scan(deduped, string.Join("; ", problems));
    }

    private static IReadOnlyList<AvailableUpdate> ReadWinget(out string error)
    {
        error = "";
        var winget = WingetLocator.Find();
        if (winget is null)
        {
            // Not an error worth surfacing on every server in the fleet, forever. The
            // absence is expected on Windows Server and the message is long; the OS-update
            // half of this scan is unaffected either way.
            return [];
        }

        var psi = new ProcessStartInfo(winget)
        {
            // --include-unknown: a package whose installed version winget cannot determine is
            // still upgradable, and omitting it hides exactly the stale software an operator
            // is looking for. --disable-interactivity and the two agreement flags are what
            // stop this blocking forever on a prompt nobody can answer: this runs as SYSTEM
            // with no console attached.
            Arguments = "upgrade --include-unknown --disable-interactivity " +
                        "--accept-source-agreements",
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };

        using var process = Process.Start(psi);
        if (process is null)
        {
            error = "winget did not start";
            return [];
        }
        var stdout = process.StandardOutput.ReadToEnd();
        if (!process.WaitForExit((int)WingetTimeout.TotalMilliseconds))
        {
            try { process.Kill(entireProcessTree: true); } catch (InvalidOperationException) { }
            error = "winget did not finish in time";
            return [];
        }
        // A non-zero exit with parseable output still counts: winget returns a failure code
        // when a source could not be refreshed while still listing what it knows.
        return WingetUpgradeParser.Parse(stdout);
    }
}
