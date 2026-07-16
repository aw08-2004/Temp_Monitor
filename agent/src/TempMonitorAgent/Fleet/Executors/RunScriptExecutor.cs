namespace TempMonitorAgent.Fleet.Executors;

/// <summary>
/// run_script (HIGH-RISK, signature-gated by the dispatcher): write params.script to a
/// temp file and run it. params.shell selects "powershell" (default) or "cmd".
/// Because it runs arbitrary code as SYSTEM, it only ever reaches here after a valid
/// offline Ed25519 signature has been verified.
/// </summary>
public sealed class RunScriptExecutor : ICommandExecutor
{
    public string Type => "run_script";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, CancellationToken ct)
    {
        var script = cmd.Params.GetString("script");
        if (string.IsNullOrEmpty(script))
            return CommandResult.Fail("run_script requires params.script");

        var shell = (cmd.Params.GetString("shell") ?? "powershell").ToLowerInvariant();
        var isCmd = shell is "cmd" or "batch" or "bat";
        var ext = isCmd ? ".cmd" : ".ps1";
        var scriptPath = Path.Combine(Path.GetTempPath(), $"tmagent_{Guid.NewGuid():N}{ext}");

        try
        {
            await File.WriteAllTextAsync(scriptPath, script, ct);

            ProcessOutcome outcome = isCmd
                ? await ProcessRunner.RunAsync("cmd.exe", $"/c \"{scriptPath}\"", ct, timeoutSeconds: 600)
                : await ProcessRunner.RunAsync(
                    "powershell.exe",
                    $"-NoProfile -NonInteractive -ExecutionPolicy Bypass -File \"{scriptPath}\"",
                    ct, timeoutSeconds: 600);

            var summary = $"exit={outcome.ExitCode}{(outcome.TimedOut ? " (timed out)" : "")}\n{outcome.Output}";
            return outcome.ExitCode == 0 && !outcome.TimedOut
                ? CommandResult.Ok(summary)
                : CommandResult.Fail(summary);
        }
        catch (Exception e)
        {
            return CommandResult.Fail($"run_script error: {e.Message}");
        }
        finally
        {
            try { File.Delete(scriptPath); } catch { /* ignore */ }
        }
    }
}
