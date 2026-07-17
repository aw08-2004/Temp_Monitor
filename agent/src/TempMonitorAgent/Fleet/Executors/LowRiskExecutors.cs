using System.Management;
using Microsoft.Extensions.Logging;

namespace TempMonitorAgent.Fleet.Executors;

// restart/shutdown/rename take onOutput but ignore it: they return in well under the
// console's poll interval, so there is no progress to narrate.

/// <summary>restart: reboot the machine after an optional delay (default 60s).</summary>
public sealed class RestartExecutor : ICommandExecutor
{
    public string Type => "restart";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        int delay = cmd.Params.GetInt("delay_seconds", 60);
        var outcome = await ProcessRunner.RunAsync(
            "shutdown.exe", $"/r /t {delay} /c \"TempMonitor fleet restart\"", ct, timeoutSeconds: 30);
        return outcome.ExitCode == 0
            ? CommandResult.Ok($"restart scheduled in {delay}s")
            : CommandResult.Fail($"shutdown /r exited {outcome.ExitCode}: {outcome.Output}");
    }
}

/// <summary>shutdown: power off the machine after an optional delay (default 60s).</summary>
public sealed class ShutdownExecutor : ICommandExecutor
{
    public string Type => "shutdown";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        int delay = cmd.Params.GetInt("delay_seconds", 60);
        var outcome = await ProcessRunner.RunAsync(
            "shutdown.exe", $"/s /t {delay} /c \"TempMonitor fleet shutdown\"", ct, timeoutSeconds: 30);
        return outcome.ExitCode == 0
            ? CommandResult.Ok($"shutdown scheduled in {delay}s")
            : CommandResult.Fail($"shutdown /s exited {outcome.ExitCode}: {outcome.Output}");
    }
}

/// <summary>rename: change the computer name (takes effect on next reboot).</summary>
public sealed class RenameExecutor : ICommandExecutor
{
    private readonly ILogger<RenameExecutor> _log;
    public RenameExecutor(ILogger<RenameExecutor> log) => _log = log;

    public string Type => "rename";

    public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var newName = cmd.Params.GetString("new_name");
        if (string.IsNullOrWhiteSpace(newName))
            return Task.FromResult(CommandResult.Fail("rename requires params.new_name"));

        try
        {
            using var searcher = new ManagementObjectSearcher("SELECT * FROM Win32_ComputerSystem");
            foreach (ManagementObject mo in searcher.Get())
            {
                using (mo)
                {
                    var inParams = mo.GetMethodParameters("Rename");
                    inParams["Name"] = newName;
                    using var outParams = mo.InvokeMethod("Rename", inParams, null);
                    var ret = Convert.ToUInt32(outParams["ReturnValue"]);
                    return Task.FromResult(ret == 0
                        ? CommandResult.Ok($"renamed to '{newName}' (reboot required)")
                        : CommandResult.Fail($"Win32_ComputerSystem.Rename returned {ret}"));
                }
            }
            return Task.FromResult(CommandResult.Fail("Win32_ComputerSystem instance not found"));
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "rename failed");
            return Task.FromResult(CommandResult.Fail($"rename error: {e.Message}"));
        }
    }
}

/// <summary>gpupdate: force a Group Policy refresh. Streams — it can run for minutes.</summary>
public sealed class GpUpdateExecutor : ICommandExecutor
{
    public string Type => "gpupdate";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var outcome = await ProcessRunner.RunAsync(
            "gpupdate.exe", "/force", ct, timeoutSeconds: 180, onLine: onOutput);
        return outcome.ExitCode == 0
            ? CommandResult.Ok(outcome.Output)
            : CommandResult.Fail($"gpupdate exited {outcome.ExitCode}: {outcome.Output}");
    }
}

/// <summary>install_app: install via winget (params.id) or msiexec (params.msi_path).
/// Streams — a winget install is a 600s-timeout operation with real progress output.</summary>
public sealed class InstallAppExecutor : ICommandExecutor
{
    public string Type => "install_app";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var id = cmd.Params.GetString("id");
        if (!string.IsNullOrWhiteSpace(id))
        {
            var args = $"install --id {id} --silent --accept-package-agreements --accept-source-agreements";
            var outcome = await ProcessRunner.RunAsync(
                "winget.exe", args, ct, timeoutSeconds: 600, onLine: onOutput);
            return outcome.ExitCode == 0
                ? CommandResult.Ok(outcome.Output)
                : CommandResult.Fail($"winget exited {outcome.ExitCode}: {outcome.Output}");
        }

        var msi = cmd.Params.GetString("msi_path");
        if (!string.IsNullOrWhiteSpace(msi))
        {
            var outcome = await ProcessRunner.RunAsync(
                "msiexec.exe", $"/i \"{msi}\" /qn /norestart", ct, timeoutSeconds: 600, onLine: onOutput);
            return outcome.ExitCode == 0
                ? CommandResult.Ok($"msiexec ok: {outcome.Output}")
                : CommandResult.Fail($"msiexec exited {outcome.ExitCode}: {outcome.Output}");
        }

        return CommandResult.Fail("install_app requires params.id (winget) or params.msi_path");
    }
}
