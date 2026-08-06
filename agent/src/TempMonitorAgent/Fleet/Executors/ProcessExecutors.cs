using System.Diagnostics;
using System.ServiceProcess;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Telemetry;

namespace TempMonitorAgent.Fleet.Executors;

/// <summary>
/// What must not be ended, and why the check lives on the machine.
///
/// The hub keeps the same list so a refusal is immediate in the console, but THIS copy is
/// the authority: it is checked against the process that is actually running under the pid
/// right now, not against a name that travelled from a list rendered seconds ago.
///
/// Ending any of these does not close a program -- it takes the machine down, and for most
/// of them it does so immediately and with a bugcheck rather than a shutdown. A console that
/// offers a helpdesk a one-click blue screen will eventually deliver one.
/// </summary>
public static class ProcessGuard
{
    private static readonly HashSet<string> Protected = new(StringComparer.OrdinalIgnoreCase)
    {
        // Windows treats these as critical: ending one bugchecks the box.
        "smss", "csrss", "wininit", "winlogon", "services", "lsass", "lsaiso",
        // Kernel pseudo-processes. They cannot be ended at all; refusing is clearer than
        // an access-denied that reads like a permissions problem.
        "system", "system idle process", "idle", "registry", "secure system",
        "memory compression", "memcompression",
        // Killable, and the wrong answer every time: a service host takes every service in
        // it down with it, and for the RPC host that means the machine restarts itself in a
        // minute. What the operator wants is to restart ONE service, which restart_process
        // does properly -- so the refusal names that instead of just saying no.
        "svchost",
        // Ourselves. Ending the agent does not just lose the Processes card, it loses the
        // machine from the console until the service manager brings it back -- and this very
        // command's result would never be reported, leaving the operator watching a spinner
        // for work that did happen.
        "tempmonitoragent",
    };

    /// <summary>Lowercased, .exe stripped -- the same normalization the hub applies, so a
    /// name cannot slip past either end by spelling.</summary>
    public static string Normalize(string? name)
    {
        var text = (name ?? "").Trim();
        if (text.EndsWith(".exe", StringComparison.OrdinalIgnoreCase))
            text = text[..^4];
        return text.ToLowerInvariant();
    }

    public static bool IsProtected(string? name) => Protected.Contains(Normalize(name));

    /// <summary>Do the name the operator saw and the name the pid answers to agree?
    ///
    /// The guard against PID reuse, and the reason both travel in the command. A list is
    /// seconds old by the time somebody clicks it, Windows recycles ids aggressively, and
    /// "end pid 4812" on its own is an instruction to kill whatever happens to hold that
    /// number now.</summary>
    public static bool NameMatches(string? expected, string? actual) =>
        Normalize(expected) == Normalize(actual) && Normalize(actual).Length > 0;
}

/// <summary>
/// kill_process: end one process, or every instance of one name the operator selected.
///
/// Every pid is checked against the name that came with it before anything is killed, and a
/// mismatch ends NOTHING rather than being resolved in the operator's favour. A pid that has
/// already exited is reported as such and counts as success -- the operator asked for it to
/// be gone, and it is.
/// </summary>
public sealed class KillProcessExecutor : ICommandExecutor
{
    private readonly ILogger<KillProcessExecutor> _log;
    public KillProcessExecutor(ILogger<KillProcessExecutor> log) => _log = log;

    public string Type => "kill_process";

    /// <summary>How long to wait for a killed process to actually go. TerminateProcess is
    /// asynchronous, and a process with pending I/O can outlive its own kill by a moment;
    /// reporting "ended" for something still on screen is worse than waiting.</summary>
    private const int ExitWaitMillis = 5000;

    public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                            CancellationToken ct)
    {
        var expected = cmd.Params.GetString("name");
        if (string.IsNullOrWhiteSpace(expected))
            return Task.FromResult(CommandResult.Fail("kill_process requires params.name"));

        var pids = ReadPids(cmd.Params);
        if (pids.Count == 0)
            return Task.FromResult(CommandResult.Fail("kill_process requires params.pids"));

        bool tree = cmd.Params.GetBool("tree");
        var ended = new List<int>();
        var gone = new List<int>();
        var refused = new List<string>();

        foreach (var pid in pids)
        {
            Process process;
            try { process = Process.GetProcessById(pid); }
            catch (ArgumentException) { gone.Add(pid); continue; }
            catch (Exception e)
            {
                refused.Add($"{pid} ({e.Message})");
                continue;
            }

            using (process)
            {
                string actual;
                try { actual = process.ProcessName ?? ""; }
                catch { gone.Add(pid); continue; }

                if (!ProcessGuard.NameMatches(expected, actual))
                {
                    // The pid was recycled between the snapshot and the click. Say what it is
                    // now, so the operator can see that the list moved rather than wondering
                    // why nothing happened.
                    refused.Add($"{pid} is now {actual}, not {expected}");
                    continue;
                }
                if (ProcessGuard.IsProtected(actual))
                {
                    refused.Add($"{actual} ({pid}) is a critical Windows process");
                    continue;
                }

                try
                {
                    process.Kill(entireProcessTree: tree);
                    process.WaitForExit(ExitWaitMillis);
                    if (process.HasExited) ended.Add(pid);
                    else refused.Add($"{actual} ({pid}) did not exit");
                }
                catch (Exception e)
                {
                    _log.LogWarning(e, "Could not end {Name} ({Pid})", actual, pid);
                    refused.Add($"{actual} ({pid}): {e.Message}");
                }
            }
        }

        var parts = new List<string>();
        if (ended.Count > 0) parts.Add($"ended {expected} ({string.Join(", ", ended)})");
        if (gone.Count > 0) parts.Add($"already gone ({string.Join(", ", gone)})");
        if (refused.Count > 0) parts.Add($"refused: {string.Join("; ", refused)}");
        var summary = parts.Count > 0 ? string.Join(". ", parts) : "nothing to do";

        // A refusal is a failure even when something else succeeded: "ended 2 of 3" must not
        // come back green, or an operator ending a hung app across a machine would believe a
        // partial result was the whole one.
        return Task.FromResult(refused.Count == 0
            ? CommandResult.Ok(summary)
            : CommandResult.Fail(summary));
    }

    private static List<int> ReadPids(JsonNode? parameters)
    {
        var pids = new List<int>();
        if (parameters?["pids"] is not JsonArray array) return pids;
        foreach (var node in array)
        {
            if (node is null) continue;
            try
            {
                var pid = node.GetValue<int>();
                if (pid > 0 && !pids.Contains(pid)) pids.Add(pid);
            }
            catch { /* not a number -- the hub validates, so this is belt and braces */ }
        }
        return pids;
    }
}

/// <summary>
/// restart_process: end one process and start it again where it was.
///
/// What "again where it was" means is decided HERE, because only this machine knows:
///
///   * A process hosting exactly one Windows service is restarted AS that service --
///     stop, wait, start, wait -- taking its running dependents down and bringing them
///     back with it. Killing a service's process instead would leave the SCM believing it
///     crashed, and for a service set to no recovery action that means it simply stays down.
///   * A process hosting SEVERAL services is refused, with them named. There is no honest
///     way to guess which of them the operator meant, and restarting all of them because
///     they happen to share a host is not what anybody clicked for.
///   * Anything else is relaunched from its own image, in the Windows session it was
///     running in, as the user who was running it (see UserSessionLauncher). Launching a
///     user's application as SYSTEM instead would produce a program with no profile, no
///     mapped drives and no access to the documents it exists to open.
/// </summary>
public sealed class RestartProcessExecutor : ICommandExecutor
{
    private readonly ILogger<RestartProcessExecutor> _log;
    public RestartProcessExecutor(ILogger<RestartProcessExecutor> log) => _log = log;

    public string Type => "restart_process";

    private const int ExitWaitMillis = 10_000;
    private static readonly TimeSpan ServiceWait = TimeSpan.FromSeconds(30);

    public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                            CancellationToken ct)
    {
        var expected = cmd.Params.GetString("name");
        int pid = cmd.Params.GetInt("pid", 0);
        if (string.IsNullOrWhiteSpace(expected) || pid <= 0)
            return Task.FromResult(
                CommandResult.Fail("restart_process requires params.name and params.pid"));

        Process process;
        try { process = Process.GetProcessById(pid); }
        catch (ArgumentException)
        {
            return Task.FromResult(CommandResult.Fail(
                $"{expected} ({pid}) is no longer running"));
        }

        string actual;
        string imagePath;
        int session;
        using (process)
        {
            try { actual = process.ProcessName ?? ""; }
            catch { return Task.FromResult(CommandResult.Fail($"pid {pid} is no longer running")); }

            if (!ProcessGuard.NameMatches(expected, actual))
                return Task.FromResult(CommandResult.Fail(
                    $"{pid} is now {actual}, not {expected} -- the list had moved on, so nothing "
                    + "was restarted"));

            // Read fresh, never from the sampler's cache: the whole question is what is in
            // this process right now.
            var services = ProcessReader.ServicesForPid(pid);
            if (services.Count == 1)
                return Task.FromResult(RestartService(services[0]));
            if (services.Count > 1)
                return Task.FromResult(CommandResult.Fail(
                    $"{actual} ({pid}) hosts {services.Count} services ({string.Join(", ", services)}). "
                    + "Restart the one you mean from the Terminal tab (Restart-Service <name>) -- "
                    + "restarting this process would take all of them down together."));

            imagePath = ImagePathOf(process);
            session = SessionOf(pid);
            if (string.IsNullOrEmpty(imagePath))
                return Task.FromResult(CommandResult.Fail(
                    $"could not read the image path for {actual} ({pid}), so it could not be "
                    + "started again -- ending it was not attempted"));

            try
            {
                // Not the process tree: the children of an application that is coming back are
                // its own business, and taking them out is a different request (that is what
                // End task's "end the processes it started" is for).
                process.Kill();
                process.WaitForExit(ExitWaitMillis);
                if (!process.HasExited)
                    return Task.FromResult(CommandResult.Fail(
                        $"{actual} ({pid}) did not exit, so it was not started again"));
            }
            catch (Exception e)
            {
                _log.LogWarning(e, "Could not end {Name} ({Pid}) for restart", actual, pid);
                return Task.FromResult(CommandResult.Fail(
                    $"could not end {actual} ({pid}): {e.Message}"));
            }
        }

        return Task.FromResult(Relaunch(actual, imagePath, session));
    }

    /// <summary>Stop and start a service properly, taking its running dependents with it.
    ///
    /// Dependents are restarted explicitly afterwards because Windows does not do it: a
    /// plain stop/start of the print spooler would leave anything that depended on it
    /// stopped, and an operator restarting one service would silently have turned two off.</summary>
    private CommandResult RestartService(string serviceName)
    {
        try
        {
            using var service = new ServiceController(serviceName);
            var display = string.IsNullOrEmpty(service.DisplayName)
                ? serviceName : service.DisplayName;

            if (!service.CanStop)
                return CommandResult.Fail(
                    $"the {display} service does not accept a stop request");

            var dependents = new List<string>();
            foreach (var dependent in service.DependentServices)
            {
                using (dependent)
                {
                    if (dependent.Status != ServiceControllerStatus.Stopped)
                        dependents.Add(dependent.ServiceName);
                }
            }

            if (service.Status != ServiceControllerStatus.Stopped)
            {
                service.Stop(stopDependentServices: true);
                service.WaitForStatus(ServiceControllerStatus.Stopped, ServiceWait);
            }
            service.Start();
            service.WaitForStatus(ServiceControllerStatus.Running, ServiceWait);

            var restarted = new List<string>();
            var failed = new List<string>();
            foreach (var name in dependents)
            {
                try
                {
                    using var dependent = new ServiceController(name);
                    if (dependent.Status == ServiceControllerStatus.Stopped) dependent.Start();
                    dependent.WaitForStatus(ServiceControllerStatus.Running, ServiceWait);
                    restarted.Add(name);
                }
                catch (Exception e)
                {
                    // The service the operator asked for IS running; a dependent that would
                    // not come back is a real problem but a different one, and it must be
                    // named rather than folded into a blanket failure.
                    _log.LogWarning(e, "Dependent service {Name} did not restart", name);
                    failed.Add($"{name} ({e.Message})");
                }
            }

            var summary = $"restarted the {display} service";
            if (restarted.Count > 0) summary += $"; also restarted {string.Join(", ", restarted)}";
            if (failed.Count > 0)
                return CommandResult.Fail(
                    summary + $"; these dependents did NOT come back: {string.Join("; ", failed)}");
            return CommandResult.Ok(summary);
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "Could not restart service {Name}", serviceName);
            return CommandResult.Fail($"could not restart the {serviceName} service: {e.Message}");
        }
    }

    private CommandResult Relaunch(string name, string imagePath, int session)
    {
        if (!File.Exists(imagePath))
            return CommandResult.Fail(
                $"ended {name}, but its image is no longer at {imagePath}, so it was not "
                + "started again");

        var workingDirectory = Path.GetDirectoryName(imagePath) ?? "";

        // Session 0 is the services session -- nothing there has a desktop, and a process
        // found there is a background one that belongs to the machine rather than to a
        // person. Start it as ourselves (SYSTEM), which is what it was.
        if (session == 0)
        {
            try
            {
                using var started = Process.Start(new ProcessStartInfo
                {
                    FileName = imagePath,
                    WorkingDirectory = workingDirectory,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                });
                return CommandResult.Ok(
                    $"restarted {name} as SYSTEM (pid {started?.Id.ToString() ?? "unknown"})");
            }
            catch (Exception e)
            {
                _log.LogWarning(e, "Could not start {Path} in session 0", imagePath);
                return CommandResult.Fail($"ended {name}, but starting it again failed: {e.Message}");
            }
        }

        var result = UserSessionLauncher.LaunchAsSessionUser(imagePath, workingDirectory,
                                                             (uint)session);
        return result.Ok
            ? CommandResult.Ok($"restarted {name} in session {session} (pid {result.Pid})")
            : CommandResult.Fail(
                $"ended {name}, but starting it again in session {session} failed: {result.Error}");
    }

    private static string ImagePathOf(Process process)
    {
        // MainModule is the convenient answer but it throws for a 32-bit process seen from
        // a 64-bit one and for anything protected -- which is a large slice of what somebody
        // actually wants to restart. QueryFullProcessImageName answers for both, so it is
        // the fallback rather than the other way round.
        try
        {
            var path = process.MainModule?.FileName;
            if (!string.IsNullOrEmpty(path)) return path;
        }
        catch { /* fall through */ }

        try { return ProcessReader.ImagePathForPid(process.Id); }
        catch { return ""; }
    }

    private static int SessionOf(int pid)
    {
        try
        {
            using var process = Process.GetProcessById(pid);
            return process.SessionId;
        }
        catch { return 0; }
    }
}
