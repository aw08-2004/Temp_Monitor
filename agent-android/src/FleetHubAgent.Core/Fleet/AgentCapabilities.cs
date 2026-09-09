using System.Text.Json.Nodes;

namespace FleetHubAgent.Fleet;

/// <summary>
/// What this agent can be told to do, in the shape the hub's `capabilities` heartbeat block
/// expects (hub/capabilities.py).
///
/// **A version number cannot say "never".** The console decides what to offer from
/// MIN_*_AGENT constants that hardcode the agent minor a feature arrived in, which works while
/// every managed machine is a Windows PC on one release train. It cannot express "this device
/// can be renamed but can never run a script", which on Android is a fact about the platform
/// rather than a position in a backlog -- there is no future version of this agent that will
/// reboot a phone. So the agent states its abilities and the hub stores them, which is what
/// lets <see cref="AgentConfig.Version"/> stay at 0.1.0 permanently instead of being dragged
/// past the 3.0.0 floor to buy a feature gate it would fail anyway.
///
/// **And the version gates do not run for scheduled work at all.** They are JavaScript in the
/// console, deciding which BUTTONS to draw. Nothing evaluates them for a command the hub's
/// backup scheduler, patch scheduler, deployment scheduler or rules engine dispatches, because
/// those target a machine set. The first real device was inside a fleet-wide backup profile and
/// had `backup_files` queued to it within a minute of enrolling. CommandDispatcher.Impossible
/// is the agent-side half of that story -- answering with the reason instead of a shrug -- and
/// this is the half that stops the command being sent.
///
/// **The command list is DERIVED from the dispatcher, never written by hand.** A hand-kept list
/// drifts the first time an executor is added, and it drifts in the direction that hurts: the
/// hub goes on refusing a command this agent has just learned to run, and the only symptom is
/// a button that stays grey. Building it from the executor set means the two cannot disagree.
///
/// **Two lists, because they travel on different channels.** <see cref="Commands"/> are command
/// types, which arrive through the command queue and are what the hub's create_command checks.
/// <see cref="Features"/> are everything that is not a command -- on-demand location, app
/// policy, a usage ledger -- which arrive on the heartbeat's policy block. Mixing them into one
/// list would leave the console unable to tell which entries it may render a button for.
///
/// Sent on every heartbeat rather than only when it changes. The block is a hundred bytes and
/// never changes in normal operation, and the hub writes only when the content differs -- so
/// keeping the "have I sent this" state on the HUB rather than here is what makes a device
/// re-report itself after the hub restores its database from a backup. An agent holding that
/// flag would stay silent, and the machine would be silently ungated until somebody reinstalled
/// the app.
/// </summary>
public sealed class AgentCapabilities
{
    /// <summary>The platform slug. Must be one of the values hub/capabilities.py's PLATFORMS
    /// tuple accepts -- anything else is stored there as unknown, which reads as "has not
    /// said" and quietly turns off every gate this class exists to feed.</summary>
    public const string PlatformAndroid = "android";

    // ---------------------------------------------------------------- feature slugs
    // Mirrors hub/capabilities.py's FEATURE_* constants. None of these is reported yet; they
    // are named here so the phase that implements one adds it in a single place rather than
    // inventing a string that the hub has never heard of and will silently ignore.

    /// <summary>On-demand location (roadmap #23 phase B).</summary>
    public const string FeatureLocate = "locate";
    /// <summary>App inventory and suspension policy (phase D).</summary>
    public const string FeatureAppPolicy = "app_policy";
    /// <summary>Screen-time budgets and blocked hours (phase E).</summary>
    public const string FeatureTimePolicy = "time_policy";
    /// <summary>Usage access has actually been granted on this device (phase E). Separate from
    /// <see cref="FeatureTimePolicy"/> because the two fail differently: without usage access a
    /// curfew still holds and a BUDGET never fires, silently, since "nobody has used anything"
    /// and "I was not allowed to look" are the same zero. It is an appop no Device Owner can
    /// grant, so this is the only way the console learns that somebody has to walk over to the
    /// device.</summary>
    public const string FeatureUsageAccess = "usage_access";
    /// <summary>Enrolled as an Android Device Owner (phase A). Not something this agent
    /// chooses -- it is a fact about how the device was provisioned, and every policy feature
    /// degrades without it.</summary>
    public const string FeatureDeviceOwner = "device_owner";

    public string Platform { get; }

    /// <summary>The command types this agent will actually route, sorted so the JSON is stable
    /// -- the hub compares the stored block against the reported one to decide whether to
    /// write, and an unstable order would make every heartbeat look like a change.</summary>
    public IReadOnlyList<string> Commands { get; }

    /// <summary>Non-command abilities. Empty today, and an EMPTY LIST is not the same claim as
    /// a missing key: the hub reads an absent list as "has not said" and permits everything,
    /// while an empty one is a statement. Both are correct in their place, so this always
    /// sends a list rather than omitting the key when there is nothing in it.</summary>
    public IReadOnlyList<string> Features { get; }

    public AgentCapabilities(string platform, IEnumerable<string> commands,
        IEnumerable<string>? features = null)
    {
        Platform = platform;
        Commands = commands.Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray();
        Features = (features ?? Array.Empty<string>())
            .Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray();
    }

    /// <summary>This device's report, taken from the dispatcher that will have to answer for
    /// it. Pass the same dispatcher the command loop uses -- one built from a different
    /// executor set would describe an agent that does not exist.</summary>
    public static AgentCapabilities For(CommandDispatcher dispatcher,
        IEnumerable<string>? features = null)
        => new(PlatformAndroid, dispatcher.Implemented, features);

    /// <summary>The heartbeat block. Field names are the hub's, and changing one here silently
    /// stops the report being understood -- capabilities.clean_report reads `platform`,
    /// `commands` and `features` and quietly drops anything else.</summary>
    public JsonObject ToJson()
    {
        var commands = new JsonArray();
        foreach (var name in Commands) commands.Add(name);
        var features = new JsonArray();
        foreach (var name in Features) features.Add(name);
        return new JsonObject
        {
            ["platform"] = Platform,
            ["commands"] = commands,
            ["features"] = features,
        };
    }
}
