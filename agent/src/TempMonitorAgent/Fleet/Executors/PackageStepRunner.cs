using System.IO.Compression;
using System.Text.Json.Nodes;

namespace TempMonitorAgent.Fleet.Executors;

/// <summary>
/// The step engine behind <see cref="DeployPackageExecutor"/>: run `params.steps` in order
/// inside one working directory, with `{variables}` resolved from the payloads the hub
/// staged and from what earlier steps produced.
///
/// Split out of the executor because the two answer different questions. The executor owns
/// the DEPLOY — fetch the payloads, decide whether the software is really there afterward,
/// clean up on every path. This owns one STEP: build a command line, run it, judge its exit
/// code. Keeping them apart is also what lets the single-command recipe and the step list
/// share the payload and detection code instead of forking it.
///
/// Three properties are load-bearing:
///
///   * **A step is judged on its own exit codes.** Chaining an unpack and a driver install
///     into one `cmd /c a &amp;&amp; b` reports one number for two programs, and pnputil's
///     dialect (259 is a clean run that simply ran out of INFs) does not survive being
///     merged with someone else's. Each step carries its own set, defaulted by the hub.
///   * **The first failure stops the deploy**, unless the step says `continue_on_error`.
///     Carrying on past a failed unpack would hand pnputil an empty folder, which it would
///     cheerfully report success for.
///   * **Variables never escape the working directory by accident.** They are substituted
///     as plain text into command lines the operator wrote, but the paths they hold are all
///     produced here, under the attempt's own directory.
/// </summary>
public static class PackageStepRunner
{
    /// <summary>What one step did. `Continue` is separate from `Succeeded` because a step
    /// marked continue_on_error can fail and still let the deploy proceed -- and the log
    /// has to say both things happened.</summary>
    public readonly record struct StepOutcome(bool Succeeded, bool Continue);

    /// <summary>Run every step in order. Returns false as soon as one fails without
    /// `continue_on_error` set.</summary>
    public static async Task<bool> RunAllAsync(
        JsonArray steps, PackageVariables vars, int defaultTimeout,
        IReadOnlyCollection<int> defaultExitCodes, Action<string> say, Action<string> emit,
        CancellationToken ct)
    {
        var index = 0;
        foreach (var node in steps)
        {
            index++;
            if (node is not JsonObject step)
            {
                say($"[deploy] step {index}: FAILED: not an object");
                return false;
            }

            var kind = step.GetString("kind") ?? "";
            var label = step.GetString("name");
            say($"[deploy] step {index}/{steps.Count} — {kind}" +
                (string.IsNullOrWhiteSpace(label) ? "" : $": {label}"));

            var outcome = await RunOneAsync(step, kind, vars, defaultTimeout,
                                            defaultExitCodes, say, emit, ct);
            if (outcome.Succeeded) continue;
            if (outcome.Continue)
            {
                say($"[deploy] step {index} failed, carrying on as configured");
                continue;
            }
            say($"[deploy] FAILED at step {index}");
            return false;
        }
        return true;
    }

    private static async Task<StepOutcome> RunOneAsync(
        JsonObject step, string kind, PackageVariables vars, int defaultTimeout,
        IReadOnlyCollection<int> defaultExitCodes, Action<string> say, Action<string> emit,
        CancellationToken ct)
    {
        // continue_on_error is read up front so it applies to a malformed step too --
        // otherwise the one failure an operator most wants to skip past (a step this agent
        // is too old to understand) would be the one that ignores the flag.
        var carryOn = step["continue_on_error"] is JsonValue flag
                      && bool.TryParse(flag.ToString(), out var parsed) && parsed;
        StepOutcome Fail(string why)
        {
            say($"[deploy] {why}");
            return new StepOutcome(false, carryOn);
        }

        var timeout = Math.Clamp(step.GetInt("timeout_seconds", defaultTimeout), 30, 24 * 60 * 60);
        var codes = step.GetIntSet("success_exit_codes");
        if (codes.Count == 0) codes = new HashSet<int>(defaultExitCodes);

        try
        {
            switch (kind)
            {
                case "extract":
                {
                    // Pure .NET rather than shelling out to Expand-Archive: it is faster,
                    // it does not depend on a PowerShell version, and the zip-slip check
                    // below is ours to make rather than someone else's to have made.
                    var archive = vars.Resolve(step.GetString("archive"));
                    if (string.IsNullOrWhiteSpace(archive))
                        return Fail("FAILED: the unpack step has no archive");
                    var dest = vars.Resolve(step.GetString("dest"));
                    var saveAs = step.GetString("save_as") ?? "extracted";
                    if (string.IsNullOrWhiteSpace(dest)) dest = vars.PathIn(saveAs);

                    say($"[deploy] unpacking {archive} into {dest}");
                    var entries = Extract(archive, dest);
                    say($"[deploy] unpacked {entries} entries");
                    vars.Bind(saveAs, dest);
                    return new StepOutcome(true, carryOn);
                }

                case "run":
                {
                    var command = vars.Resolve(step.GetString("command"));
                    if (string.IsNullOrWhiteSpace(command))
                        return Fail("FAILED: the run step has no command");
                    return Judge(await Start(command, vars.Resolve(step.GetString("args"))),
                                 codes, timeout, say, carryOn);
                }

                case "powershell":
                {
                    var script = vars.Resolve(step.GetString("script"));
                    if (string.IsNullOrWhiteSpace(script))
                        return Fail("FAILED: the PowerShell step has no script");
                    // Written to a file and run with -File rather than passed with
                    // -Command: a script long enough to be worth writing here will contain
                    // quotes, and quoting it through a command line is a losing game. The
                    // file lands in the attempt's own directory, which is SYSTEM-owned and
                    // deleted afterward.
                    var path = vars.PathIn($"step-{Guid.NewGuid():N}.ps1");
                    await File.WriteAllTextAsync(path, script, ct);
                    var args = $"-NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"{path}\"";
                    return Judge(await Start("powershell.exe", args), codes, timeout, say, carryOn);
                }

                case "winget":
                {
                    var id = step.GetString("id");
                    if (string.IsNullOrWhiteSpace(id))
                        return Fail("FAILED: the winget step has no package id");
                    var winget = WingetLocator.Find();
                    if (winget is null) return Fail("FAILED: " + WingetLocator.NotFoundMessage);
                    var args = $"install --id {id} --silent --accept-package-agreements " +
                               "--accept-source-agreements";
                    var extra = vars.Resolve(step.GetString("args"));
                    if (!string.IsNullOrWhiteSpace(extra)) args += " " + extra;
                    return Judge(await Start(winget, args), codes, timeout, say, carryOn);
                }

                case "pnputil":
                {
                    var path = vars.Resolve(step.GetString("path"));
                    if (string.IsNullOrWhiteSpace(path))
                        return Fail("FAILED: the driver step has no path");
                    var subdirs = step["subdirs"] is not JsonValue s
                                  || !bool.TryParse(s.ToString(), out var wants) || wants;
                    // /add-driver takes an .inf or a pattern, never a bare folder -- point
                    // it at a directory and it fails with a usage error that says nothing
                    // about what was actually wrong. A driver pack IS a folder as far as an
                    // operator is concerned, so accept one and expand it here.
                    var target = Directory.Exists(path)
                        ? Path.Combine(path, "*.inf")
                        : path;
                    var args = $"/add-driver \"{target}\" /install" + (subdirs ? " /subdirs" : "");
                    var pnputil = Path.Combine(Environment.SystemDirectory, "pnputil.exe");
                    return Judge(await Start(pnputil, args), codes, timeout, say, carryOn);
                }

                default:
                    // A kind this build does not implement: an agent older than the hub
                    // that wrote the package. Fail closed and say which word it did not
                    // know, exactly like an unsupported detection kind.
                    return Fail($"FAILED: unsupported step kind '{kind}' — update the agent");
            }
        }
        catch (Exception e)
        {
            return Fail($"FAILED: {e.Message}");
        }

        Task<ProcessOutcome> Start(string file, string args)
        {
            say($"[deploy] running: {file} {args}");
            return ProcessRunner.RunAsync(file, args, ct, timeoutSeconds: timeout,
                                          workingDir: vars.WorkDir, onLine: emit);
        }

        StepOutcome Judge(ProcessOutcome outcome, HashSet<int> accepted, int seconds,
                          Action<string> log, bool keepGoing)
        {
            if (outcome.TimedOut)
            {
                log($"[deploy] FAILED: timed out after {seconds}s");
                return new StepOutcome(false, keepGoing);
            }
            if (!accepted.Contains(outcome.ExitCode))
            {
                log($"[deploy] FAILED: exit code {outcome.ExitCode} is not in " +
                    $"[{string.Join(", ", accepted.OrderBy(c => c))}]");
                return new StepOutcome(false, keepGoing);
            }
            log($"[deploy] exit code {outcome.ExitCode} accepted");
            return new StepOutcome(true, keepGoing);
        }
    }

    /// <summary>Unpack a zip into a directory, refusing entries that would land outside it.
    /// Returns the entry count.
    ///
    /// ZipFile.ExtractToDirectory does check this in current .NET, but the check is the
    /// entire security property of unpacking an archive an operator downloaded from a
    /// vendor, so it is made HERE rather than assumed of whatever runtime the agent is
    /// running on: an entry named ..\..\Windows\System32\driver.sys must not be written.
    /// </summary>
    public static int Extract(string archivePath, string destination)
    {
        Directory.CreateDirectory(destination);
        var root = Path.GetFullPath(destination);
        // The trailing separator matters: without it "C:\work\drivers-evil" passes a
        // StartsWith test against "C:\work\drivers".
        var rootPrefix = root.EndsWith(Path.DirectorySeparatorChar)
            ? root : root + Path.DirectorySeparatorChar;

        using var zip = ZipFile.OpenRead(archivePath);
        var count = 0;
        foreach (var entry in zip.Entries)
        {
            var target = Path.GetFullPath(Path.Combine(root, entry.FullName));
            if (!target.StartsWith(rootPrefix, StringComparison.OrdinalIgnoreCase))
                throw new IOException($"the archive entry '{entry.FullName}' points outside the folder");

            if (string.IsNullOrEmpty(entry.Name))
            {
                Directory.CreateDirectory(target);   // a directory entry
                continue;
            }
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            entry.ExtractToFile(target, overwrite: true);
            count++;
        }
        return count;
    }
}

/// <summary>
/// The `{name}` bindings for one deploy attempt, and the directory they live in.
///
/// Substitution is textual and deliberately narrow: only `{lowercase_words}` are replaced,
/// so an MSI product code like {90160000-008C-0000-1000-0000000FF1CE} in an operator's
/// arguments passes through untouched. The hub validates the same grammar at save time
/// (see packages._VARIABLE_RE), so an unknown name is refused there rather than reaching a
/// machine as a literal brace pair in a command line.
/// </summary>
public sealed class PackageVariables
{
    private readonly Dictionary<string, string> _bindings = new(StringComparer.Ordinal);

    public PackageVariables(string workDir)
    {
        WorkDir = workDir;
        Bind("work", workDir);
    }

    public string WorkDir { get; }

    public void Bind(string name, string value) => _bindings[name] = value;

    /// <summary>A path inside the attempt's working directory. The name is stripped to a
    /// file name first: a binding is ours, but the SUFFIX can come from a package
    /// definition, and joining an unchecked one would write outside the directory the
    /// executor promises to delete.</summary>
    public string PathIn(string name) =>
        Path.Combine(WorkDir, Path.GetFileName(name) is { Length: > 0 } safe ? safe : "out");

    public string Resolve(string? text)
    {
        if (string.IsNullOrEmpty(text)) return text ?? "";
        foreach (var (name, value) in _bindings)
            text = text.Replace("{" + name + "}", value, StringComparison.Ordinal);
        return text;
    }
}
