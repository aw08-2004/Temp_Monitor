using TempMonitorAgent.Fleet.Executors;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// A <c>run_script</c> naming a Linux shell must be REFUSED on Windows, not quietly run through
/// PowerShell (roadmap #22's hub-side gaps, hub 1.123.0). The hub has allowed <c>bash</c> and
/// <c>sh</c> since a rule could target a Linux box, and the Windows agent's session manager
/// maps every unknown shell to PowerShell -- so a bash script aimed at the wrong machine ran,
/// partly worked, and reported success.
///
/// The legacy values must keep their meaning: <c>powershell</c>, <c>cmd</c> and the
/// <c>batch</c>/<c>bat</c> aliases are what every rule written before 1.123.0 sends.
/// </summary>
public class RunScriptShellTests
{
    [Theory]
    [InlineData("bash")]
    [InlineData("sh")]
    [InlineData(" BASH ")]
    public void LinuxShellsAreRecognised(string shell) =>
        Assert.True(RunScriptExecutor.IsLinuxOnlyShell(shell));

    [Theory]
    [InlineData("powershell")]
    [InlineData("cmd")]
    [InlineData("batch")]
    [InlineData("bat")]
    [InlineData("")]
    public void WindowsShellsAndLegacyValuesStillRun(string shell) =>
        Assert.False(RunScriptExecutor.IsLinuxOnlyShell(shell));
}
