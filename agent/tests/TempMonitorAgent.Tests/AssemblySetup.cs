using System.Runtime.CompilerServices;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Points the agent's state root at a scratch tree for the whole test run.
///
/// Without this, anything reaching AgentConfig.ProgramDataDir writes into the real
/// %ProgramData%\FleetHub\Agent. That passes on a clean box -- the directory does not
/// exist, so the test process creates it and inherits write rights -- and fails on any
/// machine with an agent actually installed, because StateDirectory.Harden has by then
/// left the tree writable only by SYSTEM and Administrators. The result was seven tests
/// (BackupManifestTests, DeployPackageExecutorTests) that passed in CI and failed on a
/// developer's own machine, for a reason nothing in their assertions hinted at.
///
/// This is the same principle StateDirectoryTests already states: a test that depends on
/// the host's ProgramData passes or fails for reasons unrelated to the code under test.
///
/// A module initializer rather than a fixture because AgentConfig resolves the state root
/// ONCE, into a static readonly, on first touch of the class. A per-class or per-collection
/// hook would lose the race against whichever test ran first; this runs at module load,
/// before any test body does.
/// </summary>
internal static class AssemblySetup
{
    internal static string StateRoot { get; } = Path.Combine(
        Path.GetTempPath(), "fleethub-state-test-" + Guid.NewGuid().ToString("n"));

    [ModuleInitializer]
    internal static void RedirectAgentState()
    {
        Directory.CreateDirectory(StateRoot);
        Environment.SetEnvironmentVariable(
            TempMonitorAgent.AgentConfig.StateDirOverrideVar, StateRoot);

        // Best effort: the run is over, and a leaked scratch tree under %TEMP% is harmless
        // but adds up across runs. Via TestTree because Program.cs hardens
        // AgentConfig.ProgramDataDir, which the redirect above now points here -- so the day
        // a test exercises the startup path this tree comes back write-locked, and a plain
        // Directory.Delete would quietly fail rather than clean up.
        AppDomain.CurrentDomain.ProcessExit += (_, _) => TestTree.Remove(StateRoot);
    }
}
