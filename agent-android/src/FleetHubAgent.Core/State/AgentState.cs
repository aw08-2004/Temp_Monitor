using System.Text.Json;

namespace FleetHubAgent.State;

/// <summary>
/// Reads and writes the agent's persisted state through an <see cref="IStateStore"/>.
///
/// **Every read fails soft.** A missing or corrupt value reads as "no state" rather than
/// throwing, because the alternative is a service that refuses to start over a value it could
/// rewrite. The one thing that must not fail soft is the identity WRITE -- see
/// <see cref="SaveIdentity"/>.
///
/// The Linux agent's equivalent also owns an atomic-write helper and a directory-permissions
/// pass. Neither has an Android counterpart and their absence is not an omission: rename(2)
/// atomicity is SharedPreferences' own problem and it solves it, and the permissions are the
/// kernel's per-UID isolation rather than anything this code sets. See IStateStore.
/// </summary>
public sealed class AgentState(IStateStore store)
{
    private readonly IStateStore _store = store;

    public AgentIdentity LoadIdentity()
    {
        try
        {
            var json = _store.Get(StateKeys.Identity);
            if (!string.IsNullOrEmpty(json))
            {
                var id = JsonSerializer.Deserialize<AgentIdentity>(json);
                if (id is not null) return id;
            }
        }
        catch { /* fall through to an empty identity */ }
        return new AgentIdentity();
    }

    /// <summary>Persist the enrollment identity, reporting whether it is really on disk.
    ///
    /// **The caller must not adopt an identity this returns false for.** The hub returns the
    /// token exactly once; an agent running on a token that was never committed works
    /// perfectly until the process is killed, and then re-enrolls and appears in the console
    /// as a second machine with the same name. Failing the enrollment attempt instead means
    /// it is simply retried in thirty seconds.</summary>
    public bool SaveIdentity(AgentIdentity identity)
    {
        try
        {
            return _store.Set(StateKeys.Identity, JsonSerializer.Serialize(identity));
        }
        catch { return false; }
    }

    /// <summary>The operator-set machine name, or null when this device still uses its
    /// derived default.</summary>
    public string? LoadMachineNameOverride()
    {
        var name = _store.Get(StateKeys.MachineNameOverride);
        return string.IsNullOrWhiteSpace(name) ? null : name;
    }

    public bool SaveMachineNameOverride(string name) =>
        _store.Set(StateKeys.MachineNameOverride, name);

    /// <summary>The stored enrollment secret, or null. Managed configuration takes precedence
    /// over this and is read by the platform layer, not here.</summary>
    public string? LoadEnrollmentSecret()
    {
        var secret = _store.Get(StateKeys.EnrollmentSecret);
        return string.IsNullOrWhiteSpace(secret) ? null : secret;
    }

    public bool SaveEnrollmentSecret(string secret) =>
        _store.Set(StateKeys.EnrollmentSecret, secret);

    /// <summary>The stored app policy document, or null. See StateKeys.DevicePolicy.</summary>
    public string? LoadDevicePolicy() => _store.Get(StateKeys.DevicePolicy);

    /// <summary>Store the app policy document. A failed write is not fatal -- the policy is
    /// applied from memory either way; what is lost is the staleness clock surviving a
    /// restart.</summary>
    public bool SaveDevicePolicy(string document) =>
        _store.Set(StateKeys.DevicePolicy, document);

    public string? LoadHubBaseOverride()
    {
        var hub = _store.Get(StateKeys.HubBaseOverride);
        return string.IsNullOrWhiteSpace(hub) ? null : hub;
    }

    public bool SaveHubBaseOverride(string hubBase) =>
        _store.Set(StateKeys.HubBaseOverride, hubBase);

    /// <summary>What a self-update is aiming at, or null if none is in flight.
    ///
    /// A corrupt value reads as "nothing in flight", which retries rather than refusing to
    /// update -- the same fail-soft rule the identity above follows, and the safe direction
    /// here: the worst case is one extra install attempt, bounded by the counter it just
    /// forgot.</summary>
    public RestartState? LoadRestartState()
    {
        var raw = _store.Get(StateKeys.RestartState);
        if (string.IsNullOrWhiteSpace(raw)) return null;
        try { return JsonSerializer.Deserialize<RestartState>(raw); }
        catch { return null; }
    }

    public bool SaveRestartState(RestartState restart) =>
        _store.Set(StateKeys.RestartState, JsonSerializer.Serialize(restart));

    /// <summary>Forget the update in flight, once this build has done real work.</summary>
    public bool ClearRestartState() => _store.Set(StateKeys.RestartState, null);
}
