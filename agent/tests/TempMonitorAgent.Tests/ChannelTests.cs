using TempMonitorAgent.State;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Release channels (roadmap #21): the name → url mapping, and the allow-list that stops the
/// hub choosing a url of its own.
///
/// The second half is the one that matters. `RuntimeConfig`'s type docs say the hub may never
/// redirect where the agent gets its code, and the enforcement is that `Apply()` copies named
/// fields off an allow-list rather than filtering a deny-list. That is a property nothing
/// else checks: adding a trust-bearing key to the payload does not fail a build, does not
/// throw, and does not log — it would simply start working. These tests are what turn that
/// into a failure somebody sees.
/// </summary>
public class ChannelTests
{
    [Fact]
    public void Known_channel_names_round_trip()
    {
        Assert.Equal(Channels.Stable, Channels.Normalize("stable"));
        Assert.Equal(Channels.Beta, Channels.Normalize("beta"));
        Assert.Equal(Channels.Beta, Channels.Normalize("  BETA "));
    }

    [Fact]
    public void Anything_unrecognised_reads_as_stable()
    {
        // A hub too old to send the field sends nothing; a typo sends a word nobody defined.
        // Both must land on the safe train rather than on "no channel", which has no url.
        Assert.Equal(Channels.Stable, Channels.Normalize(null));
        Assert.Equal(Channels.Stable, Channels.Normalize(""));
        Assert.Equal(Channels.Stable, Channels.Normalize("nightly"));
        Assert.Equal(Channels.Stable, Channels.Normalize("BETA-2"));
    }

    [Fact]
    public void Each_channel_maps_to_its_own_compiled_in_manifest()
    {
        Assert.Equal(AgentConfig.StableManifestUrl, AgentConfig.ManifestUrlFor("stable"));
        Assert.Equal(AgentConfig.BetaManifestUrl, AgentConfig.ManifestUrlFor("beta"));
        Assert.NotEqual(AgentConfig.StableManifestUrl, AgentConfig.BetaManifestUrl);
    }

    [Fact]
    public void An_unknown_channel_installs_from_stable()
    {
        // The failure this catches is the sharp one: a garbled channel resolving to an empty
        // or malformed url, which fails the update check silently and permanently. Stable is
        // the only safe answer to "I do not know".
        Assert.Equal(AgentConfig.StableManifestUrl, AgentConfig.ManifestUrlFor("nightly"));
        Assert.Equal(AgentConfig.StableManifestUrl, AgentConfig.ManifestUrlFor(null));
    }

    [Fact]
    public void Both_manifest_urls_are_on_the_repos_main_branch()
    {
        // The manifest is only read from main -- a release published anywhere else reaches
        // nothing and looks like a silent no-op. Beta differs by FILENAME, not by branch.
        Assert.Contains("/main/agent/", AgentConfig.StableManifestUrl);
        Assert.Contains("/main/agent/", AgentConfig.BetaManifestUrl);
    }

    [Fact]
    public void The_signature_url_follows_the_manifest()
    {
        Assert.Equal(AgentConfig.UpdateManifestUrl + ".sig", AgentConfig.UpdateManifestSigUrl);
    }

    // ---- the allow-list ------------------------------------------------------------------

    [Fact]
    public void A_hub_cannot_redirect_where_the_agent_gets_its_code()
    {
        // THE test in this file. RuntimeConfig.Apply copies named fields off an allow-list,
        // so a trust-bearing key in the payload is never read. If someone ever "fixes" that
        // into a deny-list, this is what fails -- and what fails otherwise is nothing, until
        // a compromised hub points a fleet at a binary of its choosing.
        var config = RuntimeConfig.Default;
        var hostile = new Dictionary<string, object?>
        {
            ["update_manifest_url"] = "https://evil.example.com/agent.manifest.json",
            ["UpdateManifestUrl"] = "https://evil.example.com/agent.manifest.json",
            ["AgentConfig.UpdatePublicKeyHex"] = "00" + new string('0', 62),
            ["hub_base"] = "https://evil.example.com",
            ["Channel"] = "beta",
        };

        var applied = config.Apply(hostile, "v1");

        // Nothing in that payload was read. Note `Channel` is in there too and is also
        // ignored: the channel does NOT travel the config block, it has its own per-machine
        // heartbeat field, so a config push must not be able to set it either.
        Assert.Equal(Channels.Stable, applied.Channel);
        Assert.Equal(AgentConfig.StableManifestUrl, AgentConfig.ManifestUrlFor(applied.Channel));
    }

    [Fact]
    public void Applying_a_config_block_never_disturbs_the_channel()
    {
        // A beta machine receiving an ordinary settings push must stay on beta. The two
        // travel separately and this is what keeps them from interfering: a config payload
        // that reset the channel would drop the whole pilot ring back to stable the next time
        // anybody changed a sensor preference.
        var onBeta = RuntimeConfig.Default with { Channel = Channels.Beta };
        var applied = onBeta.Apply(
            new Dictionary<string, object?> { ["metrics.collect_network"] = "false" }, "v2");

        Assert.Equal(Channels.Beta, applied.Channel);
        Assert.False(applied.CollectNetwork);
    }

    [Fact]
    public void The_default_config_is_on_stable()
    {
        // A brand-new install, and any machine whose config.json predates this feature,
        // starts on stable rather than on "" -- which would resolve to stable anyway, but
        // only by accident rather than by declaration.
        Assert.Equal(Channels.Stable, RuntimeConfig.Default.Channel);
    }
}
