using TempMonitorAgent.Bios;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The decisions behind an <c>update_bios</c> flash (roadmap #9).
///
/// Nothing here flashes anything, and nothing can: a build machine is not a Dell, and the
/// payload is the manufacturer's own executable. What IS ours to get right, and what these
/// assert, is everything that happens before and after that executable runs -- the refusals,
/// the password never reaching a command line where a vendor offers a file, and an exit code
/// read without inventing a success.
///
/// The sharpest one is <c>Reboot_required_is_a_success</c>. A flash finishes during POST, so
/// the tool's "restart needed" code is the NORMAL outcome; treating it as failure would mark
/// every successful update red, which is the same trap 3010 is for installers.
/// </summary>
public class FirmwareFlasherTests
{
    private static readonly string[] Models = ["Latitude 5540", "Latitude 5550"];

    // ------------------------------------------------------------------ hardware

    [Fact]
    public void A_matching_machine_is_allowed()
    {
        Assert.Null(FirmwareFlasher.CheckHardware("Dell Inc.", "Latitude 5540", "Dell Inc.",
                                                  Models));
    }

    [Fact]
    public void Case_and_padding_do_not_make_a_mismatch()
    {
        // Writing "Latitude 5540" and reading " latitude 5540 " is the normal case on real
        // hardware. Being strict here would refuse a machine the image is genuinely for.
        Assert.Null(FirmwareFlasher.CheckHardware(" dell inc. ", " LATITUDE 5540 ",
                                                  "Dell Inc.", Models));
    }

    [Fact]
    public void The_wrong_model_is_refused_and_the_reason_names_it()
    {
        var reason = FirmwareFlasher.CheckHardware("Dell Inc.", "OptiPlex 7010", "Dell Inc.",
                                                   Models);
        Assert.NotNull(reason);
        // The reason IS the feature: an operator can act on "reports model X, image lists Y".
        Assert.Contains("OptiPlex 7010", reason);
        Assert.Contains("Latitude 5540", reason);
    }

    [Fact]
    public void The_wrong_manufacturer_is_refused()
    {
        var reason = FirmwareFlasher.CheckHardware("HP", "Latitude 5540", "Dell Inc.", Models);
        Assert.NotNull(reason);
        Assert.Contains("HP", reason);
    }

    [Theory]
    [InlineData("", "Latitude 5540")]
    [InlineData("Dell Inc.", "")]
    public void Hardware_it_cannot_identify_is_refused_rather_than_assumed(
        string vendor, string model)
    {
        // "We could not tell" is not a match. This is the one operation with no undo, so an
        // unknown must never be spent as a yes.
        Assert.NotNull(FirmwareFlasher.CheckHardware(vendor, model, "Dell Inc.", Models));
    }

    [Fact]
    public void An_image_listing_no_models_matches_nothing()
    {
        // The hub refuses to CREATE such a payload; this is the second line of that defence,
        // because an empty list read as "any model" is how an image reaches a board it was
        // not built for.
        Assert.NotNull(FirmwareFlasher.CheckHardware("Dell Inc.", "Latitude 5540", "Dell Inc.",
                                                     []));
    }

    // ------------------------------------------------------------------ power

    [Fact]
    public void Mains_power_passes()
    {
        Assert.Null(FirmwareFlasher.CheckPower(new FirmwareFlasher.PowerState(false, 90),
                                               requireAc: true, minBatteryPercent: 30));
    }

    [Fact]
    public void Running_on_battery_is_refused_when_mains_is_required()
    {
        var reason = FirmwareFlasher.CheckPower(new FirmwareFlasher.PowerState(true, 95),
                                                requireAc: true, minBatteryPercent: 0);
        Assert.NotNull(reason);
        Assert.Contains("battery", reason);
    }

    [Fact]
    public void An_unknown_power_state_is_refused_not_assumed_to_be_mains()
    {
        // A laptop whose battery class is missing or throwing is exactly the machine this
        // check exists for. Reading the unknown as "plugged in" would spend the guess that
        // cannot be taken back.
        Assert.NotNull(FirmwareFlasher.CheckPower(new FirmwareFlasher.PowerState(null, null),
                                                  requireAc: true, minBatteryPercent: 0));
    }

    [Fact]
    public void An_unknown_power_state_passes_when_mains_is_not_required()
    {
        Assert.Null(FirmwareFlasher.CheckPower(new FirmwareFlasher.PowerState(null, null),
                                               requireAc: false, minBatteryPercent: 30));
    }

    [Fact]
    public void A_flat_battery_is_refused_even_on_mains()
    {
        // Plugged in at the desk with an empty battery is still a risk if the power is pulled
        // during the restart, which is when the flash actually happens.
        var reason = FirmwareFlasher.CheckPower(new FirmwareFlasher.PowerState(false, 11),
                                                requireAc: true, minBatteryPercent: 30);
        Assert.NotNull(reason);
        Assert.Contains("11%", reason);
    }

    [Fact]
    public void A_desktop_with_no_battery_reading_is_not_refused_on_charge()
    {
        Assert.Null(FirmwareFlasher.CheckPower(new FirmwareFlasher.PowerState(false, null),
                                               requireAc: true, minBatteryPercent: 30));
    }

    // ------------------------------------------------------------------ command line

    [Fact]
    public void Operator_arguments_beat_the_vendor_default()
    {
        // The payload carries whatever the operator's own vendor documentation says, and it
        // always wins -- the defaults are convenience, not knowledge.
        var plan = FirmwareFlasher.BuildPlan("Dell Inc.", @"C:\stage\bios.exe", "/q /r=off",
                                             null, null);
        Assert.Equal(@"C:\stage\bios.exe", plan.FileName);
        Assert.Equal("/q /r=off", plan.Arguments);
    }

    [Fact]
    public void Each_vendor_gets_its_own_silent_switch_by_default()
    {
        Assert.Equal("/s", FirmwareFlasher.BuildPlan("Dell Inc.", "x.exe", null, null, null)
                                          .Arguments);
        Assert.Equal("-s", FirmwareFlasher.BuildPlan("HP", "x.exe", "", null, null).Arguments);
        Assert.Equal("-s", FirmwareFlasher.BuildPlan("LENOVO", "x.exe", null, null, null)
                                          .Arguments);
    }

    [Fact]
    public void A_file_backed_password_never_reaches_the_command_line()
    {
        // HP and Lenovo read the setup password from a file, so the value stays out of the
        // process list -- which anything running locally can read for the life of the flash.
        var plan = FirmwareFlasher.BuildPlan("HP", "x.exe", null, "hunter2", @"C:\stage\a.pw");
        Assert.Contains(@"C:\stage\a.pw", plan.Arguments);
        Assert.DoesNotContain("hunter2", plan.Arguments);
        Assert.True(FirmwareFlasher.NeedsPasswordFile("HP"));
    }

    [Fact]
    public void Dell_has_no_password_file_form_and_the_cost_is_explicit()
    {
        // Documented rather than hidden: Dell's updater takes the password inline, so it IS
        // in the command line for the duration. NeedsPasswordFile says so, which is what lets
        // the executor skip writing a file that would never be read.
        var plan = FirmwareFlasher.BuildPlan("Dell Inc.", "x.exe", null, "hunter2", null);
        Assert.Contains("/p=hunter2", plan.Arguments);
        Assert.False(FirmwareFlasher.NeedsPasswordFile("Dell Inc."));
    }

    [Fact]
    public void A_machine_with_no_password_gets_no_password_switch()
    {
        var plan = FirmwareFlasher.BuildPlan("HP", "x.exe", null, null, null);
        Assert.Equal("-s", plan.Arguments);
    }

    [Fact]
    public void An_unknown_vendor_still_flashes_with_the_operators_own_arguments()
    {
        // "We do not know this vendor's switch" must not become "this machine cannot be
        // updated" -- the operator supplied the command line for exactly this case.
        var plan = FirmwareFlasher.BuildPlan("Acme Computers", "x.exe", "/quiet", "pw", null);
        Assert.Equal("/quiet", plan.Arguments);
    }

    [Fact]
    public void The_file_token_is_substituted_for_vendors_that_want_it_named()
    {
        var plan = FirmwareFlasher.BuildPlan("HP", @"C:\stage\b.exe", "-s -f{file}", null,
                                             null);
        Assert.Equal(@"-s -fC:\stage\b.exe", plan.Arguments);
    }

    // ------------------------------------------------------------------ exit codes

    [Fact]
    public void A_clean_exit_is_staged()
    {
        Assert.Null(FirmwareFlasher.ClassifyExit("Dell Inc.", 0));
        Assert.Null(FirmwareFlasher.ClassifyExit("HP", 0));
    }

    [Theory]
    [InlineData("Dell Inc.", 2)]
    [InlineData("Dell Inc.", 6)]
    [InlineData("HP", 3010)]
    [InlineData("LENOVO", 3010)]
    public void Reboot_required_is_a_success(string vendor, int code)
    {
        // The whole point: the flash HAPPENS during the restart, so "restart needed" is the
        // normal successful outcome and not a failure to report.
        Assert.Null(FirmwareFlasher.ClassifyExit(vendor, code));
    }

    [Fact]
    public void A_real_failure_is_reported_with_its_code()
    {
        var reason = FirmwareFlasher.ClassifyExit("Dell Inc.", 1);
        Assert.NotNull(reason);
        Assert.Contains("1", reason);
    }

    [Fact]
    public void A_dell_only_reboot_code_is_not_assumed_for_other_vendors()
    {
        // 2 means "staged, restart needed" to a Dell Update Package and nothing of the sort
        // elsewhere. Generalising it would report an unknown HP failure as a success.
        Assert.NotNull(FirmwareFlasher.ClassifyExit("HP", 2));
    }
}
