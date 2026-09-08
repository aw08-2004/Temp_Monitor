using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using FleetHubAgent.State;

namespace FleetHubAgent.Fleet;

/// <summary>
/// Holds the device's app policy, decides when to apply it, and reports what happened.
///
/// **An <see cref="IInventorySource"/>, which is not a coincidence.** What the device did with
/// its policy is exactly the shape of every other change-only block: it is expensive to
/// produce, it rarely changes, and it must reach the hub even if the heartbeat that first
/// carried it failed. Reusing that machinery means the report inherits the "not sent until a
/// heartbeat succeeds" property for free, rather than growing a second half-implementation of
/// it.
///
/// **Applying happens on the inventory loop, never on the heartbeat.** Suspending forty
/// packages is a binder call per package on some builds, and the heartbeat is the call that
/// decides whether the machine reads online. It also gives the dead-man switch a tick of its
/// own: <see cref="Tick"/> is what notices a policy has gone stale, and it has to run whether
/// or not the hub is reachable -- which, when the switch matters, it is not.
///
/// **The persisted copy is what makes the dead-man switch honest.** Android kills an app's
/// process without warning; an agent that forgot its policy on every restart would restart the
/// staleness clock with it, and a device whose hub had gone silent a month ago would go on
/// enforcing forever, one restart at a time.
/// </summary>
public sealed class PolicyCoordinator(
    ILogger<PolicyCoordinator> log, AgentState state, IPolicyEnforcer enforcer)
    : IInventorySource
{
    /// <summary>Matches the heartbeat key hub/fleet_web.py ingests.</summary>
    public string Key => "policy_state";

    /// <summary>Short, because this block only changes when an application happens and the
    /// reporter drops an unchanged one anyway. The interval is really "how soon after a policy
    /// is applied does the console learn", and a minute is the same answer the inventory tick
    /// gives.</summary>
    public TimeSpan RefreshInterval => TimeSpan.FromMinutes(1);

    private readonly object _gate = new();
    private DevicePolicy? _policy;
    /// <summary>The version whose application has actually been carried out, which is not the
    /// same as the version held: between accepting a document and the next tick they differ,
    /// and so do they after an application that partly failed.</summary>
    private string _appliedVersion = "";
    private DateTimeOffset? _appliedAt;
    private IReadOnlyList<string> _failed = [];
    private string _error = "";
    private bool _lifted;

    /// <summary>The version this device currently holds, sent on every heartbeat so the hub can
    /// skip re-sending an unchanged document. Empty when none has ever arrived, which a hub
    /// reads as "different from whatever I have" and answers with the current one.</summary>
    public string CurrentVersion
    {
        get { lock (_gate) return _policy?.Version ?? ""; }
    }

    /// <summary>Load whatever was persisted. Called once, at composition, so a restart resumes
    /// enforcing rather than starting from nothing -- and, more importantly, resumes the
    /// staleness clock where it left off.</summary>
    public void Restore()
    {
        var raw = state.LoadDevicePolicy();
        if (string.IsNullOrEmpty(raw)) return;
        try
        {
            var restored = DevicePolicy.FromJson(JsonNode.Parse(raw));
            if (restored is null) return;
            lock (_gate) _policy = restored;
            log.LogInformation("Restored app policy {Version} ({Count} package(s)), " +
                               "confirmed {When}",
                restored.Version, restored.Blocked.Count, restored.ReceivedAtUtc);
        }
        catch (Exception e)
        {
            // A stored document that will not parse is dropped rather than fatal. The next
            // heartbeat brings the current one, and until then nothing new is applied.
            log.LogWarning("Stored app policy could not be read: {Msg}", e.Message);
        }
    }

    /// <summary>The hub sent a document. Stores it and persists it; applying is
    /// <see cref="Tick"/>'s job.</summary>
    public void Accept(JsonNode? document, string? version, DateTimeOffset now)
    {
        var parsed = DevicePolicy.Parse(document, version, now);
        if (parsed is null)
        {
            // NOT read as empty. Empty means "lift everything", which is a real instruction;
            // unparseable means the hub said something this agent does not understand, and the
            // safe answer is to keep enforcing what is applied until a document makes sense.
            log.LogWarning("Ignoring an app policy document this agent cannot read");
            return;
        }
        lock (_gate) _policy = parsed;
        Persist(parsed);
        log.LogInformation("Accepted app policy {Version}: {Count} package(s) to suspend",
            parsed.Version, parsed.Blocked.Count);
    }

    /// <summary>The hub confirmed the version this device already holds. Refreshes the
    /// staleness clock -- see DevicePolicy on why that is not a no-op.</summary>
    public void Reaffirm(DateTimeOffset now)
    {
        DevicePolicy? policy;
        lock (_gate)
        {
            _policy?.Reaffirm(now);
            policy = _policy;
        }
        if (policy is not null) Persist(policy);
    }

    /// <summary>Apply the held policy if anything has changed, and lift it if it has gone
    /// stale. Cheap when there is nothing to do, which is almost always.</summary>
    public void Tick(DateTimeOffset now)
    {
        DevicePolicy? policy;
        string applied;
        bool lifted;
        lock (_gate)
        {
            policy = _policy;
            applied = _appliedVersion;
            lifted = _lifted;
        }
        if (policy is null) return;

        var expired = policy.IsExpired(now);
        if (expired && lifted) return;                       // already lifted; nothing to do
        if (!expired && applied == policy.Version) return;   // already applied; nothing to do

        if (!enforcer.CanEnforce)
        {
            // Reported rather than retried silently. A sideloaded build is the ordinary state
            // of a device that was never provisioned, and the console has to be able to say
            // "this device is not fully managed" instead of showing a policy that looks live.
            lock (_gate)
            {
                _appliedVersion = policy.Version;
                _appliedAt = now;
                _failed = policy.Blocked;
                _error = "this device is not fully managed, so no app policy can be applied";
            }
            log.LogWarning("App policy {Version} cannot be applied: not a device owner",
                policy.Version);
            return;
        }

        var wanted = policy.Effective(now);
        if (expired)
        {
            // The dead-man switch firing. Logged at Warning because it is not routine and
            // somebody reading the log later needs to see it: a device lifted its restrictions
            // because it stopped hearing from this hub.
            log.LogWarning("App policy {Version} has not been confirmed for {Days:F1} day(s); " +
                           "lifting every restriction", policy.Version,
                (now - policy.ReceivedAtUtc).TotalDays);
        }

        IReadOnlyList<string> failed;
        try
        {
            failed = enforcer.Apply(wanted);
        }
        catch (Exception e)
        {
            // The enforcer contract says it must not throw; if it does, that is recorded and
            // retried rather than left to take down the loop.
            log.LogWarning(e, "Applying app policy {Version} threw", policy.Version);
            lock (_gate)
            {
                _error = $"applying the policy failed: {e.Message}";
                _appliedAt = now;
            }
            return;
        }

        lock (_gate)
        {
            _appliedVersion = policy.Version;
            _appliedAt = now;
            _failed = failed;
            _lifted = expired;
            _error = expired
                ? "the policy was lifted because this device has not heard from the hub"
                : "";
        }
        log.LogInformation("Applied app policy {Version}: {Wanted} wanted, {Failed} refused",
            policy.Version, wanted.Count, failed.Count);
    }

    /// <summary>The `policy_state` block. Null before anything has ever been applied, so a
    /// device that has never had a policy sends nothing at all rather than an empty report the
    /// console would have to special-case.</summary>
    public JsonObject? Read()
    {
        lock (_gate)
        {
            if (_appliedAt is null) return null;
            return new JsonObject
            {
                ["version"] = _appliedVersion,
                ["applied_at"] = _appliedAt.Value.ToUnixTimeSeconds(),
                // The field this whole report exists for: setPackagesSuspended returns what it
                // could not suspend, and a policy reported as applied while three of its
                // targets are still running is worse than no policy at all.
                ["failed"] = new JsonArray(_failed.Select(p => (JsonNode)p!).ToArray()),
                ["error"] = _error,
            };
        }
    }

    private void Persist(DevicePolicy policy)
    {
        try { state.SaveDevicePolicy(policy.ToJson().ToJsonString()); }
        catch (Exception e)
        {
            // Not fatal: the policy is applied from memory either way. What is lost is the
            // staleness clock surviving a restart, which is worth a line.
            log.LogWarning("Could not persist the app policy: {Msg}", e.Message);
        }
    }
}
