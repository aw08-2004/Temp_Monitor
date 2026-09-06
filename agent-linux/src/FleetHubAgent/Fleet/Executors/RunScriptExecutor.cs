using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Fleet.Executors;

/// <summary>
/// run_script: execute an operator-supplied script as root and return what it said.
///
/// **The hub's `shell` parameter is Windows-shaped, and this is where that shows.** Its enum
/// is ("powershell", "cmd") -- see hub/fleet.py COMMAND_PARAMS -- because until now every
/// machine that could receive a run_script was a Windows machine. A Linux box has neither.
///
/// Refusing the command would be the literal reading and it would make run_script permanently
/// unusable here, since the console offers no third choice and defaults to powershell. So a
/// Windows shell name is treated as "the operator took the default" and the script runs under
/// this machine's shell -- and **the result says so in its first line**, because the one thing
/// that must not happen is an operator believing PowerShell ran. A genuine PowerShell script
/// executed by sh fails on its first cmdlet, which is noisy and harmless; an operator who
/// never learns which shell ran is the actual hazard.
///
/// `sh` and `bash` are accepted too, so the day the hub's enum grows them nothing here needs
/// to change. Adding them is the follow-up this note exists to name (ROADMAP.MD #22).
///
/// The script goes to the shell on STDIN rather than as `-c &lt;script&gt;`. An argv-passed
/// script is bounded by ARG_MAX and mangled by anything that re-parses the command line;
/// stdin has neither problem, and it is how the shell is meant to be fed a program.
/// </summary>
public sealed class RunScriptExecutor : ICommandExecutor
{
    private readonly ILogger<RunScriptExecutor> _log;
    public RunScriptExecutor(ILogger<RunScriptExecutor> log) => _log = log;

    public string Type => "run_script";

    /// <summary>Shell names the hub can send today, none of which exist on this machine.
    /// Named rather than inlined so the mapping is greppable from the hub side.</summary>
    private static readonly string[] WindowsShells = { "powershell", "pwsh", "cmd" };

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var script = cmd.Params.GetString("script");
        if (string.IsNullOrWhiteSpace(script))
            return CommandResult.Fail("run_script requires params.script");

        // Same clamp the Windows executor applies, and the same bounds the hub advertises
        // while an operator is typing (1s to 24h).
        var timeout = Math.Clamp(
            cmd.Params.GetInt("timeout_seconds", AgentConfig.DefaultCommandTimeoutSeconds),
            1, 86400);

        var requested = (cmd.Params.GetString("shell") ?? "").Trim().ToLowerInvariant();
        var (shell, substituted) = ResolveShell(requested);

        _log.LogInformation("run_script via {Shell} (requested '{Requested}'), timeout {Timeout}s",
            shell, requested, timeout);

        var run = await ProcessRunner.RunStdinAsync(shell, script, timeout, onOutput, ct);

        var header = substituted
            ? $"[fleethub] '{requested}' is not available on Linux; ran under {shell} instead.\n"
            : "";

        if (run.TimedOut)
            return CommandResult.Fail(header + run.Output);

        return run.ExitCode == 0
            ? CommandResult.Ok(header + run.Output)
            : CommandResult.Fail(header + $"exited {run.ExitCode}\n{run.Output}");
    }

    /// <summary>Which interpreter to run, and whether we substituted one for a shell the
    /// operator named that does not exist here. Internal so a test can pin the substitution
    /// rule -- getting this wrong is how an operator would be misled about what ran.</summary>
    internal static (string Shell, bool Substituted) ResolveShell(string requested)
    {
        if (requested is "sh") return ("/bin/sh", false);
        if (requested is "bash") return (BashOrSh(), false);
        // Empty means the hub sent no shell at all, which is not a substitution -- there was
        // nothing to substitute FOR.
        if (requested.Length == 0) return (BashOrSh(), false);
        if (Array.IndexOf(WindowsShells, requested) >= 0) return (BashOrSh(), true);
        // Anything else is a name we do not recognise. Treated like the Windows case rather
        // than executed: running whatever string the hub sent as an interpreter path would
        // turn a typo in an enum into arbitrary program selection.
        return (BashOrSh(), true);
    }

    /// <summary>bash where it exists, /bin/sh otherwise.
    ///
    /// bash is preferred because an operator writing a "shell script" for a fleet console is
    /// writing bash -- arrays, [[ ]], pipefail -- and a container image with only dash would
    /// otherwise fail those with a syntax error rather than a useful message. /bin/sh always
    /// exists, so this never resolves to nothing.</summary>
    private static string BashOrSh()
    {
        if (File.Exists("/bin/bash")) return "/bin/bash";
        if (File.Exists("/usr/bin/bash")) return "/usr/bin/bash";
        return "/bin/sh";
    }
}
