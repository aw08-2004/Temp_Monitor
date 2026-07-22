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
/// dispatched — payload, command line, timeout, success exit codes, detection rule — not
/// a package id to look up. That is what stops an operator editing a package mid-rollout
/// from giving half the fleet a different install (see packages.build_command_params).
///
/// Four steps, and all four have to pass:
///
///   1. **Resolve the payload.** A hub-hosted file is downloaded over the authenticated
///      channel and its sha256 checked against the digest the HUB computed at upload.
///      A url/unc payload is fetched/copied and checked only if the operator pinned a
///      hash. winget resolves its own payload and has its own trust chain.
///   2. **Run it**, with {file} replaced by the resolved local path, under the package's
///      own timeout.
///   3. **Check the exit code** against the package's success set (0 and 3010 by
///      default — 3010 is "installed, reboot required", and failing it would paint half
///      a fleet's MSI installs red).
///   4. **Check detection.** An installer exiting 0 is evidence, not proof: silent
///      installers routinely return 0 having done nothing, and that is exactly the
///      failure a fleet-wide push must not report as success. So the recipe also carries
///      a post-install check — a file, a registry value, or an installed-version floor —
///      and the deploy only succeeds if the software is actually THERE afterward.
///
/// The payload is deleted afterward, on every path. These land in the agent's own
/// %ProgramData% staging directory (SYSTEM-owned, like the self-updater's), not %TEMP%,
/// so a half-finished install can't leave an executable somewhere a standard user could
/// swap out before it runs.
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
        var source = cmd.Params.GetObject("source");
        if (source is null)
            return CommandResult.Fail("deploy_package requires params.source");

        var kind = source.GetString("kind") ?? "";
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

        Say($"[deploy] {packageName} ({kind})");

        string? payloadPath = null;
        try
        {
            // ---- 1. payload ----
            if (kind is "upload" or "url" or "unc")
            {
                var (path, error) = await ResolvePayloadAsync(source, kind, Say, ct);
                if (error is not null) return new CommandResult(false, log + "\n" + error);
                payloadPath = path;
            }

            // ---- 2. run ----
            var (file, args) = BuildCommandLine(cmd, source, kind, payloadPath);
            if (file is null)
                return new CommandResult(false, log + "\ndeploy_package has no install command");

            Say($"[deploy] running: {file} {args}");
            var outcome = await ProcessRunner.RunAsync(
                file, args, ct, timeoutSeconds: timeout, onLine: Emit);

            if (outcome.TimedOut)
                return new CommandResult(false, log + $"\n[deploy] FAILED: timed out after {timeout}s");

            // ---- 3. exit code ----
            if (!successCodes.Contains(outcome.ExitCode))
            {
                Say($"[deploy] FAILED: exit code {outcome.ExitCode} is not in " +
                    $"[{string.Join(", ", successCodes.OrderBy(c => c))}]");
                return new CommandResult(false, log.ToString());
            }
            Say($"[deploy] exit code {outcome.ExitCode} accepted");

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
            // On every path, including a failure: a rejected or spent installer must not
            // stay on disk waiting to be run by something else.
            if (payloadPath is not null)
            {
                try { if (File.Exists(payloadPath)) File.Delete(payloadPath); }
                catch (Exception e) { _log.LogDebug("Could not clean up {Path}: {Msg}", payloadPath, e.Message); }
            }
        }
    }

    /// <summary>Fetch (or copy) the payload and verify it. Returns (path, error).</summary>
    private async Task<(string? Path, string? Error)> ResolvePayloadAsync(
        JsonObject source, string kind, Action<string> say, CancellationToken ct)
    {
        var sha = source.GetString("sha256");
        var fileName = source.GetString("file_name");
        // Never trust a name from the params as a path component — a "file_name" of
        // ..\..\Windows\System32\x.dll would otherwise write outside the staging dir.
        var safeName = string.IsNullOrWhiteSpace(fileName)
            ? "payload.bin"
            : Path.GetFileName(fileName);
        var dest = Path.Combine(StagingDir, $"{Guid.NewGuid():N}-{safeName}");

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

    /// <summary>Substitute {file} and assemble the process to start.</summary>
    private static (string? File, string Args) BuildCommandLine(
        FleetCommand cmd, JsonObject source, string kind, string? payloadPath)
    {
        var command = cmd.Params.GetString("install_command") ?? "";
        var args = cmd.Params.GetString("install_args") ?? "";

        if (kind == "winget")
        {
            // The agent builds winget's command line itself; the hub refuses to store one
            // for a winget package, so any extra switches are appended rather than
            // replacing ours. Mirrors InstallAppExecutor's flags for consistency.
            var id = source.GetString("id");
            if (string.IsNullOrWhiteSpace(id)) return (null, "");
            var wingetArgs =
                $"install --id {id} --silent --accept-package-agreements --accept-source-agreements";
            if (!string.IsNullOrWhiteSpace(args)) wingetArgs += " " + args;
            return ("winget.exe", wingetArgs);
        }

        if (payloadPath is not null)
        {
            command = command.Replace("{file}", payloadPath, StringComparison.Ordinal);
            args = args.Replace("{file}", payloadPath, StringComparison.Ordinal);
        }
        return (string.IsNullOrWhiteSpace(command) ? null : command, args);
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
