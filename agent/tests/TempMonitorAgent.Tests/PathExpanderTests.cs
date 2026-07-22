using System.Text.Json.Nodes;
using TempMonitorAgent.Backup;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The agent half of the path grammar, driven by tests/backup_path_vectors.json — the
/// SAME fixture tests/test_backup_paths.py reads.
///
/// That sharing is the entire point. The hub validates and previews patterns; the agent
/// expands them for real. If the two disagree, the console shows an operator one set of
/// folders and the machine backs up a different set, and nobody finds out until a restore
/// comes up short. Neither suite can drift without this file failing.
///
/// Registry discovery itself (Discover(), and the NTUSER.DAT mounting behind it) is not
/// unit-testable without a real machine and real profiles — so the grammar is tested from
/// the fixture, and discovery is exercised by the shape check at the bottom plus the live
/// end-to-end run on a VM.
/// </summary>
public class PathExpanderTests
{
    private static JsonObject LoadVectors()
    {
        for (var probe = new DirectoryInfo(AppContext.BaseDirectory); probe is not null; probe = probe.Parent)
        {
            var candidate = Path.Combine(probe.FullName, "tests", "backup_path_vectors.json");
            if (File.Exists(candidate))
                return JsonNode.Parse(File.ReadAllText(candidate))!.AsObject();
        }
        throw new FileNotFoundException("tests/backup_path_vectors.json not found");
    }

    /// <summary>Rebuild the fixture's machine description into the agent's own shape.</summary>
    private static MachineProfiles Profiles(JsonObject vectors)
    {
        var node = vectors["profiles"]!.AsObject();
        var env = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var kv in node["env"]!.AsObject())
            env[kv.Key] = kv.Value!.GetValue<string>();

        var users = new List<UserProfile>();
        foreach (var user in node["users"]!.AsArray())
        {
            var obj = user!.AsObject();
            var folders = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            foreach (var kv in obj["folders"]!.AsObject())
                folders[kv.Key] = kv.Value!.GetValue<string>();
            users.Add(new UserProfile(
                obj["name"]!.GetValue<string>(),
                obj["sid"]!.GetValue<string>(),
                obj["path"]!.GetValue<string>(),
                folders));
        }
        return new MachineProfiles(node["profile_root"]!.GetValue<string>(), env, users);
    }

    [Fact]
    public void MatchesEverySharedExpansionVector()
    {
        var vectors = LoadVectors();
        var profiles = Profiles(vectors);

        foreach (var entry in vectors["expansions"]!.AsArray())
        {
            var test = entry!.AsObject();
            var pattern = test["pattern"]!.GetValue<string>();
            var expected = test["expect"]!.AsArray().Select(n => n!.GetValue<string>()).ToList();
            var actual = PathExpander.Expand(pattern, profiles);

            Assert.True(expected.SequenceEqual(actual),
                $"{pattern} — {test["why"]!.GetValue<string>()}\n" +
                $"  expected: {string.Join(" | ", expected)}\n" +
                $"  actual:   {string.Join(" | ", actual)}");
        }
    }

    [Fact]
    public void RefusesAnUnknownTokenRatherThanMatchingNothing()
    {
        // The most valuable rule in the grammar: %Userss% must not quietly expand to
        // nothing and produce a green run over an empty backup.
        var profiles = Profiles(LoadVectors());
        var problems = new List<string>();
        var result = PathExpander.Expand(@"%Userss%\Desktop", profiles, problems);

        Assert.Empty(result);
        Assert.Single(problems);
        Assert.Contains("%userss%", problems[0], StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void ReportsAUserMissingAFolderInsteadOfGuessingOne()
    {
        // carol has no Documents in the fixture. Defaulting to <profile>\Documents is
        // exactly how a redirected folder gets "backed up" as an empty stub.
        var profiles = Profiles(LoadVectors());
        var problems = new List<string>();
        var result = PathExpander.Expand("%Documents%", profiles, problems);

        Assert.Single(result);
        Assert.Contains("bob", result[0]);
        Assert.Contains(problems, p => p.Contains("carol") && p.Contains("documents"));
    }

    [Fact]
    public void FollowsOneDriveRedirectionRatherThanTheLiteralProfilePath()
    {
        // The case this whole grammar exists for.
        var profiles = Profiles(LoadVectors());
        var token = PathExpander.Expand("%Desktop%", profiles);
        var literal = PathExpander.Expand(@"%Users%\Desktop", profiles);

        Assert.Contains(token, p => p.Contains("OneDrive"));
        Assert.DoesNotContain(literal, p => p.Contains("OneDrive"));
    }

    [Fact]
    public void ExplainsAMachineWithNoProfiles()
    {
        var empty = new MachineProfiles(@"C:\Users",
            new Dictionary<string, string>(), []);
        var problems = new List<string>();
        Assert.Empty(PathExpander.Expand(@"%Users%\Desktop", empty, problems));
        Assert.Contains(problems, p => p.Contains("no user profiles"));
    }

    [Fact]
    public void MatchesEverySharedExcludeVector()
    {
        var vectors = LoadVectors();
        var profiles = Profiles(vectors);

        foreach (var entry in vectors["excludes"]!.AsArray())
        {
            var test = entry!.AsObject();
            var patterns = test["patterns"]!.AsArray().Select(n => n!.GetValue<string>()).ToList();
            var why = test["why"]!.GetValue<string>();
            var matcher = new PathExpander.ExcludeMatcher(patterns, profiles);

            foreach (var path in test["excluded"]!.AsArray())
                Assert.True(matcher.Matches(path!.GetValue<string>()),
                    $"should EXCLUDE {path} — {why}");
            foreach (var path in test["kept"]!.AsArray())
                Assert.False(matcher.Matches(path!.GetValue<string>()),
                    $"should KEEP {path} — {why}");
        }
    }

    [Fact]
    public void AnEmptyExcludeListMatchesNothing()
    {
        var matcher = new PathExpander.ExcludeMatcher([], Profiles(LoadVectors()));
        Assert.True(matcher.IsEmpty);
        Assert.False(matcher.Matches(@"C:\anything\at\all.txt"));
    }

    [Theory]
    [InlineData(@"C:/Users/bob", @"C:\Users\bob")]
    [InlineData(@"C:\Users\bob\", @"C:\Users\bob")]
    [InlineData(@"C:\Users\\bob", @"C:\Users\bob")]
    [InlineData(@"C:\", @"C:\")]
    [InlineData(@"\\srv\share\dir", @"\\srv\share\dir")]
    public void NormalizesLikeTheHub(string input, string expected)
        => Assert.Equal(expected, PathExpander.Normalize(input));

    [Fact]
    public void DiscoversThisMachineWithoutThrowing()
    {
        // Not an assertion about THIS machine's contents — the build agent may have one
        // profile or twenty. It asserts the discovery path (including any NTUSER.DAT
        // mounting) completes and returns a coherent shape, because a throw here would
        // take out every backup on every machine.
        var profiles = PathExpander.Discover();
        Assert.NotNull(profiles);
        Assert.NotNull(profiles.Users);
        Assert.True(profiles.Env.ContainsKey("SystemDrive"),
            "SystemDrive should always resolve on Windows");
        foreach (var user in profiles.Users)
        {
            Assert.False(string.IsNullOrWhiteSpace(user.Name));
            Assert.False(string.IsNullOrWhiteSpace(user.Path));
        }
    }
}
