using System.Xml;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// Every XML file in the Android project parses.
///
/// **This exists because one mistake has now cost six builds.** XML forbids `--` inside a
/// comment, and this repo's house style puts double hyphens in prose by reflex -- every
/// docstring above this one is full of them. The manifest, the csproj, `strings.xml`,
/// `app_restrictions.xml` and `device_admin.xml` are the five files where that habit is a
/// build error rather than a style, and the error it produces names an MSBuild target and a
/// line number in a generated copy, not the file somebody edited.
///
/// A ninety-second Android build to discover a typo is the wrong feedback loop. This is the
/// same check in two hundred milliseconds, on a test run that needs no SDK, no workload and no
/// device -- which is the entire reason FleetHubAgent.Core.Tests exists at all.
///
/// It walks up from the test assembly to find the project rather than taking a path from the
/// build, so it works from `dotnet test`, from an IDE, and from the repo root alike.
/// </summary>
public class AndroidResourceXmlTests
{
    private static DirectoryInfo AndroidProject()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            var candidate = Path.Combine(dir.FullName, "src", "FleetHubAgent.Android");
            if (Directory.Exists(candidate)) return new DirectoryInfo(candidate);
            dir = dir.Parent;
        }
        throw new DirectoryNotFoundException(
            "Could not find src/FleetHubAgent.Android above " + AppContext.BaseDirectory);
    }

    public static TheoryData<string> XmlFiles()
    {
        var root = AndroidProject();
        var data = new TheoryData<string>();
        foreach (var path in Directory.EnumerateFiles(root.FullName, "*.xml",
                                                      SearchOption.AllDirectories))
        {
            // obj/ and bin/ hold generated and merged copies. A failure in one of those is a
            // symptom of a failure in a source file, which this already covers, and they are
            // full of files this project did not write.
            var relative = Path.GetRelativePath(root.FullName, path);
            if (relative.StartsWith("obj") || relative.StartsWith("bin")) continue;
            data.Add(relative);
        }
        return data;
    }

    [Theory]
    [MemberData(nameof(XmlFiles))]
    public void Parses(string relativePath)
    {
        var full = Path.Combine(AndroidProject().FullName, relativePath);
        var settings = new XmlReaderSettings { DtdProcessing = DtdProcessing.Prohibit };
        using var reader = XmlReader.Create(full, settings);
        // Reading to the end is what actually parses the comments; Create alone does not.
        while (reader.Read()) { }
    }

    /// <summary>Both csproj files, not just the app's.
    ///
    /// Neither is caught by the theory above, which only looks for *.xml, and BOTH have
    /// already been broken by the double-hyphen habit -- the Core one was among the first six.
    /// Checking only the app's was a gap that reappeared the moment Core grew a comment of its
    /// own, so this walks up to the parent and takes every project file under it.</summary>
    public static TheoryData<string> ProjectFiles()
    {
        var src = AndroidProject().Parent
                  ?? throw new DirectoryNotFoundException("No src/ above the Android project");
        var data = new TheoryData<string>();
        foreach (var path in Directory.EnumerateFiles(src.FullName, "*.csproj",
                                                      SearchOption.AllDirectories))
        {
            if (path.Contains($"{Path.DirectorySeparatorChar}obj{Path.DirectorySeparatorChar}")
                || path.Contains($"{Path.DirectorySeparatorChar}bin{Path.DirectorySeparatorChar}"))
                continue;
            data.Add(Path.GetRelativePath(src.FullName, path));
        }
        return data;
    }

    [Theory]
    [MemberData(nameof(ProjectFiles))]
    public void Every_csproj_parses_too(string relative)
    {
        var src = AndroidProject().Parent!;
        var full = Path.Combine(src.FullName, relative);
        Assert.True(File.Exists(full), full);
        using var reader = XmlReader.Create(full);
        while (reader.Read()) { }
    }

    [Fact]
    public void There_is_something_to_check()
    {
        // A guard against the guard: a path that stopped resolving would make every assertion
        // above pass vacuously, which is the one way a test like this fails silently.
        Assert.NotEmpty(XmlFiles());
    }
}
