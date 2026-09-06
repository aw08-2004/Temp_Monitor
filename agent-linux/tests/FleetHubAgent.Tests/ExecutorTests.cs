using FleetHubAgent.Fleet.Executors;

namespace FleetHubAgent.Tests;

/// <summary>
/// Covers the three decisions the executors make BEFORE they touch the machine -- the ones
/// where being wrong is quiet.
///
/// A wrong shutdown time spec is the clearest case: shutdown(8) takes minutes and the hub
/// sends seconds, so "+90" is not a slow reboot, it is 90 minutes. An operator who asked for
/// 90 seconds and was told "scheduled" would find the machine still up an hour later, with
/// nothing in any log that looked like a failure.
/// </summary>
public class ShutdownSchedulingTests
{
    [Theory]
    [InlineData(0, "now")]
    [InlineData(-5, "now")]      // a hub that sent a negative delay means "go", not "go back"
    [InlineData(1, "+1")]        // anything at all rounds UP: never sooner than asked
    [InlineData(59, "+1")]
    [InlineData(60, "+1")]       // the hub's own default
    [InlineData(61, "+2")]
    [InlineData(600, "+10")]
    [InlineData(86400, "+1440")] // the hub's advertised maximum
    public void TimeSpec_rounds_up_to_whole_minutes(int seconds, string expected)
    {
        Assert.Equal(expected, ShutdownScheduling.TimeSpec(seconds).Spec);
    }

    [Fact]
    public void TimeSpec_human_form_matches_the_spec_it_reports()
    {
        // The result string is the only place an operator learns their 90 seconds became two
        // minutes, so it must not drift from the spec actually passed to shutdown(8).
        Assert.Equal("in 2 minutes", ShutdownScheduling.TimeSpec(90).Human);
        Assert.Equal("in 1 minute", ShutdownScheduling.TimeSpec(60).Human);
        Assert.Equal("immediately", ShutdownScheduling.TimeSpec(0).Human);
    }

    [Fact]
    public void Force_is_reported_as_having_no_effect()
    {
        // force is accepted and does nothing on Linux -- see ShutdownScheduling.ForceNote.
        // Silently ignoring it is the failure: an operator who ticked the box would believe
        // an unsaveable application had been forced closed.
        Assert.Contains("no effect", ShutdownScheduling.ForceNote(true));
        Assert.Equal("", ShutdownScheduling.ForceNote(false));
    }
}

/// <summary>Hostname validation, which stands between operator input and a shell-free execve.
/// The value is passed as its own argv entry so it can never become a second command; this is
/// about giving a useful refusal rather than hostnamectl's "Invalid argument".</summary>
public class HostnameValidationTests
{
    [Theory]
    [InlineData("web01")]
    [InlineData("a")]
    [InlineData("host-with-hyphens")]
    [InlineData("HOST01")]
    [InlineData("0abc")]
    public void Accepts_valid_labels(string name) =>
        Assert.True(RenameExecutor.IsValidHostname(name));

    [Theory]
    [InlineData("")]
    [InlineData("-leading")]
    [InlineData("trailing-")]
    [InlineData("has space")]
    [InlineData("has.dot")]        // a label, not an FQDN: hostnamectl's static name is one label
    [InlineData("semi;colon")]
    [InlineData("back`tick")]
    [InlineData("dollar$sign")]
    public void Rejects_invalid_labels(string name) =>
        Assert.False(RenameExecutor.IsValidHostname(name));

    [Fact]
    public void Rejects_labels_over_63_characters()
    {
        Assert.True(RenameExecutor.IsValidHostname(new string('a', 63)));
        Assert.False(RenameExecutor.IsValidHostname(new string('a', 64)));
    }
}

/// <summary>
/// Shell resolution for run_script.
///
/// The failure this guards is one of interpretation, not of crashing: the hub's `shell` enum
/// is ("powershell", "cmd") because it was written for a Windows-only fleet, and this agent
/// runs the script under a Linux shell regardless. That is only safe as long as the
/// substitution is REPORTED -- an operator who believes PowerShell ran is the hazard. So the
/// flag that drives that message is what is pinned here.
/// </summary>
public class ResolveShellTests
{
    [Theory]
    [InlineData("powershell")]
    [InlineData("pwsh")]
    [InlineData("cmd")]
    [InlineData("nonsense")]   // an unrecognised name is substituted, never executed as a path
    public void Windows_and_unknown_shells_are_flagged_as_substituted(string requested)
    {
        var (shell, substituted) = RunScriptExecutor.ResolveShell(requested);
        Assert.True(substituted, $"'{requested}' must be reported as a substitution");
        Assert.StartsWith("/", shell);
        Assert.DoesNotContain(requested, shell);
    }

    [Theory]
    [InlineData("sh")]
    [InlineData("bash")]
    [InlineData("")]           // the hub sent no shell: nothing was substituted FOR
    public void Native_and_absent_shells_are_not_flagged(string requested)
    {
        var (shell, substituted) = RunScriptExecutor.ResolveShell(requested);
        Assert.False(substituted, $"'{requested}' must not be reported as a substitution");
        Assert.StartsWith("/", shell);
    }

    [Fact]
    public void Never_executes_the_string_the_hub_sent()
    {
        // The value the hub sent must never reach execve as a program name. If it did, a typo
        // in an enum -- or a hub someone had got at -- would become arbitrary program selection
        // on every Linux machine.
        //
        // Asserted as "one of the known interpreters", not as a fixed path: which one comes
        // back depends on whether the machine running the test has bash, and this test must
        // pass on the Windows workstation it is usually run from as well as on the target.
        string[] allowed = { "/bin/sh", "/bin/bash", "/usr/bin/bash" };
        foreach (var requested in new[] { "powershell", "/bin/evil", "../../evil", "bash", "sh" })
            Assert.Contains(RunScriptExecutor.ResolveShell(requested).Shell, allowed);
    }
}
