using TempMonitorAgent.Fleet;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>WingetLocator is deliberately environment-dependent, so these assert the
/// contract rather than a fixed path: whatever it returns must be a file that actually
/// exists, and it must never hand back the bare alias name that fails under SYSTEM.</summary>
public class WingetLocatorTests
{
    [Fact]
    public void Find_returns_an_existing_file_or_null()
    {
        WingetLocator.Invalidate();
        var path = WingetLocator.Find();
        if (path is null) return;   // no App Installer on this machine — a valid outcome
        Assert.True(File.Exists(path), $"WingetLocator returned a path that does not exist: {path}");
        Assert.True(Path.IsPathRooted(path), $"WingetLocator must return a full path, got: {path}");
    }

    [Fact]
    public void Find_is_stable_across_calls()
    {
        WingetLocator.Invalidate();
        var first = WingetLocator.Find();
        var second = WingetLocator.Find();
        Assert.Equal(first, second);
    }

    [Fact]
    public void Invalidate_forces_a_fresh_resolution()
    {
        var before = WingetLocator.Find();
        WingetLocator.Invalidate();
        Assert.Equal(before, WingetLocator.Find());
    }

    // ---- Package ranking -----------------------------------------------------------------
    // Which of several side-by-side App Installer packages to run winget from. Unlike Find()
    // above this is pure string ranking, so it can be pinned exactly -- and it needs to be,
    // because the obvious rule (highest version wins) picks the WRONG package on a real
    // machine. The two names below are from a Windows 11 Pro box in the fleet.

    private const string RealX64 = "Microsoft.DesktopAppInstaller_1.29.279.0_x64__8wekyb3d8bbwe";
    private const string NeutralStub =
        "Microsoft.DesktopAppInstaller_2026.623.1704.0_neutral_~_8wekyb3d8bbwe";

    [Fact]
    public void PickBest_prefers_the_architecture_build_over_a_higher_versioned_stub()
    {
        // 2026.623.1704.0 > 1.29.279.0, so version alone would choose the resource stub.
        Assert.Equal(RealX64, WingetLocator.PickBestPackage(new[] { RealX64, NeutralStub }, "x64"));
    }

    [Fact]
    public void PickBest_does_not_depend_on_enumeration_order()
    {
        Assert.Equal(RealX64, WingetLocator.PickBestPackage(new[] { NeutralStub, RealX64 }, "x64"));
    }

    [Fact]
    public void PickBest_within_a_rank_takes_the_highest_version()
    {
        // Two real builds side by side during a Store update.
        const string older = "Microsoft.DesktopAppInstaller_1.22.10582.0_x64__8wekyb3d8bbwe";
        Assert.Equal(RealX64, WingetLocator.PickBestPackage(new[] { older, RealX64 }, "x64"));
    }

    [Fact]
    public void PickBest_falls_back_to_a_staged_package_rather_than_reporting_nothing()
    {
        // If the stub is all that is present AND it holds a winget.exe, running it beats
        // telling the operator winget is missing on a machine where it plainly is not.
        Assert.Equal(NeutralStub, WingetLocator.PickBestPackage(new[] { NeutralStub }, "x64"));
    }

    [Fact]
    public void PickBest_prefers_a_normal_neutral_package_to_a_staged_one()
    {
        const string neutral = "Microsoft.DesktopAppInstaller_3.0.0.0_neutral__8wekyb3d8bbwe";
        Assert.Equal(neutral, WingetLocator.PickBestPackage(new[] { NeutralStub, neutral }, "x64"));
    }

    [Fact]
    public void PickBest_matches_the_running_architecture()
    {
        const string arm = "Microsoft.DesktopAppInstaller_1.29.279.0_arm64__8wekyb3d8bbwe";
        Assert.Equal(arm, WingetLocator.PickBestPackage(new[] { RealX64, arm }, "arm64"));
    }

    [Fact]
    public void PickBest_survives_an_unparseable_version()
    {
        const string junk = "Microsoft.DesktopAppInstaller_notaversion_x64__8wekyb3d8bbwe";
        Assert.Equal(RealX64, WingetLocator.PickBestPackage(new[] { junk, RealX64 }, "x64"));
    }

    [Fact]
    public void PickBest_returns_null_when_there_is_nothing_to_pick()
    {
        Assert.Null(WingetLocator.PickBestPackage(Array.Empty<string>(), "x64"));
    }

    [Fact]
    public void CurrentArchitecture_is_a_name_packages_actually_use()
    {
        Assert.Contains(WingetLocator.CurrentArchitecture(), new[] { "x64", "x86", "arm64" });
    }
}
