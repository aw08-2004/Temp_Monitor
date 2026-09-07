using FleetHubAgent.State;

namespace FleetHubAgent;

/// <summary>
/// The name this device currently reports as "machine", and the only thing allowed to change
/// it.
///
/// **Why this is an object rather than AgentConfig.MachineName.** On Windows and Linux the
/// machine name is the hostname: the OS owns it, rename asks the OS to change it, and the
/// agent reads it back. Android has no hostname, so this agent's name is a value it stores
/// itself -- which means something in this process has to own it, keep it consistent between
/// the telemetry loop and the command loop, and persist a change before reporting under it.
///
/// The ordering inside <see cref="TrySet"/> is the load-bearing part. A rename that is adopted
/// in memory and then fails to persist gives a device that reports under the new name until it
/// is next killed and under the old one afterwards -- which is not a lost setting, it is TWO
/// machines in the console, and the second one appears days later with no obvious cause.
/// </summary>
public sealed class MachineNameProvider
{
    private readonly AgentState _state;
    private readonly string _default;
    private string _current;
    private readonly Lock _gate = new();

    /// <param name="derivedDefault">What this device is called when no operator has renamed
    /// it -- see <see cref="MachineNaming.Derive"/>.</param>
    public MachineNameProvider(AgentState state, string derivedDefault)
    {
        _state = state;
        _default = derivedDefault;
        // A stored name that is no longer valid (written by an older build, or restored from a
        // backup taken on another device) loses to the derived default rather than being sent:
        // the hub would answer a bad name with a 400 on every report, and the device would go
        // quiet with no local symptom.
        var stored = state.LoadMachineNameOverride();
        _current = MachineNaming.IsValid(stored) ? stored! : derivedDefault;
    }

    public string Current
    {
        get { lock (_gate) return _current; }
    }

    /// <summary>Whether this device is still reporting under its derived name.</summary>
    public bool IsDefault
    {
        get { lock (_gate) return string.Equals(_current, _default, StringComparison.Ordinal); }
    }

    /// <summary>
    /// Rename the device, persisting before adopting.
    ///
    /// Returns false when the name is unusable or could not be stored, and the caller reports
    /// that as a failed command -- an operator told "renamed" about a name that will be gone
    /// at the next restart is the failure this whole class is shaped around.
    /// </summary>
    public bool TrySet(string name, out string error)
    {
        var trimmed = (name ?? "").Trim();
        if (!MachineNaming.IsValid(trimmed))
        {
            error = $"'{name}' is not a usable machine name: 1-{MachineNaming.MaxChars} " +
                    "printable ASCII characters, and none of < > \" ' &";
            return false;
        }

        lock (_gate)
        {
            if (string.Equals(_current, trimmed, StringComparison.Ordinal))
            {
                error = "";
                return true;
            }
            // Persist FIRST. See the class note: adopting a name we could not store is what
            // duplicates the device in the console.
            if (!_state.SaveMachineNameOverride(trimmed))
            {
                error = "the new name could not be saved on the device, so it was not adopted";
                return false;
            }
            _current = trimmed;
        }

        error = "";
        return true;
    }
}
