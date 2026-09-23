using System.Text.Json;
using TempMonitorAgent.Watchdog;

namespace TempMonitorAgent.State;

/// <summary>
/// Reads/writes the agent's persisted state under %ProgramData%\TempMonitorAgent:
/// enrollment identity (agent.json), the self-update restart guard (restart_state.json),
/// the hub-delivered runtime config (config.json) and the watchdog document with its
/// restart history (watchdogs.json). All operations fail soft — a missing/corrupt file
/// reads as "no state" rather than throwing.
/// </summary>
public sealed class AgentState
{
    private static readonly JsonSerializerOptions JsonOpts = new() { WriteIndented = true };

    public void EnsureStateDir()
    {
        Directory.CreateDirectory(AgentConfig.ProgramDataDir);
    }

    // --- Enrollment identity ----------------------------------------------
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
        var json = JsonSerializer.Serialize(identity, JsonOpts);
        AtomicWrite(AgentConfig.AgentIdentityPath, json);
    }

    /// <summary>Throw the enrollment identity away, so the next tick enrolls from scratch.
    ///
    /// **Deleting the file is the point, not blanking the field in memory.** The identity
    /// outlives the process AND the install -- the uninstaller deliberately leaves
    /// %ProgramData% alone -- so an identity the hub no longer recognises would otherwise
    /// survive a stop, a self-update and a full reinstall, and keep this machine on the
    /// unauthenticated telemetry path forever. Fails soft like everything else here: a file
    /// we could not delete leaves the old identity in place and the caller simply refuses
    /// again next tick, which is where we already were.</summary>
    public void ClearIdentity()
    {
        try
        {
            if (File.Exists(AgentConfig.AgentIdentityPath))
                File.Delete(AgentConfig.AgentIdentityPath);
        }
        catch { /* ignore -- see the docstring */ }
    }

    // --- Restart guard -----------------------------------------------------
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
        catch { /* ignore */ }
        return null;
    }

    public void SaveRestartState(RestartState state)
    {
        EnsureStateDir();
        var json = JsonSerializer.Serialize(state, JsonOpts);
        AtomicWrite(AgentConfig.RestartStatePath, json);
    }

    public void ClearRestartState()
    {
        try
        {
            if (File.Exists(AgentConfig.RestartStatePath))
                File.Delete(AgentConfig.RestartStatePath);
        }
        catch { /* ignore */ }
    }

    // --- Hub-delivered runtime config --------------------------------------
    public RuntimeConfig LoadRuntimeConfig()
    {
        try
        {
            if (File.Exists(AgentConfig.AgentConfigPath))
            {
                var json = File.ReadAllText(AgentConfig.AgentConfigPath);
                var cfg = JsonSerializer.Deserialize<RuntimeConfig>(json);
                if (cfg is not null) return cfg;
            }
        }
        catch { /* fall through to compiled defaults */ }
        return RuntimeConfig.Default;
    }

    public void SaveRuntimeConfig(RuntimeConfig config)
    {
        EnsureStateDir();
        var json = JsonSerializer.Serialize(config, JsonOpts);
        AtomicWrite(AgentConfig.AgentConfigPath, json);
    }

    // --- Watchdogs (roadmap #20) -------------------------------------------
    /// <summary>The watchdog document and the restart history behind it, or null when this
    /// machine has never held one.
    ///
    /// **Persisted for the restart history, not for the document.** The document arrives again
    /// on the first heartbeat after a restart; the count of how many times the spooler has
    /// already been restarted in the last hour does not, and losing it would let a machine
    /// whose flapping service takes the agent down with it restart that service forever while
    /// the hub is told everything is fine. Fails soft like everything else here: an unreadable
    /// file reads as "no watchdogs", which is the direction that does nothing rather than the
    /// direction that acts on stale instructions.</summary>
    public StoredWatchdogState? LoadWatchdogState()
    {
        try
        {
            if (File.Exists(AgentConfig.WatchdogStatePath))
            {
                var json = File.ReadAllText(AgentConfig.WatchdogStatePath);
                return JsonSerializer.Deserialize<StoredWatchdogState>(json);
            }
        }
        catch { /* ignore -- see the docstring */ }
        return null;
    }

    public void SaveWatchdogState(StoredWatchdogState state)
    {
        try
        {
            EnsureStateDir();
            AtomicWrite(AgentConfig.WatchdogStatePath, JsonSerializer.Serialize(state, JsonOpts));
        }
        catch
        {
            // A write that fails costs the flap history across a restart, which is bad, but
            // throwing here would cost the watchdog TICK, which is worse: the loop exists to
            // act on a machine nobody is watching.
        }
    }

    private static void AtomicWrite(string path, string contents)
    {
        var tmp = path + ".tmp";
        File.WriteAllText(tmp, contents);
        File.Move(tmp, path, overwrite: true);
    }
}
