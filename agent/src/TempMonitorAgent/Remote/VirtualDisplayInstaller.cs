using System.IO.Compression;
using System.Runtime.InteropServices;
using System.Security.Cryptography.Pkcs;
using System.Security.Cryptography.X509Certificates;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Installs, configures and removes the bundled IddCx virtual display adapter, so a machine with
/// no monitor has something to capture.
///
/// **Why a driver at all.** DXGI Desktop Duplication duplicates a display *output*. A headless
/// machine has none, so there is nothing to duplicate, and the GDI fallback dutifully blits a
/// blank screen. No amount of capture-side cleverness fixes that: the pixels genuinely do not
/// exist. An indirect display driver creates a real (if virtual) output that Windows composites
/// the desktop -- and the logon screen -- onto.
///
/// **Why this is safe to do from the agent.** IddCx drivers are UMDF2 <b>user-mode</b> drivers.
/// The kernel-mode signing regime (DSE, HVCI, Secure Boot attestation) does not apply to them;
/// what PnP enforces is INF/catalog signature validation, satisfied by importing the catalog's
/// signer into TrustedPublisher. No test-signing, no Secure Boot changes.
///
/// **What we are nonetheless doing.** Step <see cref="InstallAsync"/> (5)-(6) makes this machine
/// trust a new publisher. That is a real expansion of trust and it is logged in full -- subject,
/// thumbprint, which store -- into both the command output and the agent log, and audited at
/// security level by the hub. It is deliberately an on-demand, per-machine action rather than
/// anything that happens fleet-wide or automatically.
///
/// Every step is bounded and reversible: a failure past the DriverStore stage unwinds what it
/// did, and an install record is persisted so uninstall still works after an agent self-update.
/// </summary>
public sealed class VirtualDisplayInstaller
{
    /// <summary>Where the driver's own settings file must live -- a fixed path baked into the
    /// driver, not our choice.</summary>
    internal const string SettingsPath = @"C:\VirtualDisplayDriver\vdd_settings.xml";

    /// <summary>Minimum OS build (Windows 10 1903) the driver supports.</summary>
    private const int MinimumBuild = 18362;

    private static string StagingDir => Path.Combine(AgentConfig.ProgramDataDir, "drivers");
    private static string RecordPath => Path.Combine(AgentConfig.ProgramDataDir, "virtualdisplay.json");

    private readonly ILogger _log;
    private readonly IPackageDownloader _downloader;

    public VirtualDisplayInstaller(ILogger log, IPackageDownloader downloader)
    {
        _log = log;
        _downloader = downloader;
    }

    public readonly record struct Outcome(bool Ok, string Message, bool RebootRequired)
    {
        public static Outcome Fail(string message) => new(false, message, false);
        public static Outcome Success(string message, bool reboot = false) => new(true, message, reboot);
    }

    // ------------------------------------------------------------------ detect
    /// <summary>Is the virtual display installed, and has its devnode started?</summary>
    public static (bool present, bool started) Detect() => DisplayProbe.DetectVirtualDisplay();

    // ------------------------------------------------------------------ install
    /// <summary>
    /// Install the driver from a hub-held, hash-pinned payload. Idempotent: an install onto a
    /// machine that already has it reports success without touching anything.
    /// </summary>
    public async Task<Outcome> InstallAsync(
        string payloadUrl, string payloadSha256, string version, VddSettings settings,
        Action<string>? onOutput, CancellationToken ct)
    {
        void Say(string message)
        {
            _log.LogInformation("virtual display: {Message}", message);
            onOutput?.Invoke(message);
        }

        // 1. Already installed?
        var (present, started) = Detect();
        if (present)
        {
            Say($"virtual display already installed (devnode {(started ? "started" : "NOT started")})");
            // Still apply the requested modes -- the operator may be here to change them.
            var applied = ApplySettings(settings, onOutput);
            return applied.Ok
                ? Outcome.Success("already installed; settings applied", applied.RebootRequired)
                : applied;
        }

        // 2. Preflight.
        if (Environment.OSVersion.Version.Build < MinimumBuild)
            return Outcome.Fail(
                $"needs Windows 10 1903 or newer (build {MinimumBuild}); this machine is build " +
                $"{Environment.OSVersion.Version.Build}");
        if (RuntimeInformation.ProcessArchitecture == Architecture.Arm64 && !settings.AllowArm64)
            return Outcome.Fail(
                "ARM64 is refused by default: the driver may require test-signing there. " +
                "Re-issue with allow_arm64 if you have verified it on this hardware.");
        if (!File.Exists(Path.Combine(Environment.SystemDirectory, "vcruntime140.dll")))
            Say("WARNING: vcruntime140.dll not found in System32; the driver needs the Microsoft " +
                "Visual C++ runtime and may fail to start without it.");

        string staged = Path.Combine(StagingDir, $"{Guid.NewGuid():N}-vdd.zip");
        string extracted = staged + ".d";
        string? publishedInf = null;
        bool certImported = false;
        string certStore = "";

        try
        {
            // 3. Download + verify the digest.
            Directory.CreateDirectory(StagingDir);
            Say($"downloading driver payload ({payloadSha256[..Math.Min(12, payloadSha256.Length)]}…)");
            var downloadError = await _downloader.DownloadPackageAsync(
                payloadUrl, staged, payloadSha256, ct);
            if (downloadError is not null)
                return Outcome.Fail($"payload download failed: {downloadError}");

            // 4. Extract, refusing any entry that would escape the destination.
            Directory.CreateDirectory(extracted);
            var (infPath, catPath, extractError) = ExtractAndLocate(staged, extracted);
            if (extractError is not null) return Outcome.Fail(extractError);

            // 5. Inspect the catalog signature BEFORE trusting it, and say exactly what we are
            //    about to trust. This is the step that deserves the scrutiny.
            X509Certificate2Collection certificates;
            try
            {
                // A .cat is a PKCS#7 SignedData envelope, so its signer chain comes out of
                // SignedCms rather than a certificate loader.
                var cms = new SignedCms();
                cms.Decode(File.ReadAllBytes(catPath!));
                certificates = cms.Certificates;
            }
            catch (Exception e)
            {
                return Outcome.Fail($"could not read the driver catalog's signature: {e.Message}");
            }
            if (certificates.Count == 0)
                return Outcome.Fail("the driver catalog carries no signature; refusing to install");

            foreach (var cert in certificates)
                Say($"catalog signer: subject={cert.Subject} thumbprint={cert.Thumbprint} " +
                    $"expires={cert.NotAfter:yyyy-MM-dd}");

            bool chainOk = BuildsToATrustedRoot(certificates[0]);
            certStore = chainOk ? "TrustedPublisher" : "TrustedPublisher + Root";
            Say(chainOk
                ? "signer chains to an already-trusted root; importing into TrustedPublisher only"
                : "signer does NOT chain to a trusted root; importing into Root as well");

            // 6. Import.
            ImportCertificates(certificates, alsoRoot: !chainOk);
            certImported = true;

            // 7. Stage into the DriverStore. No /install: MttVDD is root-enumerated, so there is
            //    no hardware for PnP to match it against -- the devnode has to be created by hand.
            Say("adding the driver to the DriverStore");
            var add = RunTool("pnputil.exe", $"/add-driver \"{infPath}\"", ct);
            if (add.exitCode != 0)
                return Outcome.Fail($"pnputil /add-driver failed ({add.exitCode}): {add.output}");
            publishedInf = ParsePublishedName(add.output);
            Say($"published as {publishedInf ?? "(name not reported)"}");

            // 8. Write the settings BEFORE the device starts, so its first enumeration already
            //    has the modes the operator asked for.
            WriteSettings(settings, Say);

            // 9. Create the root devnode.
            Say("creating the virtual display device node");
            var (created, rebootRequired, createError) =
                RootDevice.Create(DisplayProbe.VirtualDisplayHardwareId, infPath!);
            if (!created)
            {
                Rollback(publishedInf, certImported, certificates, chainOk, Say);
                return Outcome.Fail($"could not create the device node: {createError}");
            }

            // 10. Wait for it to actually start. "Installed but not started" is a different and
            //     much more confusing situation than "not installed", so we resolve it here.
            bool startedOk = await WaitForStart(TimeSpan.FromSeconds(30), ct);
            var probe = DisplayProbe.ProbeFromService();

            Persist(new InstallRecord
            {
                Version = version,
                PublishedInf = publishedInf ?? "",
                CertThumbprints = certificates.Select(c => c.Thumbprint).ToList(),
                CertStore = certStore,
                ImportedRoot = !chainOk,
                InstalledAt = DateTimeOffset.UtcNow,
            });

            string summary =
                $"virtual display {version} installed. devnode {(startedOk ? "started" : "NOT started")}; " +
                $"physical monitors={probe.PhysicalMonitors}; " +
                $"driver={publishedInf}; trusted into {certStore}" +
                (rebootRequired ? "; A REBOOT IS REQUIRED" : "");
            Say(summary);
            if (!startedOk && !rebootRequired)
                Say("The device did not start within 30s. Check Device Manager for a yellow bang; " +
                    "the usual cause is a missing Visual C++ runtime.");
            return Outcome.Success(summary, rebootRequired);
        }
        catch (OperationCanceledException)
        {
            return Outcome.Fail("cancelled");
        }
        catch (Exception e)
        {
            _log.LogError(e, "virtual display install failed");
            return Outcome.Fail($"install failed: {e.Message}");
        }
        finally
        {
            TryDelete(staged);
            TryDeleteDirectory(extracted);
        }
    }

    // ------------------------------------------------------------------ uninstall
    /// <summary>
    /// Remove the virtual display. Works from the persisted install record, which is why that
    /// record is written -- the published <c>oem##.inf</c> name is reported once, at install
    /// time, and cannot be recovered afterwards.
    /// </summary>
    public Outcome Uninstall(Action<string>? onOutput, CancellationToken ct = default)
    {
        void Say(string message)
        {
            _log.LogInformation("virtual display: {Message}", message);
            onOutput?.Invoke(message);
        }

        var (present, _) = Detect();
        var record = LoadRecord();
        if (!present && record is null)
            return Outcome.Success("virtual display is not installed; nothing to do");

        bool rebootRequired = false;
        if (present)
        {
            Say("removing the virtual display device node");
            var (removed, reboot, error) = RootDevice.Remove(DisplayProbe.VirtualDisplayHardwareId);
            rebootRequired |= reboot;
            if (!removed) Say($"device node removal reported: {error}");
        }

        if (record?.PublishedInf is { Length: > 0 } inf)
        {
            Say($"deleting the driver package {inf} from the DriverStore");
            var del = RunTool("pnputil.exe", $"/delete-driver {inf} /uninstall /force", ct);
            if (del.exitCode != 0) Say($"pnputil /delete-driver reported {del.exitCode}: {del.output}");
        }
        else
        {
            Say("no install record, so the DriverStore package name is unknown; " +
                "the driver package is left staged (harmless, but visible in pnputil /enum-drivers)");
        }

        // Only remove certificates we ourselves imported -- a thumbprint that was already
        // trusted before we arrived is not ours to revoke.
        if (record is not null) RemoveCertificates(record, Say);

        TryDeleteDirectory(Path.GetDirectoryName(SettingsPath)!);
        TryDelete(RecordPath);

        string summary = "virtual display removed" + (rebootRequired ? "; A REBOOT IS REQUIRED" : "");
        Say(summary);
        return Outcome.Success(summary, rebootRequired);
    }

    // ------------------------------------------------------------------ settings
    /// <summary>Rewrite the driver's settings file and restart its devnode so new modes take
    /// effect without a reboot.</summary>
    public Outcome ApplySettings(VddSettings settings, Action<string>? onOutput)
    {
        void Say(string message)
        {
            _log.LogInformation("virtual display: {Message}", message);
            onOutput?.Invoke(message);
        }

        var (present, _) = Detect();
        if (!present) return Outcome.Fail("virtual display is not installed on this machine");

        try { WriteSettings(settings, Say); }
        catch (Exception e) { return Outcome.Fail($"could not write the settings file: {e.Message}"); }

        var (restarted, error) = RootDevice.Restart(DisplayProbe.VirtualDisplayHardwareId);
        if (!restarted)
            return Outcome.Success(
                $"settings written, but the device could not be restarted ({error}); " +
                "they will apply after the next reboot");

        string summary = settings.MonitorCount == 0
            ? "virtual display stood down (0 monitors) -- the driver stays installed"
            : $"virtual display set to {settings.MonitorCount} monitor(s) at " +
              string.Join(", ", settings.Modes.Select(m => $"{m.Width}x{m.Height}@{m.Hz}"));
        Say(summary);
        return Outcome.Success(summary);
    }

    private static void WriteSettings(VddSettings settings, Action<string> say)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(SettingsPath)!);
        File.WriteAllText(SettingsPath, settings.ToXml(), new UTF8Encoding(false));
        say($"wrote {SettingsPath} ({settings.MonitorCount} monitor(s), " +
            $"{settings.Modes.Count} mode(s))");
    }

    // ------------------------------------------------------------------ helpers
    /// <summary>Extract the payload, refusing any entry whose path would escape the destination,
    /// and locate the INF + catalog.</summary>
    private static (string? inf, string? cat, string? error) ExtractAndLocate(
        string zipPath, string destination)
    {
        string root = Path.GetFullPath(destination);
        try
        {
            using var archive = ZipFile.OpenRead(zipPath);
            foreach (var entry in archive.Entries)
            {
                if (entry.FullName.EndsWith('/') || entry.FullName.EndsWith('\\')) continue;
                string target = Path.GetFullPath(Path.Combine(root, entry.FullName));
                // Zip-slip: an entry named ..\..\Windows\System32\... must never be written.
                if (!target.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
                    return (null, null, $"driver payload contains an unsafe path: {entry.FullName}");
                Directory.CreateDirectory(Path.GetDirectoryName(target)!);
                entry.ExtractToFile(target, overwrite: true);
            }
        }
        catch (Exception e)
        {
            return (null, null, $"could not extract the driver payload: {e.Message}");
        }

        string? inf = Directory.EnumerateFiles(root, "*.inf", SearchOption.AllDirectories)
            .FirstOrDefault(p => Path.GetFileName(p).StartsWith("MttVDD", StringComparison.OrdinalIgnoreCase));
        if (inf is null)
            return (null, null, "driver payload does not contain MttVDD.inf");

        string? cat = Directory.EnumerateFiles(Path.GetDirectoryName(inf)!, "*.cat").FirstOrDefault();
        if (cat is null)
            return (null, null, "driver payload does not contain a signature catalog (.cat)");

        return (inf, cat, null);
    }

    private static bool BuildsToATrustedRoot(X509Certificate2 certificate)
    {
        try
        {
            using var chain = new X509Chain();
            chain.ChainPolicy.RevocationMode = X509RevocationMode.Online;
            chain.ChainPolicy.RevocationFlag = X509RevocationFlag.ExcludeRoot;
            // The signing certificate is expected to have expired relative to now (code-signing
            // certificates are timestamped), so an expiry-only failure is not a trust failure.
            chain.ChainPolicy.VerificationFlags = X509VerificationFlags.IgnoreNotTimeValid;
            return chain.Build(certificate);
        }
        catch { return false; }
    }

    private static void ImportCertificates(X509Certificate2Collection certificates, bool alsoRoot)
    {
        AddTo(StoreName.TrustedPublisher, certificates);
        if (alsoRoot) AddTo(StoreName.Root, certificates);

        static void AddTo(StoreName name, X509Certificate2Collection certificates)
        {
            using var store = new X509Store(name, StoreLocation.LocalMachine);
            store.Open(OpenFlags.ReadWrite);
            foreach (var certificate in certificates) store.Add(certificate);
        }
    }

    private static void RemoveCertificates(InstallRecord record, Action<string> say)
    {
        foreach (var name in record.ImportedRoot
                     ? new[] { StoreName.TrustedPublisher, StoreName.Root }
                     : new[] { StoreName.TrustedPublisher })
        {
            try
            {
                using var store = new X509Store(name, StoreLocation.LocalMachine);
                store.Open(OpenFlags.ReadWrite);
                foreach (var thumbprint in record.CertThumbprints)
                {
                    var found = store.Certificates.Find(
                        X509FindType.FindByThumbprint, thumbprint, validOnly: false);
                    foreach (var certificate in found) store.Remove(certificate);
                    if (found.Count > 0) say($"removed {thumbprint} from {name}");
                }
            }
            catch (Exception e)
            {
                say($"could not clean up certificates in {name}: {e.Message}");
            }
        }
    }

    /// <summary>Undo an install that failed after it started changing the machine.</summary>
    private void Rollback(string? publishedInf, bool certImported,
                          X509Certificate2Collection certificates, bool chainOk, Action<string> say)
    {
        say("rolling back the partial install");
        if (publishedInf is { Length: > 0 })
            RunTool("pnputil.exe", $"/delete-driver {publishedInf} /uninstall /force", default);
        if (certImported)
            RemoveCertificates(new InstallRecord
            {
                CertThumbprints = certificates.Select(c => c.Thumbprint).ToList(),
                ImportedRoot = !chainOk,
            }, say);
    }

    private static async Task<bool> WaitForStart(TimeSpan timeout, CancellationToken ct)
    {
        var deadline = DateTimeOffset.UtcNow + timeout;
        while (DateTimeOffset.UtcNow < deadline)
        {
            if (Detect().started) return true;
            try { await Task.Delay(1000, ct); } catch (OperationCanceledException) { return false; }
        }
        return Detect().started;
    }

    /// <summary>
    /// pnputil reports the DriverStore name as "Published Name: oem12.inf". That name is needed
    /// for uninstall and is not recoverable afterwards, so it is parsed here and persisted.
    ///
    /// Matched by the VALUE's shape rather than the label's text, deliberately: pnputil is
    /// localised, so "Published Name" is only "Published Name" on an English machine, and a
    /// fleet is not all one locale. <c>oemNN.inf</c> is the DriverStore's own naming and is not
    /// localised -- and being that specific also avoids matching the source INF path pnputil
    /// echoes back on the line above.
    /// </summary>
    internal static string? ParsePublishedName(string pnputilOutput)
    {
        var match = System.Text.RegularExpressions.Regex.Match(
            pnputilOutput ?? "", @"\boem\d+\.inf\b",
            System.Text.RegularExpressions.RegexOptions.IgnoreCase);
        return match.Success ? match.Value.ToLowerInvariant() : null;
    }

    private static (int exitCode, string output) RunTool(string exe, string arguments, CancellationToken ct)
    {
        try
        {
            using var process = new System.Diagnostics.Process
            {
                StartInfo = new System.Diagnostics.ProcessStartInfo(exe, arguments)
                {
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                },
            };
            process.Start();
            string output = process.StandardOutput.ReadToEnd() + process.StandardError.ReadToEnd();
            if (!process.WaitForExit(120_000))
            {
                try { process.Kill(true); } catch { }
                return (-1, output + "\n(timed out after 120s)");
            }
            return (process.ExitCode, output.Trim());
        }
        catch (Exception e)
        {
            return (-1, e.Message);
        }
    }

    // ------------------------------------------------------------------ install record
    internal sealed class InstallRecord
    {
        [JsonPropertyName("version")] public string Version { get; set; } = "";
        /// <summary>The DriverStore's <c>oem##.inf</c> name. Reported once by pnputil at install
        /// time and unrecoverable afterwards, which is the whole reason this file exists.</summary>
        [JsonPropertyName("published_inf")] public string PublishedInf { get; set; } = "";
        [JsonPropertyName("cert_thumbprints")] public List<string> CertThumbprints { get; set; } = new();
        [JsonPropertyName("cert_store")] public string CertStore { get; set; } = "";
        [JsonPropertyName("imported_root")] public bool ImportedRoot { get; set; }
        [JsonPropertyName("installed_at")] public DateTimeOffset InstalledAt { get; set; }
    }

    internal static InstallRecord? LoadRecord()
    {
        try
        {
            return File.Exists(RecordPath)
                ? JsonSerializer.Deserialize<InstallRecord>(File.ReadAllText(RecordPath))
                : null;
        }
        catch { return null; }
    }

    private static void Persist(InstallRecord record)
    {
        try
        {
            Directory.CreateDirectory(AgentConfig.ProgramDataDir);
            File.WriteAllText(RecordPath, JsonSerializer.Serialize(record));
        }
        catch { /* uninstall degrades to "package name unknown", which it reports */ }
    }

    private static void TryDelete(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); } catch { }
    }

    private static void TryDeleteDirectory(string path)
    {
        try { if (Directory.Exists(path)) Directory.Delete(path, recursive: true); } catch { }
    }
}

/// <summary>The driver's own configuration: how many virtual monitors, and at what modes.</summary>
public sealed record VddSettings(
    int MonitorCount,
    IReadOnlyList<VddMode> Modes,
    bool AllowArm64 = false)
{
    /// <summary>A single 1080p60 virtual monitor -- the right default for "I just want to see
    /// the logon screen on a headless server".</summary>
    public static VddSettings Default { get; } = new(1, new[] { new VddMode(1920, 1080, 60) });

    public string ToXml()
    {
        var sb = new StringBuilder();
        sb.AppendLine("<?xml version=\"1.0\" encoding=\"UTF-8\"?>");
        sb.AppendLine("<vdd_settings>");
        sb.AppendLine("  <monitors>");
        sb.AppendLine($"    <count>{Math.Clamp(MonitorCount, 0, 8)}</count>");
        sb.AppendLine("  </monitors>");
        sb.AppendLine("  <resolutions>");
        foreach (var mode in Modes.Take(32))
        {
            sb.AppendLine("    <resolution>");
            sb.AppendLine($"      <width>{mode.Width}</width>");
            sb.AppendLine($"      <height>{mode.Height}</height>");
            sb.AppendLine($"      <refresh_rate>{mode.Hz}</refresh_rate>");
            sb.AppendLine("    </resolution>");
        }
        sb.AppendLine("  </resolutions>");
        sb.AppendLine("</vdd_settings>");
        return sb.ToString();
    }
}

public readonly record struct VddMode(int Width, int Height, int Hz);
