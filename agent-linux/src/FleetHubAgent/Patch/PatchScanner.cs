using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet;

namespace FleetHubAgent.Patch;

/// <summary>
/// What this machine's package manager says is available -- the Linux half of roadmap #14.
///
/// <para><b>Nothing here refreshes anything.</b> Not `apt-get update`, not a dnf metadata
/// sync. That is the single most important property of this file. A refresh is a network
/// operation that takes the package manager's lock, can run for minutes behind a slow mirror,
/// and would have this agent fighting the machine's own `apt-daily.timer` or
/// `dnf-makecache.timer` for it -- on every managed box, forever. What the hub wants is what
/// the machine currently believes, which is what its own refresh timer already maintains. So
/// both scans read the cached index and say so in the console (`patches.source.apt` names the
/// machine's refresh timer as what decides freshness).</para>
///
/// <para><b>Simulation, not action.</b> apt is asked for `-s upgrade`, which computes the
/// upgrade and performs none of it, with `Debug::NoLocking=true` so a running unattended
/// upgrade cannot block the scan (and the scan cannot block it). dnf is asked for
/// `check-update -C`, which is a query. Neither can change a package on this machine, which
/// matters because this runs unattended as root on a schedule nobody is watching.</para>
///
/// <para><b>`upgrade`, not `full-upgrade`.</b> The difference is the packages that can only be
/// upgraded by removing something else, and a fleet console that lists an update whose
/// installation would uninstall part of the machine is offering an operator a trap. Those are
/// exactly the ones a human should decide about at the machine, so they are left out of the
/// inventory rather than reported as ordinary work.</para>
///
/// <para>Parsing is separated from running so the fixtures that actually bite -- a wrapped dnf
/// line, an apt origin list with a security archive in the middle of it, a machine with
/// neither package manager -- are testable without a container per distribution.</para>
/// </summary>
public sealed class PatchScanner(ILogger<PatchScanner> log)
{
    /// <summary>The scan's own ceiling. Generous: apt's dependency solver on a big machine is
    /// seconds, and a mirror is never contacted. It exists so a package manager wedged on
    /// something unexpected costs one inventory pass rather than the loop.</summary>
    private const int TimeoutSeconds = 180;

    /// <summary>Defensive ceiling, matching hub/patches.py MAX_UPDATES_PER_REPORT. A machine
    /// that has been offline for a year genuinely offers thousands; the hub would drop the
    /// tail anyway, and truncating here keeps the heartbeat body small.</summary>
    internal const int MaxUpdates = 500;

    /// <summary>Read what is available, or say why not. Never throws.</summary>
    public async Task<PatchScan> ReadAsync(CancellationToken ct)
    {
        // apt first where both exist. A machine with apt-get is Debian-family whatever else
        // it has installed, and running dnf there would query an index nothing maintains.
        if (File.Exists("/usr/bin/apt-get") || File.Exists("/bin/apt-get"))
            return await ScanAptAsync(ct);
        if (File.Exists("/usr/bin/dnf") || File.Exists("/bin/dnf"))
            return await ScanDnfAsync(ct);

        // Not an error worth reporting: a container image or an immutable distribution
        // legitimately has neither, and an operator reading "no package manager" on every
        // heartbeat forever learns nothing. An empty list is the honest inventory.
        log.LogDebug("No supported package manager found; reporting no available updates");
        return new PatchScan(Array.Empty<AvailableUpdate>(), null);
    }

    private async Task<PatchScan> ScanAptAsync(CancellationToken ct)
    {
        var apt = ProcessRunner.ProgramPath("/usr/bin/apt-get", "/bin/apt-get");
        var run = await ProcessRunner.RunAsync(
            apt,
            new[] { "-s", "-q", "-o", "Debug::NoLocking=true", "upgrade" },
            TimeoutSeconds, onOutput: null, ct);

        // apt-get exits 0 with nothing to do as well as with a full upgrade plan, so the exit
        // code is only ever bad news here.
        if (run.TimedOut) return PatchScan.Failed($"apt-get did not finish in {TimeoutSeconds}s");
        if (run.ExitCode != 0)
            return PatchScan.Failed($"apt-get exited {run.ExitCode}: {FirstLine(run.Output)}");

        return new PatchScan(ParseApt(run.Output), null);
    }

    private async Task<PatchScan> ScanDnfAsync(CancellationToken ct)
    {
        var dnf = ProcessRunner.ProgramPath("/usr/bin/dnf", "/bin/dnf");

        // LC_ALL=C: ParseDnf stops at the first "Obsoleting" heading, and that word is
        // the English one. Without a locale override a non-English service locale would
        // produce a translated heading that the parser does not recognise, causing the
        // obsoleting section to leak into the inventory as phantom updates.
        var env = new Dictionary<string, string> { ["LC_ALL"] = "C" };

        // -C: answer from the cache. See the class remarks -- a metadata refresh here would
        // be a network call on somebody else's schedule.
        var run = await ProcessRunner.RunAsync(
            dnf, new[] { "-q", "-C", "check-update" }, TimeoutSeconds, onOutput: null, ct, env);

        if (run.TimedOut) return PatchScan.Failed($"dnf did not finish in {TimeoutSeconds}s");
        // **100 is the success case**, and mistaking it for a failure is the obvious way to
        // write this wrong: dnf exits 0 when nothing is available and 100 when something is.
        if (run.ExitCode is not (0 or 100))
            return PatchScan.Failed($"dnf exited {run.ExitCode}: {FirstLine(run.Output)}");

        var updates = ParseDnf(run.Output);
        if (updates.Count == 0) return new PatchScan(updates, null);

        // Which of them are security fixes, asked as a second query rather than parsed out of
        // updateinfo: `check-update --security` answers in the format already parsed above,
        // and on a machine whose repositories ship no updateinfo it answers with nothing --
        // which leaves every update `unknown`, the honest answer, rather than implying that
        // none of them is a security fix.
        var security = await ProcessRunner.RunAsync(
            dnf, new[] { "-q", "-C", "check-update", "--security" },
            TimeoutSeconds, onOutput: null, ct, env);

        if (!security.TimedOut && security.ExitCode is 0 or 100)
        {
            var names = ParseDnf(security.Output).Select(u => u.Uid)
                .ToHashSet(StringComparer.Ordinal);
            if (names.Count > 0)
            {
                updates = updates
                    .Select(u => names.Contains(u.Uid)
                        ? u with { Classification = PatchClassifications.Security }
                        : u)
                    .ToList();
            }
        }

        return new PatchScan(updates, null);
    }

    // ---------------------------------------------------------------- parsers

    /// <summary>
    /// Parse `apt-get -s upgrade`, which reports its plan one package per line:
    ///
    /// <code>
    /// Inst libssl3 [3.0.2-0ubuntu1.12] (3.0.2-0ubuntu1.15 Ubuntu:22.04/jammy-security [amd64])
    /// </code>
    ///
    /// <para><b>apt-get is parsed rather than `apt list --upgradable` for a stated reason:</b>
    /// apt prints "WARNING: apt does not have a stable CLI interface. Use with caution in
    /// scripts." on every invocation, and it means it. apt-get's output is the interface that
    /// scripts have consumed for two decades, and the `Inst`/`Conf` lines have not moved.</para>
    ///
    /// <para><b>The origin list is what says "security".</b> Everything after the new version
    /// inside the parentheses is where the package would come from -- `jammy-security` on
    /// Ubuntu, `Debian-Security:12/stable-security` on Debian -- and matching the word is what
    /// lets the console sort a routine version bump from a fix somebody is waiting on. A
    /// package offered from several archives at once counts as security if any of them is,
    /// which is how apt itself would install it.</para>
    /// </summary>
    internal static List<AvailableUpdate> ParseApt(string output)
    {
        var updates = new List<AvailableUpdate>();
        var seen = new HashSet<string>(StringComparer.Ordinal);

        foreach (var raw in SplitLines(output))
        {
            if (!raw.StartsWith("Inst ", StringComparison.Ordinal)) continue;

            var rest = raw[5..].Trim();
            var space = rest.IndexOf(' ');
            var name = space < 0 ? rest : rest[..space];
            // Architecture-qualified names (`libssl3:amd64`) are the same package; the hub's
            // uid regex accepts the colon, but two entries for one package would be two rows
            // for one decision.
            var colon = name.IndexOf(':');
            if (colon > 0) name = name[..colon];
            if (name.Length == 0 || !seen.Add(name)) continue;

            // The parenthesised group is "<new version> <origins...> [<arch>]". A line
            // without one is malformed rather than interesting, but the package name in it is
            // still true, so it is reported with no version rather than dropped.
            var open = rest.IndexOf('(');
            var close = open < 0 ? -1 : rest.IndexOf(')', open);
            var version = "";
            var origins = "";
            if (open >= 0 && close > open)
            {
                var inside = rest[(open + 1)..close];
                var cut = inside.IndexOf(' ');
                version = cut < 0 ? inside : inside[..cut];
                origins = cut < 0 ? "" : inside[(cut + 1)..];
            }

            updates.Add(new AvailableUpdate(
                Uid: name,
                Source: PatchSources.Apt,
                Title: version.Length > 0 ? $"{name} {version}" : name,
                Classification: origins.Contains("security", StringComparison.OrdinalIgnoreCase)
                    ? PatchClassifications.Security
                    : PatchClassifications.Other,
                RebootRequired: NeedsReboot(name)));

            if (updates.Count >= MaxUpdates) break;
        }
        return updates;
    }

    /// <summary>
    /// Parse `dnf check-update`, which is three columns:
    ///
    /// <code>
    /// kernel.x86_64          5.14.0-427.el9          baseos
    /// </code>
    ///
    /// <para><b>Two shapes have to be tolerated.</b> dnf wraps a long package name onto its
    /// own line and indents the columns under it, so a one-token line is remembered as a
    /// pending name rather than discarded. And everything from an `Obsoleting Packages`
    /// heading onwards describes packages being replaced rather than updates on offer, so the
    /// parse stops there -- listing them would offer an operator an "update" to a package the
    /// machine is about to lose.</para>
    ///
    /// <para>The `.arch` suffix is stripped from the name, for the same reason apt's `:amd64`
    /// is: the identity an operator approves is the package, not the build of it.</para>
    /// </summary>
    internal static List<AvailableUpdate> ParseDnf(string output)
    {
        var updates = new List<AvailableUpdate>();
        var seen = new HashSet<string>(StringComparer.Ordinal);
        string? pending = null;

        foreach (var raw in SplitLines(output))
        {
            if (raw.Length == 0) { pending = null; continue; }
            if (raw.StartsWith("Obsoleting", StringComparison.OrdinalIgnoreCase)) break;
            // dnf's own chatter, which -q does not always silence on every version.
            if (raw.StartsWith("Last metadata", StringComparison.OrdinalIgnoreCase)) continue;

            var indented = char.IsWhiteSpace(raw[0]);
            var parts = raw.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);

            string nevra;
            string version;
            if (indented && pending is not null && parts.Length >= 2)
            {
                // Continuation of a wrapped name: the columns belong to the line above.
                nevra = pending;
                version = parts[0];
                pending = null;
            }
            else if (!indented && parts.Length == 1)
            {
                // A name with its columns wrapped onto the next line. Nothing is emitted
                // until that line arrives -- a name with no version is not an update anybody
                // can read.
                pending = parts[0];
                continue;
            }
            else if (!indented && parts.Length >= 3)
            {
                nevra = parts[0];
                version = parts[1];
                pending = null;
            }
            else
            {
                pending = null;
                continue;
            }

            var dot = nevra.LastIndexOf('.');
            var name = dot > 0 ? nevra[..dot] : nevra;
            if (name.Length == 0 || !seen.Add(name)) continue;

            updates.Add(new AvailableUpdate(
                Uid: name,
                Source: PatchSources.Dnf,
                Title: $"{name} {version}",
                // Unknown until the --security query says otherwise. See ScanDnfAsync.
                Classification: PatchClassifications.Unknown,
                RebootRequired: NeedsReboot(name)));

            if (updates.Count >= MaxUpdates) break;
        }
        return updates;
    }

    /// <summary>Whether installing this package means a restart.
    ///
    /// **A name match, and deliberately a narrow one.** Neither package manager reports this:
    /// the honest signal on Debian is `/var/run/reboot-required`, which is a fact about the
    /// MACHINE after an install rather than about an update before one, and there is nothing
    /// equivalent to ask beforehand. A kernel package is the one case where the answer is
    /// knowable from the name and is what an operator planning a maintenance window actually
    /// needs to see, so that is all this claims. Everything else reports false, which is the
    /// safe direction to be wrong in: a machine that needed a restart still says so through
    /// `livepatch`/`needrestart` on the box, whereas a console that flagged every openssl
    /// update as requiring one would train people to ignore the flag.</summary>
    internal static bool NeedsReboot(string package) =>
        package.StartsWith("linux-image", StringComparison.Ordinal) ||
        package.StartsWith("linux-generic", StringComparison.Ordinal) ||
        package is "kernel" or "kernel-core" or "kernel-modules" or "kernel-PAE";

    private static IEnumerable<string> SplitLines(string text) =>
        (text ?? "").Split('\n').Select(l => l.TrimEnd('\r'));

    private static string FirstLine(string text) =>
        SplitLines(text).FirstOrDefault(l => l.Trim().Length > 0)?.Trim() ?? "";
}
