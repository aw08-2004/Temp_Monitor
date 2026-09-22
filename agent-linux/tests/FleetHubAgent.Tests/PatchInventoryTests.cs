using FleetHubAgent.Patch;

namespace FleetHubAgent.Tests;

/// <summary>
/// Covers the two halves of the patch inventory that fail without saying anything: the parse,
/// and the shape of the payload.
///
/// The parse is one end of a contract whose other end is Python. A misparse does not raise --
/// it reports a machine as missing nothing, which is indistinguishable in the console from a
/// machine that is fully patched. That is the worst answer this feature can give, because it
/// is the one nobody goes to look at.
///
/// The payload shape is the same class of failure one layer up. hub/patches.py tests
/// `data.get("patches") is not None` precisely so that an EMPTY update list still counts as a
/// report -- an update that stops being offered is the only evidence the hub ever gets that
/// it installed. A payload that degraded to a bare array, or that was omitted when empty,
/// would leave every patch run open until it timed out and was recorded as a failure.
/// </summary>
public class PatchInventoryTests
{
    // ---------------------------------------------------------------- apt

    private const string AptOutput = """
        NOTE: This is only a simulation!
              apt-get needs root privileges for real execution.
        Reading package lists...
        Building dependency tree...
        The following packages will be upgraded:
          libssl3 openssh-server linux-image-generic
        3 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.
        Inst libssl3 [3.0.2-0ubuntu1.12] (3.0.2-0ubuntu1.15 Ubuntu:22.04/jammy-updates, Ubuntu:22.04/jammy-security [amd64])
        Conf libssl3 (3.0.2-0ubuntu1.15 Ubuntu:22.04/jammy-updates [amd64])
        Inst openssh-server [1:8.9p1-3] (1:8.9p1-3ubuntu0.6 Ubuntu:22.04/jammy-updates [amd64])
        Inst linux-image-generic [5.15.0.86.83] (5.15.0.91.88 Ubuntu:22.04/jammy-updates [amd64])
        Conf openssh-server (1:8.9p1-3ubuntu0.6 Ubuntu:22.04/jammy-updates [amd64])
        """;

    [Fact]
    public void Apt_reads_one_update_per_Inst_line()
    {
        var updates = PatchScanner.ParseApt(AptOutput);

        // Conf lines describe the same packages being configured. Counting them would double
        // every machine's patch count.
        Assert.Equal(new[] { "libssl3", "openssh-server", "linux-image-generic" },
            updates.Select(u => u.Uid));
        Assert.All(updates, u => Assert.Equal("apt", u.Source));
    }

    [Fact]
    public void Apt_carries_the_version_in_the_title_and_not_in_the_identity()
    {
        // The uid an operator approves is the package. If the version were part of it, the
        // approval would expire at the next upload to the archive and the row would never
        // disappear the way confirm_from_inventory needs it to.
        var update = PatchScanner.ParseApt(AptOutput).First();
        Assert.Equal("libssl3", update.Uid);
        Assert.Equal("libssl3 3.0.2-0ubuntu1.15", update.Title);
    }

    [Fact]
    public void Apt_reads_security_out_of_the_origin_list()
    {
        var updates = PatchScanner.ParseApt(AptOutput).ToDictionary(u => u.Uid);

        // Offered from -updates AND -security: apt would take it from either, and the one
        // worth telling an operator about is the security archive.
        Assert.Equal("security", updates["libssl3"].Classification);
        Assert.Equal("other", updates["openssh-server"].Classification);
    }

    [Fact]
    public void Apt_reads_a_debian_security_origin_too()
    {
        var update = Assert.Single(PatchScanner.ParseApt(
            "Inst libc6 [2.36-9+deb12u3] (2.36-9+deb12u7 Debian-Security:12/stable-security [amd64])"));
        Assert.Equal("security", update.Classification);
    }

    [Fact]
    public void Apt_treats_an_architecture_qualified_name_as_one_package()
    {
        var updates = PatchScanner.ParseApt(
            "Inst libssl3:amd64 [3.0.2] (3.0.3 Ubuntu:22.04/jammy-updates [amd64])\n" +
            "Inst libssl3 [3.0.2] (3.0.3 Ubuntu:22.04/jammy-updates [amd64])");
        var update = Assert.Single(updates);
        Assert.Equal("libssl3", update.Uid);
    }

    [Fact]
    public void Apt_reports_a_new_dependency_that_has_no_installed_version()
    {
        // A line with no [current version] bracket is a package being pulled in. It is still
        // something the machine is about to be offered.
        var update = Assert.Single(PatchScanner.ParseApt(
            "Inst libfoo1 (1.2.3 Ubuntu:22.04/jammy-updates [amd64])"));
        Assert.Equal("libfoo1", update.Uid);
        Assert.Equal("libfoo1 1.2.3", update.Title);
    }

    [Fact]
    public void A_fully_patched_machine_parses_to_nothing_rather_than_failing()
    {
        Assert.Empty(PatchScanner.ParseApt(
            "Reading package lists...\n" +
            "0 upgraded, 0 newly installed, 0 to remove and 0 not upgraded.\n"));
    }

    // ---------------------------------------------------------------- dnf

    [Fact]
    public void Dnf_reads_the_three_column_form()
    {
        var updates = PatchScanner.ParseDnf("""
            kernel.x86_64                   5.14.0-427.el9               baseos
            openssl-libs.x86_64             1:3.0.7-24.el9               appstream
            """);

        Assert.Equal(new[] { "kernel", "openssl-libs" }, updates.Select(u => u.Uid));
        Assert.Equal("kernel 5.14.0-427.el9", updates[0].Title);
        Assert.All(updates, u => Assert.Equal("dnf", u.Source));
    }

    [Fact]
    public void Dnf_says_unknown_rather_than_guessing_a_classification()
    {
        // check-update carries no advisory information. The security pass is a second query,
        // and on a machine whose repositories ship no updateinfo it answers nothing -- at
        // which point "unknown" is the honest report and "other" would be a claim.
        var update = Assert.Single(PatchScanner.ParseDnf("zlib.x86_64  1.2.11-40.el9  baseos"));
        Assert.Equal("unknown", update.Classification);
    }

    [Fact]
    public void Dnf_joins_a_wrapped_line_back_together()
    {
        // dnf breaks a long name onto its own line and indents the columns under it. Read
        // naively that is one update with no version and one orphan row.
        var updates = PatchScanner.ParseDnf("""
            some-very-long-package-name-that-wraps.noarch
                                            2.4.0-1.el9                  appstream
            zlib.x86_64                     1.2.11-40.el9                baseos
            """);

        Assert.Equal(new[] { "some-very-long-package-name-that-wraps", "zlib" },
            updates.Select(u => u.Uid));
        Assert.Equal("some-very-long-package-name-that-wraps 2.4.0-1.el9", updates[0].Title);
    }

    [Fact]
    public void Dnf_stops_at_the_obsoleting_section()
    {
        // Everything below that heading is a package being REPLACED. Offering one as an
        // update points an operator at something the machine is about to lose.
        var updates = PatchScanner.ParseDnf("""
            zlib.x86_64                     1.2.11-40.el9                baseos

            Obsoleting Packages
            grub2-tools.x86_64              1:2.06-80.el9                baseos
                grub2-tools-minimal.x86_64  1:2.06-70.el9                @System
            """);

        var update = Assert.Single(updates);
        Assert.Equal("zlib", update.Uid);
    }

    // ---------------------------------------------------------------- shared

    [Theory]
    [InlineData("linux-image-generic", true)]
    [InlineData("linux-image-5.15.0-91-generic", true)]
    [InlineData("kernel", true)]
    [InlineData("kernel-core", true)]
    [InlineData("openssl", false)]
    [InlineData("linux-libc-dev", false)]   // a header package; nothing restarts for it
    public void Only_a_kernel_package_claims_to_need_a_restart(string package, bool expected)
    {
        Assert.Equal(expected, PatchScanner.NeedsReboot(package));
    }

    // ---------------------------------------------------------------- payload

    [Fact]
    public void An_empty_scan_still_produces_an_updates_key()
    {
        // THE payload test. hub/fleet_web.py reads `data.get("patches") is not None`, so an
        // object with an empty list is a report and a missing key is silence. This transition
        // is what closes out a patch run.
        var payload = PatchInventoryReporter.ToPayload(
            new PatchScan(Array.Empty<AvailableUpdate>(), null));

        Assert.NotNull(payload["updates"]);
        Assert.Empty(payload["updates"]!.AsArray());
    }

    [Fact]
    public void The_payload_uses_the_hubs_own_field_names()
    {
        var payload = PatchInventoryReporter.ToPayload(new PatchScan(
            new[] { new AvailableUpdate("openssl", "apt", "openssl 3.0.15", "security", false) },
            null));

        var update = payload["updates"]!.AsArray()[0]!;
        // Every one of these is read by hub/patches.py parse_report. A rename here is a
        // machine that reports updates the hub silently drops.
        Assert.Equal("openssl", (string?)update["uid"]);
        Assert.Equal("apt", (string?)update["source"]);
        Assert.Equal("openssl 3.0.15", (string?)update["title"]);
        Assert.Equal("security", (string?)update["classification"]);
        Assert.False((bool?)update["reboot_required"]);
    }

    [Fact]
    public void A_failed_scan_reports_the_reason_beside_an_empty_list()
    {
        // "Nothing is available" and "I could not ask" are different facts that look
        // identical in a count. Only one of them is a reason to walk over to a machine.
        var payload = PatchInventoryReporter.ToPayload(PatchScan.Failed("dnf exited 1"));
        Assert.Empty(payload["updates"]!.AsArray());
        Assert.Equal("dnf exited 1", (string?)payload["error"]);
    }
}
