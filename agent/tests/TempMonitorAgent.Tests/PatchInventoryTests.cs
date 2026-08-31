using System.Text.Json.Nodes;
using TempMonitorAgent.Patch;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The two halves of roadmap #14 that can be pinned exactly: the winget table parse, and the
/// wire payload the hub ingests.
///
/// Neither talks to Windows Update or winget. The COM search and the process launch are
/// environment-dependent by nature (WingetLocatorTests makes the same trade), but the parse
/// and the payload are pure -- and they are also the two places a mistake is SILENT. A
/// mis-sliced winget row puts a phantom update in front of an operator to approve; a payload
/// that drops its empty list means no patch run ever gets confirmed.
/// </summary>
public class PatchInventoryTests
{
    // Real `winget upgrade` output, headers and column padding intact. The column starts are
    // whatever winget chose for THIS data, which is the point -- a parser with fixed offsets
    // passes on the machine it was written against and mis-slices everywhere else.
    private const string RealOutput = """
Name                           Id                            Version      Available    Source
-------------------------------------------------------------------------------------------
Mozilla Firefox                Mozilla.Firefox               140.0.4      141.0        winget
7-Zip                          7zip.7zip                     24.09        25.00        winget
Notepad++ (64-bit)             Notepad++.Notepad++           8.7.1        8.8.1        winget
3 upgrades available.
""";

    [Fact]
    public void Parses_every_row_of_a_real_table()
    {
        var rows = WingetUpgradeParser.Parse(RealOutput);
        Assert.Equal(3, rows.Count);
        Assert.Equal("winget:mozilla.firefox", rows[0].Uid);
        Assert.Equal("Mozilla.Firefox", rows[0].NativeId);
        Assert.Equal(PatchSources.Winget, rows[0].Source);
    }

    [Fact]
    public void Keeps_a_package_name_that_contains_spaces_out_of_the_id()
    {
        // The failure this catches: slicing on whitespace instead of the header's column
        // starts, so "Notepad++ (64-bit)" bleeds into the id and the package can never be
        // matched, approved or installed.
        var rows = WingetUpgradeParser.Parse(RealOutput);
        var notepad = rows.Single(r => r.NativeId == "Notepad++.Notepad++");
        Assert.DoesNotContain(' ', notepad.NativeId);
        Assert.Contains("Notepad++ (64-bit)", notepad.Title);
    }

    [Fact]
    public void Title_carries_both_versions_so_an_operator_can_see_the_jump()
    {
        var rows = WingetUpgradeParser.Parse(RealOutput);
        Assert.Contains("140.0.4", rows[0].Title);
        Assert.Contains("141.0", rows[0].Title);
    }

    [Fact]
    public void Uid_does_not_change_when_a_new_version_appears()
    {
        // An approval says "Firefox may update". Folding the version into the uid would
        // silently un-approve the package the day a new build lands, which reads to an
        // operator as the approval having been forgotten.
        var later = RealOutput.Replace("141.0", "142.0");
        var before = WingetUpgradeParser.Parse(RealOutput)[0].Uid;
        var after = WingetUpgradeParser.Parse(later)[0].Uid;
        Assert.Equal(before, after);
    }

    [Fact]
    public void Ignores_the_trailing_summary_line()
    {
        var rows = WingetUpgradeParser.Parse(RealOutput);
        Assert.DoesNotContain(rows, r => r.Title.Contains("upgrades available"));
    }

    [Fact]
    public void Stops_at_the_packages_with_no_available_upgrade()
    {
        // winget lists these under their own heading. They have no upgrade, and offering them
        // as if they did would put un-installable rows in the approval queue.
        var withTail = RealOutput + """

2 package(s) have version numbers that cannot be determined. The following packages cannot be upgraded from winget:
Name                           Id                            Version      Available    Source
Weird Thing                    Weird.Thing                   1.0          Unknown      winget
""";
        var rows = WingetUpgradeParser.Parse(withTail);
        Assert.DoesNotContain(rows, r => r.NativeId == "Weird.Thing");
        Assert.Equal(3, rows.Count);
    }

    [Fact]
    public void Empty_and_garbage_input_yield_nothing_rather_than_throwing()
    {
        Assert.Empty(WingetUpgradeParser.Parse(null));
        Assert.Empty(WingetUpgradeParser.Parse(""));
        Assert.Empty(WingetUpgradeParser.Parse("   \n  \n"));
        Assert.Empty(WingetUpgradeParser.Parse("Failed when searching source; results will "
                                               + "not be included: msstore"));
    }

    [Fact]
    public void No_upgrades_is_an_empty_list_not_a_failure()
    {
        var none = """
Name    Id    Version    Available    Source
--------------------------------------------
""";
        Assert.Empty(WingetUpgradeParser.Parse(none));
    }

    // ---- the wire payload ---------------------------------------------------------------

    [Fact]
    public void Payload_always_carries_an_updates_array()
    {
        // THE test in this file. A machine with nothing available must still send a payload
        // with an `updates` key, because "I am offering no updates" is the only honest
        // evidence an install worked -- it is what closes a patch run out on the hub. If this
        // ever became a bare array, or were omitted when empty, every successful patch run
        // would sit in REBOOTING until its confirm timeout recorded it as a failure.
        var payload = PatchInventoryReporter.ToPayload(
            new PatchScanner.Scan([], ""));
        Assert.True(payload.ContainsKey("updates"));
        Assert.IsType<JsonArray>(payload["updates"]);
        Assert.Empty(payload["updates"]!.AsArray());
    }

    [Fact]
    public void Payload_field_names_match_what_the_hub_ingests()
    {
        // The C# scanner and hub/patches.py parse_report are bound by nothing but these
        // names. A rename on one side is silent on the other: the hub drops the entry and the
        // machine simply reports no updates, forever.
        var scan = new PatchScanner.Scan([
            new AvailableUpdate("windows_update:kb5060842", "some-guid",
                                PatchSources.WindowsUpdate, "KB5060842",
                                "Cumulative Update", "security", true, 1234)
        ], "");
        var row = PatchInventoryReporter.ToPayload(scan)["updates"]!.AsArray()[0]!.AsObject();
        Assert.Equal("windows_update:kb5060842", (string?)row["uid"]);
        Assert.Equal("windows_update", (string?)row["source"]);
        Assert.Equal("KB5060842", (string?)row["kb"]);
        Assert.Equal("Cumulative Update", (string?)row["title"]);
        Assert.Equal("security", (string?)row["classification"]);
        Assert.True((bool?)row["reboot_required"]);
        Assert.Equal(1234, (long?)row["size_bytes"]);
    }

    [Fact]
    public void Payload_reports_a_scan_error_without_losing_the_updates_it_did_get()
    {
        // Windows Server has no winget and never will; a machine pointed at a downed WSUS
        // cannot search Windows Update. Either is normal somewhere in a fleet, and one
        // failing must not discard what the other found.
        var scan = new PatchScanner.Scan([
            new AvailableUpdate("winget:mozilla.firefox", "Mozilla.Firefox",
                                PatchSources.Winget, "", "Firefox", "unknown", false, 0)
        ], "Windows Update refused the search: 0x8024402C");
        var payload = PatchInventoryReporter.ToPayload(scan);
        Assert.Single(payload["updates"]!.AsArray());
        Assert.Contains("0x8024402C", (string?)payload["error"]);
    }

    // ---- Windows Update mapping ---------------------------------------------------------
    //
    // WindowsUpdateApi is late-bound, which means any object with the right member names
    // stands in for a COM update. That is the one upside of not having an interop assembly,
    // and it makes the classification rules -- the part with real judgement in it -- testable
    // with no Windows Update Agent involved.

    // An INDEXER, not a method called Item. A C# indexer compiles to a property named `Item`,
    // which is exactly what WindowsUpdateApi.Index reaches for -- and it is what a real
    // IUpdateCollection exposes. A method of the same name would not be reached the same way,
    // so the fake would be testing a path the COM object never takes.
    private sealed class FakeCategories(params string[] names)
    {
        public int Count => names.Length;
        public object this[int i] => new FakeCategory(names[i]);
    }

    private sealed class FakeCategory(string name)
    {
        public string Name => name;
    }

    [Fact]
    public void A_security_update_is_classified_security_whatever_else_it_also_is()
    {
        // An update carries several categories at once. Taking the first match would file a
        // security update under "Updates" about half the time -- and that is the one
        // classification an operator auto-approves, so it would quietly stop being applied.
        Assert.Equal("security", WindowsUpdateApi.Classify(
            new FakeCategories("Windows 11", "Updates", "Security Updates")));
        Assert.Equal("security", WindowsUpdateApi.Classify(
            new FakeCategories("Security Updates")));
    }

    [Fact]
    public void Drivers_and_feature_updates_are_kept_distinct_from_ordinary_updates()
    {
        // Both are excluded from auto-approval on the hub, so mapping either onto "other"
        // would hand a scheduler exactly the two kinds nobody wanted it to have.
        Assert.Equal("driver", WindowsUpdateApi.Classify(new FakeCategories("Drivers")));
        Assert.Equal("feature", WindowsUpdateApi.Classify(new FakeCategories("Feature Packs")));
        Assert.Equal("critical", WindowsUpdateApi.Classify(
            new FakeCategories("Critical Updates")));
        Assert.Equal("other", WindowsUpdateApi.Classify(new FakeCategories("Update Rollups")));
    }

    [Fact]
    public void An_unrecognised_category_is_unknown_rather_than_other()
    {
        Assert.Equal("unknown", WindowsUpdateApi.Classify(new FakeCategories("Klingon Packs")));
        Assert.Equal("unknown", WindowsUpdateApi.Classify(null));
    }

    private sealed class FakeBehavior(int reboot)
    {
        public int RebootBehavior => reboot;
    }

    [Fact]
    public void Can_request_a_reboot_counts_as_requiring_one()
    {
        // 0 = never, 1 = always, 2 = can request. Treating 2 as "no" risks reporting an
        // update installed when it is waiting on a restart -- the exact failure this feature
        // exists to prevent. An unnecessary restart is the cheaper mistake.
        Assert.False(WindowsUpdateApi.RebootBehaviour(new FakeBehavior(0)));
        Assert.True(WindowsUpdateApi.RebootBehaviour(new FakeBehavior(1)));
        Assert.True(WindowsUpdateApi.RebootBehaviour(new FakeBehavior(2)));
        Assert.False(WindowsUpdateApi.RebootBehaviour(null));
    }

    // ---- the reboot decision -------------------------------------------------------------

    [Fact]
    public void If_required_follows_what_the_install_reported()
    {
        // The default policy, and the one that ties the exit-code fix below to an actual
        // restart: a winget package that exited 3010 must end with a reboot scheduled, or the
        // update stays staged and the hub waits out its confirm timeout on a machine that was
        // never going to come back.
        Assert.True(InstallPatchesExecutor.ShouldRestart("if_required", true));
        Assert.False(InstallPatchesExecutor.ShouldRestart("if_required", false));
    }

    [Fact]
    public void Always_and_never_override_what_the_install_reported()
    {
        Assert.True(InstallPatchesExecutor.ShouldRestart("always", false));
        Assert.False(InstallPatchesExecutor.ShouldRestart("never", true));
    }

    [Fact]
    public void An_unrecognised_policy_falls_back_to_if_required()
    {
        // Not to always, and not to never. A garbled policy must not silently turn a fleet's
        // restarts on (rebooting PCs nobody asked to reboot) or off (updates that never
        // finish), so it lands on the same answer the hub defaults to.
        Assert.True(InstallPatchesExecutor.ShouldRestart("nonsense", true));
        Assert.False(InstallPatchesExecutor.ShouldRestart("nonsense", false));
        Assert.True(InstallPatchesExecutor.ShouldRestart(null, true));
        Assert.False(InstallPatchesExecutor.ShouldRestart("", false));
        Assert.True(InstallPatchesExecutor.ShouldRestart("  ALWAYS  ", false));
    }

    // ---- winget exit codes and package ids ----------------------------------------------

    [Fact]
    public void Reboot_required_is_a_successful_install()
    {
        // 3010 is ERROR_SUCCESS_REBOOT_REQUIRED and winget passes the wrapped installer's
        // code through unchanged. Treating it as failure marks a package that installed
        // perfectly as failed and spends a retry on a machine with nothing wrong with it --
        // the same trap packages.DEFAULT_SUCCESS_EXIT_CODES documents on the hub side.
        Assert.Contains(0, PatchInstaller.WingetSuccessCodes);
        Assert.Contains(3010, PatchInstaller.WingetSuccessCodes);
        Assert.Contains(1641, PatchInstaller.WingetSuccessCodes);
        Assert.DoesNotContain(1, PatchInstaller.WingetSuccessCodes);
    }

    [Fact]
    public void The_codes_that_mean_success_and_the_ones_that_owe_a_restart_are_not_the_same()
    {
        // "it worked" and "it needs a reboot to finish" are different facts, and the
        // executor's if_required policy reads only the second. A winget-only batch whose
        // reboot never propagated would sit half-applied with nothing scheduling the restart.
        Assert.Contains(3010, PatchInstaller.WingetRebootCodes);
        Assert.Contains(1641, PatchInstaller.WingetRebootCodes);
        Assert.DoesNotContain(0, PatchInstaller.WingetRebootCodes);
        Assert.True(PatchInstaller.WingetRebootCodes
                        .IsSubsetOf(PatchInstaller.WingetSuccessCodes),
                    "a code that owes a restart must also count as an install");
    }

    [Fact]
    public void Real_package_ids_are_accepted()
    {
        Assert.True(WingetPackageId.IsSafe("Mozilla.Firefox"));
        Assert.True(WingetPackageId.IsSafe("7zip.7zip"));
        Assert.True(WingetPackageId.IsSafe("Notepad++.Notepad++"));
        Assert.True(WingetPackageId.IsSafe("Microsoft.VCRedist.2015+.x64"));
        Assert.True(WingetPackageId.IsSafe("Some-Vendor_Thing.1"));
    }

    [Fact]
    public void Anything_that_could_reach_a_command_line_is_refused()
    {
        // This value is reflected text from an external tool and ends up in a command line,
        // so the guard is an allow-list rather than an escape -- the bug class
        // Files/OpenItemExecutor.cs names CVE-2024-27980 for. A literal-space test was the
        // old check and was not enough: tab and quote are equally significant to
        // CommandLineToArgvW.
        Assert.False(WingetPackageId.IsSafe("Evil.Thing\" --uninstall \"x"));
        Assert.False(WingetPackageId.IsSafe("Evil\tThing"));
        Assert.False(WingetPackageId.IsSafe("Evil Thing"));
        Assert.False(WingetPackageId.IsSafe(@"Evil\Thing"));
        Assert.False(WingetPackageId.IsSafe("Evil&calc"));
        Assert.False(WingetPackageId.IsSafe("Evil|calc"));
        Assert.False(WingetPackageId.IsSafe(null));
        Assert.False(WingetPackageId.IsSafe("   "));
        Assert.False(WingetPackageId.IsSafe(new string('a', 201)));
    }

    [Fact]
    public void The_parser_drops_a_row_whose_id_would_be_refused_downstream()
    {
        // The parser and the installer must agree about what an id is: one admitting what the
        // other refuses would either lose real updates or put external text on a command line.
        var hostile = """
Name                           Id                            Version      Available    Source
-----------------------------------------------------------------------------------------------
Evil Thing                     Evil"Thing                    1.0          2.0          winget
Mozilla Firefox                Mozilla.Firefox               140.0.4      141.0        winget
""";
        var rows = WingetUpgradeParser.Parse(hostile);
        Assert.Single(rows);
        Assert.Equal("Mozilla.Firefox", rows[0].NativeId);
        Assert.All(rows, r => Assert.True(WingetPackageId.IsSafe(r.NativeId)));
    }

    private sealed class FakeKbs(params string[] ids)
    {
        public int Count => ids.Length;
        public object this[int i] => ids[i];
    }

    [Fact]
    public void Kb_numbers_are_read_as_digits_however_they_are_written()
    {
        Assert.Equal("5060842", WindowsUpdateApi.FirstKb(new FakeKbs("5060842")));
        Assert.Equal("5060842", WindowsUpdateApi.FirstKb(new FakeKbs("KB5060842")));
        Assert.Equal("", WindowsUpdateApi.FirstKb(new FakeKbs()));
        Assert.Equal("", WindowsUpdateApi.FirstKb(null));
    }
}
