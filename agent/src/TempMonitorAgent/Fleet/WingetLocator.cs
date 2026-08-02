using System.Runtime.InteropServices;

namespace TempMonitorAgent.Fleet;

/// <summary>Finds the real winget.exe for a process running as SYSTEM.
///
/// "winget" on a normal desktop is an App Execution Alias — a zero-byte reparse point in
/// %LOCALAPPDATA%\Microsoft\WindowsApps that the shell redirects into the packaged app.
/// That directory is PER USER. The agent runs as a service under SYSTEM, whose
/// %LOCALAPPDATA% is C:\Windows\System32\config\systemprofile\AppData\Local, and that
/// copy of WindowsApps does not contain winget. So Process.Start("winget.exe") from the
/// service throws Win32Exception 2 ("the system cannot find the file specified") on a
/// machine where winget works perfectly for the logged-in operator — which is exactly how
/// every winget deploy was failing.
///
/// The fix is to skip the alias and run the packaged binary directly out of
/// C:\Program Files\WindowsApps\Microsoft.DesktopAppInstaller_&lt;ver&gt;_&lt;arch&gt;__8wekyb3d8bbwe.
/// SYSTEM can read that directory (a standard user cannot, which is why this cannot be
/// probed while debugging from an ordinary shell). Several versions can sit side by side
/// during a Store update, so the highest version wins.
///
/// Resolution is cached: it is a directory enumeration of WindowsApps, and a fleet-wide
/// rollout would otherwise repeat it for every package on every machine.</summary>
public static class WingetLocator
{
    private static string? _cached;
    private static readonly object Gate = new();

    /// <summary>What to tell an operator when Find() returns null.
    ///
    /// Held here rather than written at each call site because it is the same three facts
    /// every time, and the Windows Server one is the least obvious: Server ships no Store
    /// and no App Installer, so winget there is not "missing", it was never going to be
    /// present, and no amount of installing it by hand is the answer.</summary>
    public const string NotFoundMessage =
        "winget (App Installer) was not found on this machine. On Windows Server this is " +
        "expected -- there is no Store and no App Installer -- so use an upload or url " +
        "payload for this package instead. On Windows 10/11, install 'App Installer' from " +
        "the Microsoft Store. (Note that typing 'winget' into a SYSTEM shell always fails " +
        "even where winget works, because the alias is per-user; that is not evidence " +
        "either way.)";

    /// <summary>Full path to winget.exe, or null if the App Installer is not present.</summary>
    public static string? Find()
    {
        lock (Gate)
        {
            // A cached path goes stale when an App Installer update moves the binary into a
            // new versioned directory, so re-check that it is still there rather than
            // handing back a path that will throw "file not found" on the next deploy.
            if (_cached is not null && File.Exists(_cached)) return _cached;
            _cached = Locate();
            return _cached;
        }
    }

    /// <summary>Drop the cached path — after an App Installer update moves the binary to a
    /// new versioned directory, the old path is gone and must be resolved again.</summary>
    public static void Invalidate()
    {
        lock (Gate) { _cached = null; }
    }

    private static string? Locate()
    {
        // 1. The packaged binary. This is the path that works under SYSTEM.
        var programFiles = Environment.GetEnvironmentVariable("ProgramW6432")
                           ?? Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles);
        var windowsApps = Path.Combine(programFiles, "WindowsApps");
        var best = HighestVersionedWinget(windowsApps);
        if (best is not null) return best;

        // 2. The alias, for the case where the agent is being run interactively as a user
        //    (the debug/console path) rather than as the service.
        var local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        var alias = Path.Combine(local, "Microsoft", "WindowsApps", "winget.exe");
        if (File.Exists(alias)) return alias;

        // 3. Anything on PATH, for an unpackaged/portable install.
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "")
                     .Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries))
        {
            try
            {
                var candidate = Path.Combine(dir.Trim(), "winget.exe");
                // Skip a WindowsApps alias reached via PATH: it is a 0-byte reparse point
                // that only resolves for the user it belongs to, so "found" there would be
                // a lie for the service.
                if (candidate.Contains(@"\WindowsApps\", StringComparison.OrdinalIgnoreCase)) continue;
                if (File.Exists(candidate)) return candidate;
            }
            catch
            {
                // A malformed PATH entry (illegal characters) must not stop the search.
            }
        }

        return null;
    }

    /// <summary>Newest winget.exe under WindowsApps, or null.</summary>
    private static string? HighestVersionedWinget(string windowsApps)
    {
        List<string> dirs;
        try
        {
            if (!Directory.Exists(windowsApps)) return null;
            dirs = Directory.GetDirectories(windowsApps, "Microsoft.DesktopAppInstaller_*").ToList();
        }
        catch
        {
            // UnauthorizedAccess when not running as SYSTEM — fall through to the alias.
            return null;
        }

        // Windows keeps more than the architecture build here. A real machine showed:
        //   Microsoft.DesktopAppInstaller_1.29.279.0_x64__8wekyb3d8bbwe            <- the real one
        //   Microsoft.DesktopAppInstaller_2026.623.1704.0_neutral_~_8wekyb3d8bbwe  <- resource stub
        // The stub carries a much HIGHER version, so a pure highest-version-wins search points
        // at it. Today that is harmless only because the stub contains no winget.exe and the
        // File.Exists check below drops it -- which is luck, not a decision. Rank explicitly
        // instead: an architecture-matching package beats a neutral one, and a "~" (staged /
        // resource) package loses to anything else, before version is even consulted.
        // Only directories that actually hold the binary are candidates; the rest of the
        // decision is pure string ranking, which is where the subtlety is, so it lives in
        // PickBestPackage where it can be tested without a filesystem.
        var candidates = dirs.Where(d => File.Exists(Path.Combine(d, "winget.exe"))).ToList();
        var chosen = PickBestPackage(candidates.Select(Path.GetFileName)!, CurrentArchitecture());
        return chosen is null ? null : Path.Combine(windowsApps, chosen, "winget.exe");
    }

    internal static string CurrentArchitecture() => RuntimeInformation.OSArchitecture switch
    {
        Architecture.Arm64 => "arm64",
        Architecture.X86 => "x86",
        _ => "x64",
    };

    /// <summary>
    /// Choose the App Installer package directory to run winget from, given their names.
    ///
    /// Rank before version, because version alone picks the wrong one: an architecture build
    /// beats a neutral one, and a staged/resource package ("~" in the name) loses to anything
    /// else. Only within a rank does the highest version win.
    /// </summary>
    internal static string? PickBestPackage(IEnumerable<string> directoryNames, string architecture)
    {
        string? best = null;
        (int rank, Version version) bestKey = (int.MinValue, new Version(0, 0));

        foreach (var name in directoryNames)
        {
            if (string.IsNullOrEmpty(name)) continue;
            // Microsoft.DesktopAppInstaller_1.22.10582.0_x64__8wekyb3d8bbwe
            var parts = name.Split('_');
            var version = parts.Length > 1 && Version.TryParse(parts[1], out var v)
                ? v : new Version(0, 0);

            bool staged = parts.Contains("~");
            bool architectureMatch = parts.Any(
                p => p.Equals(architecture, StringComparison.OrdinalIgnoreCase));
            int rank = staged ? 0 : architectureMatch ? 2 : 1;

            if (best is null || (rank, version).CompareTo(bestKey) > 0)
            {
                best = name;
                bestKey = (rank, version);
            }
        }
        return best;
    }
}
