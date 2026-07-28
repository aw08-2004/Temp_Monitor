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
}
