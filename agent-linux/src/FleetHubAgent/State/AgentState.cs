using System.Text.Json;

namespace FleetHubAgent.State;

/// <summary>
/// Reads and writes the agent's persisted state under /var/lib/fleethub/agent -- which today
/// is one file, the enrollment identity.
///
/// **Every operation fails soft.** A missing or corrupt file reads as "no state" rather than
/// throwing, because the alternative is a service that refuses to start over a file it could
/// rewrite. The one thing that must not fail soft is the WRITE: see AtomicWrite.
/// </summary>
public sealed class AgentState
{
    private static readonly JsonSerializerOptions JsonOpts = new() { WriteIndented = true };

    /// <summary>Create and re-secure the state directory. Returns a note for the log when the
    /// permissions could not be applied -- the caller decides whether anyone is listening
    /// yet, which is why this is returned rather than logged here.</summary>
    public string? EnsureStateDir() => StateDirectory.Ensure(AgentConfig.StateDir);

    public AgentIdentity LoadIdentity()
    {
        try
        {
            if (File.Exists(AgentConfig.AgentIdentityPath))
            {
                var json = File.ReadAllText(AgentConfig.AgentIdentityPath);
                var id = JsonSerializer.Deserialize<AgentIdentity>(json);
                if (id is not null) return id;
            }
        }
        catch { /* fall through to empty identity */ }
        return new AgentIdentity();
    }

    public void SaveIdentity(AgentIdentity identity)
    {
        EnsureStateDir();
        AtomicWrite(AgentConfig.AgentIdentityPath, JsonSerializer.Serialize(identity, JsonOpts));
        StateDirectory.RestrictFile(AgentConfig.AgentIdentityPath);
    }

    /// <summary>What a self-update is aiming at, or null if none is in flight. See
    /// RestartState on why this is on disk rather than in memory.</summary>
    public RestartState? LoadRestartState()
    {
        try
        {
            if (File.Exists(AgentConfig.RestartStatePath))
            {
                var json = File.ReadAllText(AgentConfig.RestartStatePath);
                return JsonSerializer.Deserialize<RestartState>(json);
            }
        }
        catch { /* a corrupt file reads as "no update in flight", which retries rather than
                   refusing to start -- the same fail-soft rule as the identity above */ }
        return null;
    }

    public void SaveRestartState(RestartState state)
    {
        EnsureStateDir();
        AtomicWrite(AgentConfig.RestartStatePath, JsonSerializer.Serialize(state, JsonOpts));
    }

    /// <summary>Forget the update in flight. Called once the new build has done real work, not
    /// merely started -- see SelfUpdater.ConfirmRunningBuild.</summary>
    public void ClearRestartState()
    {
        try { File.Delete(AgentConfig.RestartStatePath); }
        catch { /* a file that cannot be deleted only costs one extra guard check */ }
    }

    /// <summary>Write via a temp file and rename.
    ///
    /// **The identity file is the one thing on this machine that cannot be regenerated.** The
    /// hub returns the token exactly once at enrollment; an agent that comes back to a
    /// half-written agent.json has no way to recover it, re-enrolls, and appears in the
    /// console as a SECOND machine with the same hostname. rename(2) within one filesystem is
    /// atomic, so a power cut during this leaves either the old file or the new one.</summary>
    private static void AtomicWrite(string path, string contents)
    {
        var tmp = path + ".tmp";
        File.WriteAllText(tmp, contents);
        File.Move(tmp, path, overwrite: true);
    }
}
