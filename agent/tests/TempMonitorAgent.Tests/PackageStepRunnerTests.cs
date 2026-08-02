using System.IO.Compression;
using System.Text.Json.Nodes;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The step engine: ordering, failure propagation, variable substitution, and the two
/// places unpacking an operator-supplied archive can go wrong.
///
/// What is worth pinning here is not that a step runs — it is the behaviour a deploy log
/// cannot show you afterward:
///
///   * a failed step must STOP the ones after it (carrying on past a failed unpack hands
///     pnputil an empty folder, which it reports success for),
///   * `continue_on_error` must actually let the next one run,
///   * a zip entry pointing outside the destination must be refused rather than written,
///   * and an MSI product code in an argument must survive substitution untouched.
/// </summary>
public class PackageStepRunnerTests : IDisposable
{
    private readonly string _dir = Path.Combine(
        Path.GetTempPath(), "pkgsteps-" + Guid.NewGuid().ToString("N"));

    public PackageStepRunnerTests() => Directory.CreateDirectory(_dir);

    public void Dispose()
    {
        try { Directory.Delete(_dir, recursive: true); } catch { /* best effort */ }
        GC.SuppressFinalize(this);
    }

    /// <summary>A step that runs cmd.exe, so the engine reaches a real process.</summary>
    private static JsonObject Run(string args, params (string Key, JsonNode? Value)[] extra)
    {
        var step = new JsonObject
        {
            ["kind"] = "run",
            ["command"] = "cmd.exe",
            ["args"] = args,
        };
        foreach (var (key, value) in extra) step[key] = value;
        return step;
    }

    private async Task<(bool Ok, string Log)> RunAll(PackageVariables vars, params JsonObject[] steps)
    {
        var log = new System.Text.StringBuilder();
        var ok = await PackageStepRunner.RunAllAsync(
            new JsonArray(steps.Cast<JsonNode?>().ToArray()), vars,
            defaultTimeout: 60, defaultExitCodes: new[] { 0 },
            say: line => log.AppendLine(line), emit: text => log.Append(text),
            ct: CancellationToken.None);
        return (ok, log.ToString());
    }

    [Fact]
    public async Task StepsRunInOrder()
    {
        var (ok, log) = await RunAll(new PackageVariables(_dir),
            Run("/c echo first"), Run("/c echo second"));

        Assert.True(ok);
        Assert.True(log.IndexOf("first", StringComparison.Ordinal)
                    < log.IndexOf("second", StringComparison.Ordinal));
    }

    [Fact]
    public async Task AFailedStepStopsTheOnesAfterIt()
    {
        // The property that makes steps worth having over one chained command line: the
        // deploy stops where it broke, and the log says which step that was.
        var (ok, log) = await RunAll(new PackageVariables(_dir),
            Run("/c exit 1"), Run("/c echo never-reached"));

        Assert.False(ok);
        Assert.DoesNotContain("never-reached", log);
        Assert.Contains("FAILED at step 1", log);
    }

    [Fact]
    public async Task ContinueOnErrorLetsTheNextStepRun()
    {
        var (ok, log) = await RunAll(new PackageVariables(_dir),
            Run("/c exit 1", ("continue_on_error", true)), Run("/c echo carried-on"));

        Assert.True(ok);
        Assert.Contains("carried-on", log);
        Assert.Contains("carrying on as configured", log);
    }

    [Fact]
    public async Task AStepIsJudgedOnItsOwnExitCodes()
    {
        // pnputil's 259 is the real case: a clean driver install that ran out of INFs must
        // not be a failure just because the package's own set does not list it.
        var accepted = Run("/c exit 259", ("success_exit_codes", new JsonArray(0, 259)));
        var (ok, _) = await RunAll(new PackageVariables(_dir), accepted);
        Assert.True(ok);

        var rejected = Run("/c exit 259");
        var (failed, log) = await RunAll(new PackageVariables(_dir), rejected);
        Assert.False(failed);
        Assert.Contains("exit code 259 is not in", log);
    }

    [Fact]
    public async Task AnUnknownStepKindFailsClosed()
    {
        // An agent older than the hub that wrote the package. Reporting success for a step
        // it did not perform is the one outcome that misleads.
        var (ok, log) = await RunAll(new PackageVariables(_dir),
            new JsonObject { ["kind"] = "teleport" });

        Assert.False(ok);
        Assert.Contains("unsupported step kind 'teleport'", log);
    }

    [Fact]
    public async Task VariablesAreSubstitutedIntoArguments()
    {
        var vars = new PackageVariables(_dir);
        vars.Bind("payload", Path.Combine(_dir, "installer.msi"));

        var (ok, log) = await RunAll(vars, Run("/c echo {payload}"));

        Assert.True(ok);
        Assert.Contains("installer.msi", log);
        Assert.DoesNotContain("{payload}", log);
    }

    [Fact]
    public async Task AnMsiProductCodeIsNotAVariable()
    {
        // Braces are everywhere in msiexec command lines. Only lowercase word-shaped names
        // are bindings, so a GUID passes through as itself — see PackageVariables.
        var (ok, log) = await RunAll(new PackageVariables(_dir),
            Run("/c echo {90160000-008C-0000-1000-0000000FF1CE}"));

        Assert.True(ok);
        Assert.Contains("{90160000-008C-0000-1000-0000000FF1CE}", log);
    }

    [Fact]
    public async Task ExtractUnpacksAndBindsTheFolderForLaterSteps()
    {
        var zip = Path.Combine(_dir, "drivers.zip");
        using (var archive = ZipFile.Open(zip, ZipArchiveMode.Create))
        {
            var entry = archive.CreateEntry("chipset/driver.inf");
            using var writer = new StreamWriter(entry.Open());
            writer.Write("; an inf");
        }

        var vars = new PackageVariables(_dir);
        vars.Bind("pack", zip);
        var (ok, log) = await RunAll(vars,
            new JsonObject { ["kind"] = "extract", ["archive"] = "{pack}", ["save_as"] = "unpacked" },
            Run("/c echo {unpacked}"));

        Assert.True(ok);
        Assert.True(File.Exists(Path.Combine(_dir, "unpacked", "chipset", "driver.inf")));
        // The binding is what makes the next step able to say where the drivers went.
        Assert.Contains(Path.Combine(_dir, "unpacked"), log);
    }

    [Fact]
    public void ExtractRefusesAnEntryThatEscapesTheDestination()
    {
        // Zip slip. The archive comes from a vendor download an operator pasted a URL for,
        // and this runs as SYSTEM, so "..\..\Windows\System32\driver.sys" must be refused
        // rather than written.
        var zip = Path.Combine(_dir, "evil.zip");
        using (var stream = new FileStream(zip, FileMode.Create))
        using (var archive = new ZipArchive(stream, ZipArchiveMode.Create))
        {
            var entry = archive.CreateEntry(@"..\escaped.txt");
            using var writer = new StreamWriter(entry.Open());
            writer.Write("pwned");
        }

        var dest = Path.Combine(_dir, "unpack-here");
        var error = Assert.Throws<IOException>(() => PackageStepRunner.Extract(zip, dest));
        Assert.Contains("outside the folder", error.Message);
        Assert.False(File.Exists(Path.Combine(_dir, "escaped.txt")));
    }

    [Fact]
    public void ASiblingDirectoryWithTheSamePrefixIsNotInsideTheDestination()
    {
        // The trailing-separator half of the containment check: without it, a destination
        // of "unpack" would accept a path resolving into "unpack-evil".
        var zip = Path.Combine(_dir, "prefix.zip");
        using (var stream = new FileStream(zip, FileMode.Create))
        using (var archive = new ZipArchive(stream, ZipArchiveMode.Create))
        {
            var entry = archive.CreateEntry(@"..\unpack-evil\payload.txt");
            using var writer = new StreamWriter(entry.Open());
            writer.Write("x");
        }

        Assert.Throws<IOException>(
            () => PackageStepRunner.Extract(zip, Path.Combine(_dir, "unpack")));
        Assert.False(Directory.Exists(Path.Combine(_dir, "unpack-evil")));
    }

    [Fact]
    public void PathInNeverEscapesTheWorkingDirectory()
    {
        var vars = new PackageVariables(_dir);
        var path = vars.PathIn(@"..\..\Windows\System32\evil.dll");
        Assert.Equal(Path.Combine(_dir, "evil.dll"), path);
    }
}
