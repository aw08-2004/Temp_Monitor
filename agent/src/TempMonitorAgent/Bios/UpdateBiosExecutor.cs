using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Bios;

/// <summary>
/// update_bios: flash this machine's firmware with an image the hub is holding (roadmap #9).
///
/// This replaces the stub that has answered "not implemented yet" since the command-signing
/// removal, and it is the most dangerous thing the agent does -- the only operation in the
/// product with no restore path. The shape of it is therefore refuse-early, report-honestly:
///
///   1. **Fetch the payload.** The command carries only an update id; the image URL, its
///      digest, the vendor and model list, the BIOS setup password and the power policy all
///      come from an authenticated hub endpoint. That is not a detour -- the hub audits
///      command params verbatim, so a password or a download URL carried there would sit in
///      the audit log inside the database that is itself backed up. The fetch is also a
///      conditional UPDATE hub-side, which is what makes a redelivered command safe: the
///      second fetch is refused rather than flashing twice.
///   2. **Re-check the hardware and the power**, against what this machine says about itself
///      right now. The hub checked the same things against an inventory that may be hours
///      old, and between the two a chassis can be swapped or a hostname reused.
///   3. **Download and verify** over the authenticated channel, against the digest the HUB
///      computed from the bytes it stored. A mismatch deletes the file and refuses.
///   4. **Run the manufacturer's own updater** and read its exit code, where "reboot
///      required" is the normal success.
///
/// **Nothing here reboots the machine, and nothing here reports success.** The tool exits
/// after STAGING the image; the firmware writes it during the next POST. So the report says
/// only that the flash was staged, and the hub holds the target in `rebooting` until the
/// machine comes back reporting a BIOS version -- which is the only honest evidence that it
/// worked. An executor that returned "ok" here would be answering a question it cannot see.
/// </summary>
public sealed class UpdateBiosExecutor(
    ILogger<UpdateBiosExecutor> log, FleetClient fleet, IPackageDownloader downloader)
    : ICommandExecutor
{
    public string Type => "update_bios";

    /// <summary>Beside the deploy staging directory, under %ProgramData% rather than %TEMP%,
    /// for the same ACL reason: a half-finished flash must not leave an executable somewhere a
    /// standard user could swap out before it runs.</summary>
    private static string StagingDir => Path.Combine(AgentConfig.ProgramDataDir, "firmware");

    public async Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        void Say(string line) => onOutput?.Invoke(line + Environment.NewLine);

        var updateId = cmd.Params.GetString("update_id") ?? "";
        if (updateId.Length == 0)
            return CommandResult.Fail("This command carries no firmware update id.");

        var payload = await fleet.FetchFirmwareUpdateAsync(updateId, ct);
        if (payload is null)
        {
            // The hub refuses an update that is no longer pending. Nothing has been
            // downloaded and nothing has been run, so there is nothing to report either --
            // reporting a failure here would close out a target that another delivery of the
            // same command may be legitimately flashing.
            return CommandResult.Fail(
                "The hub would not supply this firmware update. It may have been cancelled, "
                + "or already sent to this machine.");
        }

        var vendor = payload["vendor"]?.GetValue<string>() ?? "";
        var toVersion = payload["to_version"]?.GetValue<string>() ?? "";
        var models = (payload["models"] as JsonArray ?? [])
            .Select(n => n?.GetValue<string>() ?? "").Where(s => s.Length > 0).ToList();

        // ---- 2. the machine's own account of itself, now ----
        var refusal = FirmwareFlasher.CheckHardware(
            BiosReader.Manufacturer(), SystemModel(), vendor, models);
        if (refusal is null)
        {
            var power = FirmwareFlasher.ReadPower();
            refusal = FirmwareFlasher.CheckPower(
                power,
                payload["require_ac_power"]?.GetValue<bool>() ?? true,
                payload["min_battery_percent"]?.GetValue<int>() ?? 0);
        }
        if (refusal is not null)
        {
            // Reported as a refusal rather than a failure: nothing went wrong, this image
            // is not for this machine or this moment. The hub keeps the two apart so a
            // fleet of VMs and a fleet of real faults do not look the same.
            Say($"[firmware] Refused: {refusal}");
            await ReportAsync(updateId, ok: false, unsupported: true, error: refusal, ct);
            return CommandResult.Fail($"Firmware update refused: {refusal}");
        }

        // ---- 3. download and verify ----
        var url = payload["url"]?.GetValue<string>() ?? "";
        var sha = payload["sha256"]?.GetValue<string>() ?? "";
        if (url.Length == 0 || sha.Length == 0)
        {
            const string why = "the hub supplied no image URL or digest";
            await ReportAsync(updateId, ok: false, unsupported: false, error: why, ct);
            return CommandResult.Fail(why);
        }

        // Never trust a name from the payload as a path component.
        var name = payload["filename"]?.GetValue<string>() ?? "";
        var safeName = string.IsNullOrWhiteSpace(name) ? "firmware.exe" : Path.GetFileName(name);
        var imagePath = Path.Combine(StagingDir, $"{Guid.NewGuid():N}-{safeName}");
        string? passwordFile = null;

        try
        {
            Directory.CreateDirectory(StagingDir);
            Say($"[firmware] Downloading the {vendor} image for BIOS {toVersion}.");
            var downloadError = await downloader.DownloadPackageAsync(url, imagePath, sha, ct);
            if (downloadError is not null)
            {
                // A digest mismatch has already deleted the file. Refused rather than run
                // unchecked: the digest is the entire integrity story for a hub-hosted
                // payload, and this is the payload where running the wrong bytes is fatal.
                await ReportAsync(updateId, ok: false, unsupported: false,
                                  error: downloadError, ct);
                return CommandResult.Fail($"Firmware image rejected: {downloadError}");
            }
            Say("[firmware] sha256 verified.");

            // ---- 4. run the manufacturer's updater ----
            var password = payload["password"]?.GetValue<string>();
            if (!string.IsNullOrEmpty(password) && FirmwareFlasher.NeedsPasswordFile(vendor))
            {
                // Written to a SYSTEM-owned staging file rather than passed on the command
                // line, because a command line is readable from the process list by anything
                // running locally. Deleted in the finally below on every path.
                passwordFile = Path.Combine(StagingDir, $"{Guid.NewGuid():N}.pw");
                await File.WriteAllTextAsync(passwordFile, password, ct);
            }

            var plan = FirmwareFlasher.BuildPlan(vendor, imagePath,
                                                 payload["install_args"]?.GetValue<string>(),
                                                 password, passwordFile);
            // The password is never echoed, including into the command log the console shows.
            Say($"[firmware] Running the {vendor} updater.");
            var outcome = await ProcessRunner.RunAsync(
                plan.FileName, plan.Arguments, ct,
                timeoutSeconds: 30 * 60, onLine: onOutput);

            if (outcome.TimedOut)
            {
                const string why = "the manufacturer's update tool did not finish in time";
                await ReportAsync(updateId, ok: false, unsupported: false, error: why, ct);
                return CommandResult.Fail(why);
            }

            var exitProblem = FirmwareFlasher.ClassifyExit(vendor, outcome.ExitCode);
            if (exitProblem is not null)
            {
                await ReportAsync(updateId, ok: false, unsupported: false, error: exitProblem,
                                  ct);
                return CommandResult.Fail(exitProblem);
            }

            // Staged. NOT applied -- see the class docstring. The hub takes it from here.
            Say("[firmware] The image is staged. It is written during the next restart, and "
                + "the hub confirms it when this machine reports its new BIOS version.");
            var delivered = await ReportAsync(updateId, ok: true, unsupported: false,
                                              error: "", ct);
            // The inventory reporter is invalidated so the next scan re-reads the BIOS
            // version rather than serving the cached pre-flash one -- which is the very fact
            // the hub is waiting for.
            BiosInventoryReporter.Invalidate();

            var summary = $"Firmware image for BIOS {toVersion} staged. It is written during "
                          + "the next restart.";
            if (!delivered)
            {
                // Said plainly rather than swallowed: the image IS staged, and an operator
                // looking at a target stuck on "sent" needs to know the flash happened and
                // only the report was lost. The hub's sweep closes it out eventually, and
                // the machine's own version report can still confirm it before then.
                summary += " The result could not be reported to the hub.";
            }
            return CommandResult.Ok(summary);
        }
        catch (Exception e) when (e is not OperationCanceledException)
        {
            log.LogWarning(e, "Firmware update {Id} failed", updateId);
            await ReportAsync(updateId, ok: false, unsupported: false, error: e.Message,
                              CancellationToken.None);
            return CommandResult.Fail($"Firmware update failed: {e.Message}");
        }
        finally
        {
            TryDelete(passwordFile);
            TryDelete(imagePath);
        }
    }

    private void TryDelete(string? path)
    {
        if (string.IsNullOrEmpty(path)) return;
        try { if (File.Exists(path)) File.Delete(path); }
        catch (Exception e) { log.LogDebug("Could not clean up {Path}: {Msg}", path, e.Message); }
    }

    private Task<bool> ReportAsync(string updateId, bool ok, bool unsupported, string error,
                                   CancellationToken ct)
        => fleet.ReportFirmwareUpdateAsync(updateId, new JsonObject
        {
            ["ok"] = ok,
            ["unsupported"] = unsupported,
            ["error"] = error,
        }, ct);

    /// <summary>This machine's model, as WMI reports it -- the same string
    /// <c>save_machine_info</c> stores and the hub matched the image against.</summary>
    private static string SystemModel()
    {
        try
        {
            using var searcher = new System.Management.ManagementObjectSearcher(
                @"root\cimv2", "SELECT Model FROM Win32_ComputerSystem");
            using var results = searcher.Get();
            foreach (System.Management.ManagementBaseObject row in results)
            {
                using (row) return (row["Model"]?.ToString() ?? "").Trim();
            }
        }
        catch (Exception)
        {
            // Empty, which CheckHardware refuses on. An unreadable model is not a match.
        }
        return "";
    }
}
