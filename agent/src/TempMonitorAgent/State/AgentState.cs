using System.Text.Json;

namespace TempMonitorAgent.State;

/// <summary>
/// Reads/writes the agent's persisted state under %ProgramData%\TempMonitorAgent:
/// enrollment identity (agent.json) and the self-update restart guard
/// (restart_state.json). All operations fail soft — a missing/corrupt file reads
/// as "no state" rather than throwing.
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

    private static void AtomicWrite(string path, string contents)
    {
        var tmp = path + ".tmp";
        File.WriteAllText(tmp, contents);
        File.Move(tmp, path, overwrite: true);
    }
}
