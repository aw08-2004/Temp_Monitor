using System.Text;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using Microsoft.Win32;
using TempMonitorAgent.Update;

namespace TempMonitorAgent.Fleet.Executors;

/// <summary>
/// deploy_package: install one package defined in the hub (roadmap #5).
///
/// The params are a full SNAPSHOT of the package recipe taken when the attempt was
/// dispatched — payloads, command line or step list, timeouts, success exit codes,
/// detection rule — not a package id to look up. That is what stops an operator editing a
/// package mid-rollout from giving half the fleet a different install (see
/// packages.build_command_params).
///
/// Four phases, and all four have to pass:
///
///   1. **Resolve the payloads.** A hub-hosted file is downloaded over the authenticated
///      channel and its sha256 checked against the digest the HUB computed at upload.
///      A url/unc payload is fetched/copied and checked only if the operator pinned a
///      hash. winget resolves its own payload and has its own trust chain. Every payload
///      is bound to its slot name, so a step can say which file it means.
///   2. **Run the recipe.** Either `params.steps` in order (see PackageStepRunner) or, for
///      a package written before steps existed, the single command line with {file}
///      replaced by the resolved path. Both shapes arrive on the wire; which one is
///      present is the hub's decision, not a negotiation.
///   3. **Check the exit code** against the success set (0 and 3010 by default — 3010 is
///      "installed, reboot required", and failing it would paint half a fleet's MSI
///      installs red). With steps, each step is judged on its own set.
///   4. **Check detection.** An installer exiting 0 is evidence, not proof: silent
///      installers routinely return 0 having done nothing, and that is exactly the
///      failure a fleet-wide push must not report as success. So the recipe also carries
///      a post-install check — a file, a registry value, or an installed-version floor —
///      and the deploy only succeeds if the software is actually THERE afterward. It runs
///      once, at the end, because it is a claim about the PACKAGE, not about a step.
///
/// The whole working directory is deleted afterward, on every path. It lives under the
/// agent's own %ProgramData% (SYSTEM-owned, like the self-updater's staging dir), not
/// %TEMP%, so a half-finished install can't leave an executable somewhere a standard user
/// could swap out before it runs.
/// </summary>
public sealed class DeployPackageExecutor : ICommandExecutor
{
    private readonly ILogger<DeployPackageExecutor> _log;
    private readonly IPackageDownloader _downloader;

    public DeployPackageExecutor(ILogger<DeployPackageExecutor> log, IPackageDownloader downloader)
    {
        _log = log;
        _downloader = downloader;
    }

    public string Type => "deploy_package";

    /// <summary>Where payloads are staged. Beside the self-updater's staging dir, under
    /// %ProgramData%, for the ACL reason in the class docstring.</summary>
    private static string StagingDir => Path.Combine(AgentConfig.ProgramDataDir, "packages");

    public async Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var packageName = cmd.Params.GetString("package_name") ?? "package";
        var legacySource = cmd.Params.GetObject("source");
        var steps = cmd.Params.GetArray("steps");
        // `sources` is the current wire shape; `source` is what a hub older than steps
        // sends. Falling back rather than requiring both keeps this agent working against
        // either, which matters because the hub is upgraded first and separately.
        var sources = ResolveSourceList(cmd, legacySource);

        if (legacySource is null && sources.Count == 0 && (steps is null || steps.Count == 0))
            return CommandResult.Fail("deploy_package requires params.source or params.steps");

        var timeout = Math.Clamp(cmd.Params.GetInt("timeout_seconds", 900), 30, 24 * 60 * 60);
        var successCodes = cmd.Params.GetIntSet("success_exit_codes");
        if (successCodes.Count == 0)
        {
            // The hub refuses to store an empty set, so this means a malformed or
            // truncated payload. Guessing {0} here would turn that into a silent
            // "succeeded" — refuse instead.
            return CommandResult.Fail("deploy_package params carry no success_exit_codes");
        }

        var log = new StringBuilder();

        // Say() is for OUR OWN messages: a bare line that still needs terminating.
        void Say(string line) => Emit(line + "\n");

        // Emit() is for text that ALREADY ends in a newline -- which is what
        // ProcessRunner hands its onLine callback (it re-adds the newline the line-event
        // API strips). The two are separate functions rather than one because passing Say
        // straight to onLine appends a SECOND newline, double-spacing every line of
        // installer output in both the live console and the stored result log. That is
        // exactly what happened here until this was split.
        void Emit(string text)
        {
            log.Append(text);
            onOutput?.Invoke(text);
        }

        var multiStep = steps is not null && steps.Count > 0;
        Say($"[deploy] {packageName} ({(multiStep ? $"{steps!.Count} steps" : legacySource.GetString("kind") ?? "")})");

        // One directory per attempt, deleted whole at the end. Per attempt rather than
        // shared, so a retry can never pick up a half-unpacked folder the previous one left
        // behind and install from it.
        var workDir = Path.Combine(StagingDir, Guid.NewGuid().ToString("N"));
        try
        {
            Directory.CreateDirectory(workDir);
            var vars = new PackageVariables(workDir);

            // ---- 1. payloads ----
            foreach (var source in sources)
            {
                var kind = source.GetString("kind") ?? "";
                if (kind is not ("upload" or "url" or "unc")) continue;
                var name = source.GetString("name") ?? "payload";
                var (path, error) = await ResolvePayloadAsync(source, kind, workDir, Say, ct);
                if (error is not null) return new CommandResult(false, log + "\n" + error);
                vars.Bind(name, path!);
                // `{file}` is what every package written before steps says, so a package
                // with exactly one payload keeps meaning what it always meant.
                if (sources.Count == 1) vars.Bind("file", path!);
            }

            // ---- 2 + 3. run and judge ----
            bool ran;
            if (multiStep)
            {
                ran = await PackageStepRunner.RunAllAsync(
                    steps!, vars, timeout, successCodes, Say, Emit, ct);
            }
            else
            {
                ran = await RunSingleCommandAsync(
                    cmd, legacySource, vars, timeout, successCodes, Say, Emit, ct);
            }
            if (!ran) return new CommandResult(false, log.ToString());

            // ---- 4. detection ----
            var detection = cmd.Params.GetObject("detection");
            var (detected, detail) = EvaluateDetection(detection);
            Say($"[deploy] detection: {detail}");
            if (!detected)
            {
                Say("[deploy] FAILED: the installer reported success but the software " +
                    "was not detected afterward");
                return new CommandResult(false, log.ToString());
            }

            Say($"[deploy] {packageName} installed");
            return new CommandResult(true, log.ToString());
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "deploy_package failed for {Package}", packageName);
            return new CommandResult(false, log + $"\n[deploy] FAILED: {e.Message}");
        }
        finally
        {
            // On every path, including a failure: a rejected or spent installer -- and
            // anything a step unpacked next to it -- must not stay on disk waiting to be
            // run by something else.
            try { if (Directory.Exists(workDir)) Directory.Delete(workDir, recursive: true); }
            catch (Exception e) { _log.LogDebug("Could not clean up {Path}: {Msg}", workDir, e.Message); }
        }
    }

    /// <summary>The payload list, from `sources` or from a lone legacy `source`.</summary>
    private static List<JsonObject> ResolveSourceList(FleetCommand cmd, JsonObject? legacySource)
    {
        if (cmd.Params.GetArray("sources") is { } array)
            return array.OfType<JsonObject>().ToList();
        // A legacy `source` of kind "multi" is the marker the hub sends for a step-based
        // package it has no single-command projection for. It names no file, so there is
        // nothing to resolve; a build that understands steps reads them instead.
        if (legacySource is not null && legacySource.GetString("kind") != "multi")
            return new List<JsonObject> { legacySource };
        return new List<JsonObject>();
    }

    /// <summary>The original one-command recipe: build the line, run it, judge the exit
    /// code. Kept whole rather than folded into a one-step list, because it is what every
    /// package written before steps runs through and its behaviour must not shift.</summary>
    private async Task<bool> RunSingleCommandAsync(
        FleetCommand cmd, JsonObject? source, PackageVariables vars, int timeout,
        HashSet<int> successCodes, Action<string> say, Action<string> emit, CancellationToken ct)
    {
        var kind = source.GetString("kind") ?? "";
        var (file, args, buildError) = BuildCommandLine(cmd, source, kind, vars);
        if (file is null)
        {
            say("[deploy] " + (buildError ?? "FAILED: deploy_package has no install command"));
            return false;
        }

        say($"[deploy] running: {file} {args}");
        // No workingDir, so ProcessRunner's default (System32) still applies. Steps run in
        // the attempt's own directory, which is the better cwd -- but changing it HERE
        // would change what every package written before steps does, and an installer that
        // writes a log next to its cwd would start dropping it somewhere that gets deleted.
        var outcome = await ProcessRunner.RunAsync(
            file, args, ct, timeoutSeconds: timeout, onLine: emit);

        if (outcome.TimedOut)
        {
            say($"[deploy] FAILED: timed out after {timeout}s");
            return false;
        }
        if (!successCodes.Contains(outcome.ExitCode))
        {
            say($"[deploy] FAILED: exit code {outcome.ExitCode} is not in " +
                $"[{string.Join(", ", successCodes.OrderBy(c => c))}]");
            return false;
        }
        say($"[deploy] exit code {outcome.ExitCode} accepted");
        return true;
    }

    /// <summary>Fetch (or copy) one payload and verify it. Returns (path, error).</summary>
    private async Task<(string? Path, string? Error)> ResolvePayloadAsync(
        JsonObject source, string kind, string workDir, Action<string> say, CancellationToken ct)
    {
        var sha = source.GetString("sha256");
        var fileName = source.GetString("file_name");
        // Never trust a name from the params as a path component — a "file_name" of
        // ..\..\Windows\System32\x.dll would otherwise write outside the working dir.
        var safeName = string.IsNullOrWhiteSpace(fileName)
            ? "payload.bin"
            : Path.GetFileName(fileName);
        var dest = Path.Combine(workDir, $"{Guid.NewGuid():N}-{safeName}");

        if (kind == "unc")
        {
            var unc = source.GetString("ref");
            if (string.IsNullOrWhiteSpace(unc))
                return (null, "[deploy] FAILED: the UNC source has no path");
            say($"[deploy] copying {unc}");
            try
            {
                Directory.CreateDirectory(StagingDir);
                File.Copy(unc, dest, overwrite: true);
            }
            catch (Exception e)
            {
                return (null, $"[deploy] FAILED: could not read {unc}: {e.Message}");
            }
            if (!string.IsNullOrEmpty(sha))
            {
                string actual;
                using (var stream = File.OpenRead(dest))
                    actual = Convert.ToHexString(
                        await System.Security.Cryptography.SHA256.HashDataAsync(stream, ct))
                        .ToLowerInvariant();
                if (!string.Equals(actual, sha.Trim().ToLowerInvariant(), StringComparison.Ordinal))
                {
                    try { File.Delete(dest); } catch { /* best effort */ }
                    return (null, $"[deploy] FAILED: sha256 mismatch (got {actual}, expected {sha})");
                }
                say("[deploy] sha256 verified");
            }
            return (dest, null);
        }

        var url = kind == "upload" ? source.GetString("download_url") : source.GetString("ref");
        if (string.IsNullOrWhiteSpace(url))
            return (null, "[deploy] FAILED: the payload source has no URL");
        if (kind == "upload" && string.IsNullOrEmpty(sha))
        {
            // The hub always knows the digest of a file it stores, so its absence means
            // a payload that was never verified — refuse rather than run it unchecked.
            return (null, "[deploy] FAILED: a hub-hosted payload arrived with no sha256");
        }

        say($"[deploy] downloading {url}");
        var error = await _downloader.DownloadPackageAsync(url, dest, sha, ct);
        if (error is not null)
            return (null, $"[deploy] FAILED: {error}");
        say(string.IsNullOrEmpty(sha) ? "[deploy] downloaded (unpinned)" : "[deploy] sha256 verified");
        return (dest, null);
    }

    /// <summary>Substitute {file} and assemble the process to start. A null File means the
    /// command could not be built, and Error says why.</summary>
    private static (string? File, string Args, string? Error) BuildCommandLine(
        FleetCommand cmd, JsonObject? source, string kind, PackageVariables vars)
    {
        var command = cmd.Params.GetString("install_command") ?? "";
        var args = cmd.Params.GetString("install_args") ?? "";

        if (kind == "winget")
        {
            // The agent builds winget's command line itself; the hub refuses to store one
            // for a winget package, so any extra switches are appended rather than
            // replacing ours. Mirrors InstallAppExecutor's flags for consistency.
            var id = source.GetString("id");
            if (string.IsNullOrWhiteSpace(id))
                return (null, "", "FAILED: the winget source has no package id");

            // Not the bare name: as a SYSTEM service we cannot see the per-user App
            // Execution Alias. See WingetLocator.
            var winget = WingetLocator.Find();
            if (winget is null)
                return (null, "", "FAILED: " + WingetLocator.NotFoundMessage);

            var wingetArgs =
                $"install --id {id} --silent --accept-package-agreements --accept-source-agreements";
            if (!string.IsNullOrWhiteSpace(args)) wingetArgs += " " + args;
            return (winget, wingetArgs, null);
        }

        command = vars.Resolve(command);
        args = vars.Resolve(args);
        return (string.IsNullOrWhiteSpace(command) ? null : command, args, null);
    }

    // ---------------------------------------------------------------- detection
    /// <summary>Evaluate the post-install check. Returns (passed, human-readable detail).
    ///
    /// Any failure to evaluate counts as NOT detected, never as detected. A registry read
    /// that throws means we do not know whether the software is there, and "we don't
    /// know" must not be reported to the console as a successful install.</summary>
    private (bool Passed, string Detail) EvaluateDetection(JsonObject? detection)
    {
        var kind = detection.GetString("kind") ?? "none";
        try
        {
            switch (kind)
            {
                case "none":
                    return (true, "no check configured (exit code only)");

                case "file_exists":
                {
                    var path = detection.GetString("path");
                    if (string.IsNullOrWhiteSpace(path))
                        return (false, "file_exists rule has no path");
                    var found = File.Exists(path) || Directory.Exists(path);
                    return (found, found ? $"found {path}" : $"NOT found: {path}");
                }

                case "registry_value":
                {
                    var root = OpenRoot(detection.GetString("root"));
                    var keyPath = detection.GetString("key");
                    var name = detection.GetString("name");
                    if (root is null || string.IsNullOrWhiteSpace(keyPath) || string.IsNullOrWhiteSpace(name))
                        return (false, "registry rule is incomplete");

                    using var key = root.OpenSubKey(keyPath);
                    var value = key?.GetValue(name);
                    if (value is null)
                        return (false, $"NOT found: {detection.GetString("root")}\\{keyPath}\\{name}");

                    // An absent `equals` means "must merely exist"; an empty one is a real
                    // exact match against the empty string, so check presence of the
                    // property rather than emptiness of the string.
                    if (detection is not null && detection.TryGetPropertyValue("equals", out var wanted)
                        && wanted is not null)
                    {
                        var want = wanted.ToString();
                        var got = value.ToString() ?? "";
                        return (string.Equals(got, want, StringComparison.OrdinalIgnoreCase),
                                $"{name} = '{got}' (wanted '{want}')");
                    }
                    return (true, $"{name} present");
                }

                case "installed_version":
                {
                    var product = detection.GetString("name");
                    if (string.IsNullOrWhiteSpace(product))
                        return (false, "installed_version rule has no product name");
                    var found = FindInstalledVersion(product);
                    if (found is null)
                        return (false, $"NOT installed: no entry matching '{product}'");

                    var min = detection.GetString("min_version");
                    if (string.IsNullOrWhiteSpace(min))
                        return (true, $"installed: {product} {found}");
                    var ok = VersionUtil.Compare(found, min) >= 0;
                    return (ok, $"installed {found}, required >= {min}");
                }

                default:
                    // A kind this build doesn't implement (an older agent against a newer
                    // hub). Fail closed: reporting success for a check we cannot perform
                    // is the one outcome that misleads.
                    return (false, $"unsupported detection kind '{kind}' — update the agent");
            }
        }
        catch (Exception e)
        {
            return (false, $"check failed: {e.Message}");
        }
    }

    private static RegistryKey? OpenRoot(string? root) => (root ?? "").ToUpperInvariant() switch
    {
        "HKLM" => Registry.LocalMachine,
        "HKCU" => Registry.CurrentUser,
        "HKCR" => Registry.ClassesRoot,
        "HKU" => Registry.Users,
        _ => null,
    };

    /// <summary>DisplayVersion of the first installed program whose DisplayName contains
    /// <paramref name="product"/>, or null.
    ///
    /// Both registry views are searched: a 32-bit application on 64-bit Windows registers
    /// under WOW6432Node, and checking only the native view would report perfectly
    /// installed software as missing. HKCU is searched last, for per-user installs.</summary>
    private static string? FindInstalledVersion(string product)
    {
        const string uninstall = @"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall";
        var roots = new (RegistryKey Hive, RegistryView View)[]
        {
            (Registry.LocalMachine, RegistryView.Registry64),
            (Registry.LocalMachine, RegistryView.Registry32),
            (Registry.CurrentUser, RegistryView.Default),
        };

        foreach (var (hive, view) in roots)
        {
            using var baseKey = RegistryKey.OpenBaseKey(
                hive == Registry.CurrentUser ? RegistryHive.CurrentUser : RegistryHive.LocalMachine,
                view);
            using var key = baseKey.OpenSubKey(uninstall);
            if (key is null) continue;

            foreach (var subName in key.GetSubKeyNames())
            {
                using var sub = key.OpenSubKey(subName);
                var displayName = sub?.GetValue("DisplayName") as string;
                if (string.IsNullOrEmpty(displayName)) continue;
                if (displayName.IndexOf(product, StringComparison.OrdinalIgnoreCase) < 0) continue;
                // No DisplayVersion is still "installed" — report 0 so a rule with no
                // minimum passes and one with a minimum correctly fails.
                return sub?.GetValue("DisplayVersion") as string ?? "0";
            }
        }
        return null;
    }
}
