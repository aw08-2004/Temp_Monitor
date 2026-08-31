namespace TempMonitorAgent.State;

/// <summary>
/// Release channel names, and the rule that anything else is stable.
///
/// <para>Mirrors <c>hub/channels.py</c>. The two sides share nothing but these strings, so
/// they are spelled here once rather than inlined at each use — a channel name is compared
/// against a value that arrived over the wire, and a typo would silently pin a machine to
/// stable forever while the console showed it as beta.</para>
///
/// <para><b>Names only. There are no URLs in this file, and that is deliberate.</b> The hub
/// sends a channel name; <see cref="AgentConfig.UpdateManifestUrl"/> turns it into one of two
/// compiled-in urls. Keeping the mapping there rather than here means the trust decision
/// lives beside the trust root it protects.</para>
/// </summary>
public static class Channels
{
    public const string Stable = "stable";
    public const string Beta = "beta";

    /// <summary>A usable channel name, defaulting to stable.
    ///
    /// Never throws. Every channel value the agent reads — from a heartbeat, from the
    /// persisted config file written by an older build — goes through here, so an
    /// unrecognised or absent value degrades to the safe train. A hub too old to send the
    /// field sends nothing, which lands here as null and reads as stable, exactly like the
    /// watch flags that predate it.</summary>
    public static string Normalize(string? value)
    {
        var text = value?.Trim().ToLowerInvariant();
        return text == Beta ? Beta : Stable;
    }
}
