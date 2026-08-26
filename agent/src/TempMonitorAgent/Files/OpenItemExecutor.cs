using System.Diagnostics;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;
using TempMonitorAgent.Remote;

namespace TempMonitorAgent.Files;

/// <summary>
/// open_item: start one file, program or folder on this machine.
///
/// **The account is the whole design, which is why the operator picks it.** Everything else
/// the fleet runs -- run_script, the Terminal's shell, a package install -- runs as SYSTEM in
/// session 0, where a window exists but no desktop shows it. That is right for an installer
/// and wrong for "open their invoice": a WINWORD.EXE started as SYSTEM draws on nothing,
/// holds the file open, and can only be ended from Task Manager by somebody who knows to
/// look. So `run_as` is a parameter with two real modes and no default that hides the choice:
///
///   * `user`   -- the signed-in person's token, their profile, their desktop. They see it.
///   * `system` -- this service's own context, hidden. Executables and scripts only.
///
/// **A document opened as SYSTEM is refused rather than started.** It would appear on no
/// desktop for anybody, which is not what the word "open" meant when it was clicked. Naming
/// the two modes separately is only worth anything if the impossible half says so.
///
/// **Non-executables go through explorer.exe, and that is the point of the indirection.**
/// CreateProcessAsUser needs a program to run; a .pdf is not one. explorer.exe handed a path
/// invokes whatever handler is registered for that type in the USER's hive -- their PDF
/// reader, not a guess made here -- and handed a folder opens a window on it. The catch is
/// that it delegates to the shell already running in that session and exits immediately, so
/// the PID it returns is dead by the time the operator reads it. We say the request was
/// handed to their desktop instead of reporting a number that means nothing.
/// </summary>
public sealed class OpenItemExecutor : ICommandExecutor
{
    private readonly ILogger<OpenItemExecutor> _log;
    public OpenItemExecutor(ILogger<OpenItemExecutor> log) => _log = log;

    public string Type => "open_item";

    /// <summary>Things Windows will start on their own. Everything else needs a handler,
    /// which means a user hive, which means it cannot run as SYSTEM.</summary>
    private static readonly HashSet<string> Runnable = new(StringComparer.OrdinalIgnoreCase)
    {
        ".exe", ".com", ".bat", ".cmd", ".ps1", ".msi",
    };

    public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                            CancellationToken ct)
    {
        var refusal = PathRules.Reject(cmd.Params.GetString("path"));
        if (refusal is not null) return Task.FromResult(CommandResult.Fail(refusal));
        var path = PathRules.Normalize(cmd.Params.GetString("path")!);

        var isDirectory = Directory.Exists(path);
        if (!isDirectory && !File.Exists(path))
            return Task.FromResult(CommandResult.Fail($"{path} is not there any more."));

        var runAs = (cmd.Params.GetString("run_as") ?? "user").Trim().ToLowerInvariant();
        try
        {
            return Task.FromResult(runAs switch
            {
                "user" => OpenAsUser(path, isDirectory),
                "system" => OpenAsSystem(path, isDirectory),
                _ => CommandResult.Fail($"Unknown run_as: {runAs}"),
            });
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException
                                    or System.ComponentModel.Win32Exception)
        {
            _log.LogWarning(e, "open_item {Path} as {RunAs} threw", path, runAs);
            return Task.FromResult(CommandResult.Fail(e.Message));
        }
    }

    // ---------------- as the signed-in user ----------------

    private CommandResult OpenAsUser(string path, bool isDirectory)
    {
        // Resolved before anything is launched, so "nobody is here" costs nothing and reads
        // the same as it does from show_message.
        var session = SessionInjector.AutoSelectSession();
        var who = SignedInUser(session);
        if (who is null)
            return CommandResult.Fail(
                "Nobody is signed in to this PC, so there is no desktop to open it on. "
                + "Open it as SYSTEM if it is a program that does not need one.");

        var (program, arguments, handsOff) = Resolve(path, isDirectory);
        var workingDirectory = isDirectory ? path : (Path.GetDirectoryName(path) ?? "");

        var launch = UserSessionLauncher.LaunchAsSessionUser(
            program, workingDirectory, session, arguments);
        if (!launch.Ok)
            return CommandResult.Fail($"Could not start it as {who}: {launch.Error}");

        _log.LogInformation("open_item: {Path} opened as {User} in session {Session}",
                            path, who, session);
        // handsOff: explorer.exe passed the request to the shell already running there and
        // exited. The PID is real and already gone, so naming it would send an operator to
        // Task Manager to look for a process that was never the point.
        return CommandResult.Ok(handsOff
            ? $"[files] opened {path} on {who}'s desktop (session {session})"
            : $"[files] started {path} as {who} in session {session}, pid {launch.Pid}");
    }

    /// <summary>Which program actually gets launched, with what arguments, and whether it is
    /// a handoff to the user's shell rather than the thing itself.</summary>
    private static (string Program, string Arguments, bool HandsOff) Resolve(
        string path, bool isDirectory)
    {
        var system32 = Environment.SystemDirectory;
        var windows = Path.GetDirectoryName(system32) ?? @"C:\Windows";
        var explorer = Path.Combine(windows, "explorer.exe");

        if (isDirectory) return (explorer, Quoted(path), true);

        return Path.GetExtension(path).ToLowerInvariant() switch
        {
            // Started directly: a real PID to report, and no dependency on a shell being up.
            ".exe" or ".com" => (path, "", false),
            ".bat" or ".cmd" => (Path.Combine(system32, "cmd.exe"), $"/c {Quoted(path)}", false),
            ".ps1" => (Path.Combine(system32, @"WindowsPowerShell\v1.0\powershell.exe"),
                       $"-NoLogo -ExecutionPolicy Bypass -File {Quoted(path)}", false),
            ".msi" => (Path.Combine(system32, "msiexec.exe"), $"/i {Quoted(path)}", false),
            // A document. Whatever the user's own hive says opens this.
            _ => (explorer, Quoted(path), true),
        };
    }

    // ---------------- as SYSTEM ----------------

    private CommandResult OpenAsSystem(string path, bool isDirectory)
    {
        if (isDirectory)
            return CommandResult.Fail(
                "A folder can only be opened on somebody's desktop. Open it as the signed-in user.");

        var extension = Path.GetExtension(path).ToLowerInvariant();
        if (!Runnable.Contains(extension))
            return CommandResult.Fail(
                $"SYSTEM has no desktop and no file associations, so a {extension} file opened "
                + "here would run on a screen nobody can see. Open it as the signed-in user.");

        var (program, arguments, _) = Resolve(path, isDirectory: false);

        // Not ProcessRunner: that captures output and waits for the exit, and this is a
        // launch -- the operator asked for the program to be running, not for its transcript.
        // Nothing is redirected, so nothing fills a pipe nobody is draining either.
        using var proc = new Process
        {
            StartInfo = new ProcessStartInfo
            {
                FileName = program,
                Arguments = arguments,
                UseShellExecute = false,
                CreateNoWindow = true,
                WorkingDirectory = Path.GetDirectoryName(path) ?? Environment.SystemDirectory,
            },
        };
        if (!proc.Start()) return CommandResult.Fail($"Windows would not start {path}.");

        _log.LogInformation("open_item: {Path} started as SYSTEM, pid {Pid}", path, proc.Id);
        return CommandResult.Ok(
            $"[files] started {path} as SYSTEM in session 0, pid {proc.Id}. "
            + "It has no window anybody can see.");
    }

    // ---------------- helpers ----------------

    /// <summary>The person signed in to this session, or null for the logon screen -- where
    /// there is a window station but nobody to hand a window to. Same check show_message
    /// makes, and for the same reason: the hub's session list can be seconds stale and this
    /// one is authoritative.</summary>
    private static string? SignedInUser(uint session)
    {
        if (session == SessionInjector.NoActiveSession) return null;
        foreach (var s in SessionProbe.Enumerate())
        {
            if (s.SessionId != session) continue;
            if (s.IsLogonScreen || string.IsNullOrWhiteSpace(s.User)) return null;
            return string.IsNullOrWhiteSpace(s.Domain) ? s.User : $"{s.Domain}\\{s.User}";
        }
        return null;
    }

    private static string Quoted(string path) => $"\"{path}\"";
}
