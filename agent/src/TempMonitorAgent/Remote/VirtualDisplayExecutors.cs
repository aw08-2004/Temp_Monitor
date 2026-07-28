using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Remote;

/// <summary>
/// The three virtual-display commands (roadmap #2, headless capture). Grouped in one file
/// because they are one feature sharing one installer, following the ShellControlExecutors
/// precedent.
///
/// All three are gated at the hub behind the remote_control capability and machine scope, and
/// install/uninstall are audited at security level -- installing puts a third-party driver into
/// the DriverStore and its publisher into this machine's certificate store.
/// </summary>
public sealed class InstallVirtualDisplayExecutor : ICommandExecutor
{
    private readonly ILogger<InstallVirtualDisplayExecutor> _log;
    private readonly IPackageDownloader _downloader;

    public InstallVirtualDisplayExecutor(
        ILogger<InstallVirtualDisplayExecutor> log, IPackageDownloader downloader)
    {
        _log = log;
        _downloader = downloader;
    }

    public string Type => "install_virtual_display";

    public async Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var url = cmd.Params.GetString("payload_url");
        var sha = cmd.Params.GetString("payload_sha256");
        if (string.IsNullOrWhiteSpace(url) || string.IsNullOrWhiteSpace(sha))
            return CommandResult.Fail(
                "install_virtual_display requires params.payload_url and params.payload_sha256");

        var version = cmd.Params.GetString("version") ?? "unknown";
        var settings = VirtualDisplaySettingsParser.Parse(cmd.Params);

        var installer = new VirtualDisplayInstaller(_log, _downloader);
        var outcome = await installer.InstallAsync(url, sha, version, settings, onOutput, ct);

        // A fresh probe on the next heartbeat, so the machine page stops saying "no displays"
        // without waiting out the reporter's interval.
        RemoteInventoryReporter.Invalidate();

        return outcome.Ok ? CommandResult.Ok(outcome.Message) : CommandResult.Fail(outcome.Message);
    }
}

/// <summary>Removes the virtual display and cleans up what the install added.</summary>
public sealed class UninstallVirtualDisplayExecutor : ICommandExecutor
{
    private readonly ILogger<UninstallVirtualDisplayExecutor> _log;
    private readonly IPackageDownloader _downloader;

    public UninstallVirtualDisplayExecutor(
        ILogger<UninstallVirtualDisplayExecutor> log, IPackageDownloader downloader)
    {
        _log = log;
        _downloader = downloader;
    }

    public string Type => "uninstall_virtual_display";

    public Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var installer = new VirtualDisplayInstaller(_log, _downloader);
        var outcome = installer.Uninstall(onOutput, ct);
        RemoteInventoryReporter.Invalidate();
        return Task.FromResult(outcome.Ok
            ? CommandResult.Ok(outcome.Message)
            : CommandResult.Fail(outcome.Message));
    }
}

/// <summary>
/// Changes how many virtual monitors exist and at what resolutions, without a reinstall.
///
/// A count of 0 is the graceful stand-down path for a machine that has since had a real monitor
/// attached: the driver stays installed and out of the way, rather than requiring an uninstall
/// (and its reboot) to stop adding a phantom display.
/// </summary>
public sealed class SetVirtualDisplayModeExecutor : ICommandExecutor
{
    private readonly ILogger<SetVirtualDisplayModeExecutor> _log;
    private readonly IPackageDownloader _downloader;

    public SetVirtualDisplayModeExecutor(
        ILogger<SetVirtualDisplayModeExecutor> log, IPackageDownloader downloader)
    {
        _log = log;
        _downloader = downloader;
    }

    public string Type => "set_virtual_display_mode";

    public Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var installer = new VirtualDisplayInstaller(_log, _downloader);
        var outcome = installer.ApplySettings(VirtualDisplaySettingsParser.Parse(cmd.Params), onOutput);
        RemoteInventoryReporter.Invalidate();
        return Task.FromResult(outcome.Ok
            ? CommandResult.Ok(outcome.Message)
            : CommandResult.Fail(outcome.Message));
    }
}

/// <summary>
/// Re-reports the logon sessions and display outputs on the next heartbeat.
///
/// The inventory rides the heartbeat on a change-detected, self-throttled cadence, which is
/// right for the steady state but wrong for the moment an operator is looking at the session
/// picker and someone has just signed in. This gives them a refresh button.
/// </summary>
public sealed class RefreshRemoteInventoryExecutor : ICommandExecutor
{
    public string Type => "refresh_remote_inventory";

    public Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        RemoteInventoryReporter.Invalidate();
        RemoteInventoryReporter.RefreshIfDue();
        // Also return it inline, so the operator sees the answer without waiting a heartbeat.
        return Task.FromResult(CommandResult.Ok(RemoteInventoryReporter.Build().ToJsonString()));
    }
}

/// <summary>Turns command params into <see cref="VddSettings"/>. Separate and pure so the
/// parsing (which is where malformed operator input lands) is unit-testable without a driver.</summary>
internal static class VirtualDisplaySettingsParser
{
    internal static VddSettings Parse(JsonNode? paramsNode)
    {
        int monitors = paramsNode.GetInt("monitors", VddSettings.Default.MonitorCount);
        bool allowArm64 = paramsNode is JsonObject o
                          && o.TryGetPropertyValue("allow_arm64", out var arm)
                          && arm?.GetValue<bool>() == true;

        var modes = ParseModes(paramsNode);
        if (modes.Count == 0) modes = VddSettings.Default.Modes.ToList();

        return new VddSettings(Math.Clamp(monitors, 0, 8), modes, allowArm64);
    }

    /// <summary>Reads <c>resolutions: [{width, height, hz}, ...]</c>. Anything unparseable is
    /// skipped rather than failing the command -- a single bad entry should not stop an operator
    /// from getting a usable display.</summary>
    private static List<VddMode> ParseModes(JsonNode? paramsNode)
    {
        var modes = new List<VddMode>();
        if (paramsNode is not JsonObject obj
            || !obj.TryGetPropertyValue("resolutions", out var node)
            || node is not JsonArray array)
            return modes;

        foreach (var entry in array)
        {
            if (entry is not JsonObject mode) continue;
            int width = mode.GetInt("width", 0);
            int height = mode.GetInt("height", 0);
            int hz = mode.GetInt("hz", 60);
            if (width < 640 || height < 480 || width > 7680 || height > 4320) continue;
            modes.Add(new VddMode(width, height, Math.Clamp(hz, 24, 240)));
        }
        return modes;
    }
}
