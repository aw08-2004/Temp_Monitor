using FleetHubAgent.State;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// Catches the rename that produces two machines in the console instead of one renamed
/// machine.
///
/// A rename adopted in memory but not persisted is invisible for as long as the process lives:
/// the device reports under the new name, the console looks right, and the operator moves on.
/// Android then kills the app -- which it does routinely, without warning -- and the device
/// comes back reporting the OLD name. The hub has now seen both, days apart, and the second
/// entry appears with no cause anyone can trace back to the rename.
///
/// So the ordering in MachineNameProvider.TrySet is persist-then-adopt, and these tests are
/// what stop it being flipped back to the more natural-looking adopt-then-persist.
/// </summary>
public class MachineNameProviderTests
{
    private static MachineNameProvider Build(FakeStateStore store, string derived = "Pixel-9-abcd1234") =>
        new(new AgentState(store), derived);

    [Fact]
    public void Starts_on_the_derived_default_when_nothing_is_stored()
    {
        var provider = Build(new FakeStateStore());
        Assert.Equal("Pixel-9-abcd1234", provider.Current);
        Assert.True(provider.IsDefault);
    }

    [Fact]
    public void Adopts_a_stored_override_on_construction()
    {
        var store = new FakeStateStore();
        store.Set(StateKeys.MachineNameOverride, "workshop-tablet-3");

        var provider = Build(store);
        Assert.Equal("workshop-tablet-3", provider.Current);
        Assert.False(provider.IsDefault);
    }

    [Fact]
    public void A_stored_name_that_is_no_longer_valid_loses_to_the_derived_default()
    {
        // Written by an older build, or restored from a backup taken on another device. Sending
        // it would earn a 400 from /api/report on every tick and the device would go quiet with
        // no local symptom.
        var store = new FakeStateStore();
        store.Set(StateKeys.MachineNameOverride, "bad<name>");

        Assert.Equal("Pixel-9-abcd1234", Build(store).Current);
    }

    [Fact]
    public void A_rename_that_cannot_be_persisted_is_not_adopted()
    {
        var store = new FakeStateStore { WritesFail = true };
        var provider = Build(store);

        Assert.False(provider.TrySet("workshop-tablet-3", out var error));
        Assert.Equal("Pixel-9-abcd1234", provider.Current);
        Assert.Contains("could not be saved", error);
    }

    [Fact]
    public void A_persisted_rename_survives_a_restart()
    {
        var store = new FakeStateStore();
        Assert.True(Build(store).TrySet("workshop-tablet-3", out _));

        // A second provider over the same store is what the next process launch sees.
        Assert.Equal("workshop-tablet-3", Build(store).Current);
    }

    [Fact]
    public void An_invalid_name_is_refused_without_touching_the_store()
    {
        var store = new FakeStateStore();
        var provider = Build(store);

        Assert.False(provider.TrySet("tab<script>", out var error));
        Assert.Equal(0, store.WriteCount);
        Assert.Contains("not a usable machine name", error);
    }

    [Fact]
    public void Renaming_to_the_current_name_succeeds_without_a_write()
    {
        var store = new FakeStateStore();
        var provider = Build(store);

        Assert.True(provider.TrySet("Pixel-9-abcd1234", out _));
        Assert.Equal(0, store.WriteCount);
    }

    [Fact]
    public void A_name_is_trimmed_before_it_is_stored()
    {
        var store = new FakeStateStore();
        var provider = Build(store);

        Assert.True(provider.TrySet("  workshop-tablet-3  ", out _));
        Assert.Equal("workshop-tablet-3", provider.Current);
    }
}
