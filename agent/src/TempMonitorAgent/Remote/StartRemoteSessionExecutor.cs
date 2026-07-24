using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Handles the <c>start_remote_session</c> command (roadmap #2): it session-injects the
/// capture/control helper into the interactive desktop and reports whether the launch
/// succeeded. Authorization already happened at the hub's console session gate
/// (remote_control capability + machine scope); this end just carries it out.
///
/// The session parameters are written to a file and the helper is pointed at it, rather than
/// passing them as command-line arguments -- later phases add single-use TURN credentials,
/// which must never appear in the process list. The file is deleted by the helper on read.
///
/// The result is the LAUNCH outcome, not the session outcome: once the helper is running it
/// talks to the hub over its own signaling channel (phase 3), so there is nothing more for
/// this short-lived command to report or wait on.
/// </summary>
public sealed class StartRemoteSessionExecutor : ICommandExecutor
{
    private readonly ILogger<StartRemoteSessionExecutor> _log;

    public StartRemoteSessionExecutor(ILogger<StartRemoteSessionExecutor> log) => _log = log;

    public string Type => "start_remote_session";

    public Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var (session, error) = BuildSessionParams(cmd);
        if (session is null)
            return Task.FromResult(CommandResult.Fail(error ?? "invalid start_remote_session params"));

        string sessionFile;
        try
        {
            Directory.CreateDirectory(AgentConfig.RemoteStateDir);
            sessionFile = SessionFilePath(session.SessionId);
            File.WriteAllText(sessionFile, session.ToJson());
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "start_remote_session: could not stage session file");
            return Task.FromResult(CommandResult.Fail($"could not stage session file: {e.Message}"));
        }

        var exePath = Environment.ProcessPath;
        if (string.IsNullOrEmpty(exePath))
            return Task.FromResult(CommandResult.Fail("cannot resolve agent executable path"));

        var arguments = $"{AgentConfig.RemoteHelperArg} \"{sessionFile}\"";
        var result = SessionInjector.Launch(exePath, arguments);
        if (!result.Ok)
        {
            // Leave no orphan session file behind on a failed launch (the helper would have
            // deleted it; it never ran).
            TryDelete(sessionFile);
            _log.LogWarning("start_remote_session {SessionId} failed to inject: {Error}",
                session.SessionId, result.Error);
            return Task.FromResult(CommandResult.Fail($"remote helper launch failed: {result.Error}"));
        }

        _log.LogInformation(
            "start_remote_session {SessionId}: helper launched pid={Pid} in session {Session}",
            session.SessionId, result.Pid, result.SessionId);
        return Task.FromResult(CommandResult.Ok(
            $"remote helper launched (pid {result.Pid}, session {result.SessionId})"));
    }

    /// <summary>Parse and validate the command's params into session parameters. Pure and
    /// side-effect free so it can be unit-tested without a hub or a desktop.</summary>
    internal static (RemoteSessionParams? session, string? error) BuildSessionParams(FleetCommand cmd)
    {
        var sessionId = cmd.Params.GetString("session_id");
        if (string.IsNullOrWhiteSpace(sessionId))
            return (null, "start_remote_session requires params.session_id");
        if (!IsSafeSessionId(sessionId))
            return (null, "params.session_id must be alphanumeric, '-' or '_' (it names a file)");

        var consent = cmd.Params.GetString("consent_mode");
        if (string.IsNullOrWhiteSpace(consent)) consent = "unattended";

        var session = new RemoteSessionParams
        {
            SessionId = sessionId,
            Monitor = Math.Max(0, cmd.Params.GetInt("monitor", 0)),
            ConsentMode = consent,
            // Always from the hub's trusted attribution, never a client-supplied param.
            IssuedBy = cmd.IssuedBy ?? "",
            IceServers = ParseIceServers(cmd.Params),
        };
        return (session, null);
    }

    /// <summary>Read the hub-minted ICE servers out of the command params. A missing or
    /// malformed list yields an empty list (LAN host candidates still connect), never a
    /// throw.</summary>
    internal static List<IceServerConfig> ParseIceServers(JsonNode? paramsNode)
    {
        if (paramsNode is JsonObject obj && obj.TryGetPropertyValue("ice_servers", out var node)
            && node is JsonArray arr)
        {
            try { return arr.Deserialize<List<IceServerConfig>>() ?? new(); }
            catch { /* fall through to empty */ }
        }
        return new List<IceServerConfig>();
    }

    /// <summary>Session ids are hub-minted (uuid hex), but this end still validates before
    /// using one to name a file -- an id is turned into a path, so path metacharacters must
    /// never slip through.</summary>
    internal static bool IsSafeSessionId(string id)
    {
        if (id.Length is 0 or > 128) return false;
        foreach (var c in id)
            if (!(char.IsAsciiLetterOrDigit(c) || c is '-' or '_')) return false;
        return true;
    }

    internal static string SessionFilePath(string sessionId) =>
        Path.Combine(AgentConfig.RemoteStateDir, sessionId + ".session.json");

    private static void TryDelete(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); } catch { /* best effort */ }
    }
}
