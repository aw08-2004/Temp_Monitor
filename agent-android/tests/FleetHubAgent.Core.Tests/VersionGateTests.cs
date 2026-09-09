using System.Text.RegularExpressions;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// Catches the silent failure that would come from bumping this agent's version past the hub's
/// agent-train floor.
///
/// Nothing crashes if that happens. What happens instead is that the hub starts treating a
/// phone as a member of the Windows agent's release train, and three things go wrong at once,
/// none of them visibly: /api/report begins answering with the WINDOWS agent's latest version
/// (get_advertised_version), the console's MIN_*_AGENT gates start offering an Android device a
/// ConPTY terminal, a process list and a file browser that no Android app could ever answer,
/// and the dashboard counts these devices as outdated against a manifest they are not on.
///
/// It is exactly the kind of mistake a well-meaning "bring the version in line with the other
/// components" change would make, which is why the reasoning lives in AgentConfig.Version and
/// the enforcement lives here.
///
/// **This agent has THREE files in its version pair, not two**, which is the one thing here
/// that agent-linux does not have to worry about: AgentConfig.Version, the Core csproj and the
/// Android csproj, the last of which also carries the APK's versionName. A device installs by
/// APK, so a versionName that disagrees with what the agent reports would have the console and
/// the phone's own app info showing different versions of the same thing.
/// </summary>
public class VersionGateTests
{
    /// <summary>hub/app.py's AGENT_TRAIN_MIN_VERSION. Restated rather than read, because the
    /// hub is Python and this is a C# test -- if that constant ever moves, this test fails and
    /// the person moving it has to think about the Android agent, which is the point.</summary>
    private const string AgentTrainMinVersion = "3.0.0";

    [Fact]
    public void Version_stays_below_the_hubs_agent_train_floor()
    {
        Assert.True(
            Compare(AgentConfig.Version, AgentTrainMinVersion) < 0,
            $"AgentConfig.Version is {AgentConfig.Version}, at or above the hub's " +
            $"AGENT_TRAIN_MIN_VERSION ({AgentTrainMinVersion}). Read the note on " +
            "AgentConfig.Version before changing this. The hub no longer needs it -- it " +
            "picks a manifest by the platform a machine reports -- but the console's " +
            "MIN_*_AGENT gates still read a version number, and this one is what keeps " +
            "them from offering a phone a terminal it cannot provide.");
    }

    [Fact]
    public void Version_is_exactly_three_numeric_components()
    {
        // The same rule VERSIONING.md states for HUB_VERSION, applied here for the same
        // reason: the hub parses versions as digits and dots, and a suffix parses as nothing.
        Assert.Matches(@"^\d+\.\d+\.\d+$", AgentConfig.Version);
    }

    [Fact]
    public void Core_csproj_version_matches_AgentConfig()
    {
        Assert.Equal(AgentConfig.Version, VersionElement(
            Path.Combine("src", "FleetHubAgent.Core", "FleetHubAgent.Core.csproj")));
    }

    [Fact]
    public void Android_csproj_version_matches_AgentConfig()
    {
        // The APK's own version. It is a separate file from the one above and from
        // AgentConfig, and nothing but this test holds the three together -- there is no
        // release script yet.
        Assert.Equal(AgentConfig.Version, VersionElement(
            Path.Combine("src", "FleetHubAgent.Android", "FleetHubAgent.Android.csproj")));
    }

    [Fact]
    public void Android_csproj_versionName_matches_AgentConfig()
    {
        // ApplicationVersion (the integer Android sorts upgrades by) is NOT checked against
        // this -- it is a monotonically increasing build counter and has no relationship to a
        // dotted version. ApplicationDisplayVersion is the one a person reads, on the phone's
        // own app info screen, and it must agree with what the console shows.
        var csproj = ReadRepoFile(Path.Combine("src", "FleetHubAgent.Android", "FleetHubAgent.Android.csproj"));
        var match = Regex.Match(csproj, @"<ApplicationDisplayVersion>([^<]+)</ApplicationDisplayVersion>");
        Assert.True(match.Success, "No <ApplicationDisplayVersion> in FleetHubAgent.Android.csproj");
        Assert.Equal(AgentConfig.Version, match.Groups[1].Value.Trim());
    }

    private static string VersionElement(string relativePath)
    {
        var csproj = ReadRepoFile(relativePath);
        var match = Regex.Match(csproj, @"<Version>([^<]+)</Version>");
        Assert.True(match.Success, $"No <Version> element in {relativePath}");
        return match.Groups[1].Value.Trim();
    }

    /// <summary>Walk up from the test binary to the repo's agent-android tree. Beats an
    /// AppContext-relative literal, which breaks the moment the output path changes.</summary>
    private static string ReadRepoFile(string relativePath)
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            var candidate = Path.Combine(dir.FullName, relativePath);
            if (File.Exists(candidate)) return File.ReadAllText(candidate);
            dir = dir.Parent;
        }
        throw new FileNotFoundException($"Could not locate {relativePath} from {AppContext.BaseDirectory}");
    }

    /// <summary>Dotted-numeric compare, matching the hub's cmp_versions.</summary>
    private static int Compare(string a, string b)
    {
        var pa = a.Split('.').Select(int.Parse).ToArray();
        var pb = b.Split('.').Select(int.Parse).ToArray();
        for (var i = 0; i < Math.Max(pa.Length, pb.Length); i++)
        {
            var va = i < pa.Length ? pa[i] : 0;
            var vb = i < pb.Length ? pb[i] : 0;
            if (va != vb) return va > vb ? 1 : -1;
        }
        return 0;
    }
}
