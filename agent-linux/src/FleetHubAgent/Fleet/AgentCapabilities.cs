using System.Text.Json.Nodes;

namespace FleetHubAgent.Fleet;

/// <summary>
/// What this agent can be told to do, in the shape the hub's `capabilities` heartbeat block
/// expects (hub/capabilities.py).
///
/// **A version number cannot say "not this platform".** The console decides what to offer from
/// MIN_*_AGENT constants that hardcode the agent minor a feature arrived in, which works while
/// every managed machine is a Windows PC on one release train. It cannot express "this machine
/// runs scripts but will never have a ConPTY terminal", and on Linux several of those are
/// platform facts rather than backlog positions. So the agent states its abilities and the hub
/// stores them.
///
/// **This is also what lets the version number mean something again.** Until the hub read a
/// platform, `companion_version` was the only thing it had, and it read every value as though
/// it belonged to the Windows train -- so this agent was pinned at 0.1.0 purely to be ignored
/// (see AgentConfig.Version). Reporting a platform is what replaces that arrangement: the hub
/// now picks a manifest, an outdated tally and a set of console gates by what a machine IS.
///
/// **The command list is DERIVED from the dispatcher, never written by hand.** A hand-kept
/// list drifts the first time an executor is added, and it drifts in the direction that hurts:
/// the hub goes on refusing a command this agent has just learned to run, and the only symptom
/// is a button that stays grey.
///
/// **Two lists, because they travel on different channels.** <see cref="Commands"/> are command
/// types, which arrive through the command queue and are what the hub's create_command checks.
/// <see cref="Features"/> are everything that is not a command. This agent claims none today,
/// and an EMPTY list is not the same claim as a missing key -- the hub reads an absent list as
/// "has not said" and permits everything, while an empty one is a statement.
///
/// Sent on every heartbeat rather than only when it changes. The block is a hundred bytes and
/// never changes in normal operation, and the hub writes only when the content differs -- so
/// keeping the "have I sent this" state on the HUB rather than here is what makes a machine
/// re-report itself after the hub restores its database from a backup.
/// </summary>
public sealed class AgentCapabilities
{
    /// <summary>The platform slug. Must be one of the values hub/capabilities.py's PLATFORMS
    /// tuple accepts -- anything else is stored there as unknown, which reads as "has not
    /// said" and quietly turns off every gate this class exists to feed. It is also what
    /// hub/channels.py keys the release manifest on, so a wrong value here means this machine
    /// is offered the Windows agent's build.</summary>
    public const string PlatformLinux = "linux";

    public string Platform { get; }

    /// <summary>The command types this agent will actually route, sorted so the JSON is stable
    /// -- the hub compares the stored block against the reported one to decide whether to
    /// write, and an unstable order would make every heartbeat look like a change.</summary>
    public IReadOnlyList<string> Commands { get; }

    /// <summary>Non-command abilities. Empty today. See the class summary on why an empty list
    /// is sent rather than the key being omitted.</summary>
    public IReadOnlyList<string> Features { get; }

    public AgentCapabilities(string platform, IEnumerable<string> commands,
        IEnumerable<string>? features = null)
    {
        Platform = platform;
        Commands = commands.Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray();
        Features = (features ?? Array.Empty<string>())
            .Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToArray();
    }

    /// <summary>This machine's report, taken from the dispatcher that will have to answer for
    /// it. Pass the same dispatcher the command loop uses -- one built from a different
    /// executor set would describe an agent that does not exist.</summary>
    public static AgentCapabilities For(CommandDispatcher dispatcher,
        IEnumerable<string>? features = null)
        => new(PlatformLinux, dispatcher.Implemented, features);

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
