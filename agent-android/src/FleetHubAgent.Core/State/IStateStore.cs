namespace FleetHubAgent.State;

/// <summary>
/// The small key/value store the agent keeps its identity and its name override in.
///
/// **Why this is an interface here and a concrete class on Linux.** The Linux agent writes
/// /var/lib/fleethub/agent/agent.json and spends a whole file (State/StateDirectory.cs)
/// forcing that tree to 0700/0600, because on a multi-user box a world-readable bearer token
/// lets any local account impersonate the machine to the fleet. Android has no such problem
/// and no such lever: an app's private data directory is already isolated by UID, per app, by
/// the kernel, and there is no chmod for an app to get wrong. So the *security* half of that
/// file has no Android counterpart, and the *storage* half is SharedPreferences.
///
/// That leaves an interface rather than a second file-writing class for one concrete reason:
/// the protocol code that reads and writes the enrollment identity is the most dangerous code
/// in this project, and it has to be testable on a workstation with no device attached. See
/// FleetHubAgent.Core.csproj.
///
/// **Every write must be durable by the time it returns.** Android kills an app's process
/// without warning and without running anything on the way out -- there is no SIGTERM to
/// flush on. The enrollment token comes back from the hub exactly once, so a write that was
/// still sitting in a buffer when the process died leaves a device that re-enrolls and
/// appears in the console as a SECOND machine. The Android implementation therefore uses
/// SharedPreferences.Editor.Commit() and not Apply(); see AndroidStateStore.
/// </summary>
public interface IStateStore
{
    /// <summary>The stored value, or null when absent. Never throws -- a store that cannot be
    /// read is "no state", the same fail-soft rule the Linux agent applies to a corrupt
    /// agent.json, because the alternative is a service that refuses to start.</summary>
    string? Get(string key);

    /// <summary>Store a value durably, or remove it when <paramref name="value"/> is null.
    /// Returns whether it was actually committed -- callers that are about to rely on it
    /// (enrollment) check, and callers that are not (the name override) may ignore it.</summary>
    bool Set(string key, string? value);
}

/// <summary>The keys, in one place so the Android layer and the tests cannot disagree about
/// them. Renaming one silently orphans whatever a deployed device already stored under the
/// old name -- for the identity key that means a duplicate machine, so treat these as a
/// wire format rather than as local names.</summary>
public static class StateKeys
{
    /// <summary>The enrollment identity, as the JSON of <see cref="AgentIdentity"/>.</summary>
    public const string Identity = "agent_identity";

    /// <summary>The operator-set machine name, written by RenameExecutor. Absent means "use
    /// the derived default" -- see MachineNaming.</summary>
    public const string MachineNameOverride = "machine_name";

    /// <summary>The hub base URL, when an operator typed one on the setup screen. Managed
    /// configuration is read live from the MDM and is never stored here, so that an MDM
    /// changing the hub takes effect rather than losing to a stale local copy.</summary>
    public const string HubBaseOverride = "hub_base";

    /// <summary>The device's app policy, as the JSON of DevicePolicy (roadmap #23 phase D).
    ///
    /// Persisted for one reason and it is not "to avoid re-applying": the dead-man switch is
    /// measured from when the hub last confirmed the policy, so an agent that forgot it on
    /// every restart would restart that clock too -- and a device whose hub went silent a
    /// month ago would go on enforcing forever, one process kill at a time.</summary>
    public const string DevicePolicy = "device_policy";

    /// <summary>What a self-update is aiming at, and how many attempts it has had
    /// (roadmap #22). Persisted because the process that would remember it is the one
    /// the platform kills to install the new package -- and because a build that
    /// installs, starts and immediately dies would otherwise be downloaded and
    /// installed again on the next tick, forever.</summary>
    public const string RestartState = "update_state";

    /// <summary>The shared enrollment secret. **This is the hub's AGENT_ENROLLMENT_SECRET,
    /// the same value on every machine in the fleet** -- not a per-device credential. What IS
    /// minted per device, and returned exactly once, is the token enroll hands back.</summary>
    public const string EnrollmentSecret = "enrollment_secret";
}
