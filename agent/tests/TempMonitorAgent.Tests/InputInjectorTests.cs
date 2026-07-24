using TempMonitorAgent.Remote;

namespace TempMonitorAgent.Tests;

/// <summary>The pure, testable seam of input injection (roadmap #2): mapping the browser's
/// KeyboardEvent.code to a Windows virtual-key. The SendInput calls and monitor geometry need a
/// real desktop and are validated on-device; this pins the keymap that decides whether typing
/// works.</summary>
public class InputInjectorTests
{
    [Theory]
    [InlineData("KeyA", 0x41)]
    [InlineData("KeyZ", 0x5A)]
    [InlineData("Digit0", 0x30)]
    [InlineData("Digit9", 0x39)]
    [InlineData("Enter", 0x0D)]
    [InlineData("NumpadEnter", 0x0D)]
    [InlineData("Escape", 0x1B)]
    [InlineData("Backspace", 0x08)]
    [InlineData("Tab", 0x09)]
    [InlineData("Space", 0x20)]
    [InlineData("ArrowUp", 0x26)]
    [InlineData("Delete", 0x2E)]
    [InlineData("ControlLeft", 0xA2)]
    [InlineData("AltRight", 0xA5)]
    [InlineData("F1", 0x70)]
    [InlineData("F12", 0x7B)]
    public void MapCodeToVk_KnownKeys(string code, int expected)
    {
        Assert.Equal((ushort)expected, InputInjector.MapCodeToVk(code));
    }

    [Theory]
    [InlineData("")]
    [InlineData(null)]
    [InlineData("SomeUnknownKey")]
    [InlineData("IntlBackslash")]   // deliberately unmapped -> Unicode fallback handles it
    public void MapCodeToVk_UnknownKeysReturnZero(string? code)
    {
        // 0 signals "no VK" so the injector falls back to Unicode injection for printable keys.
        Assert.Equal((ushort)0, InputInjector.MapCodeToVk(code));
    }

    [Fact]
    public void MapCodeToVk_FunctionKeyOutOfRangeIsUnmapped()
    {
        Assert.Equal((ushort)0, InputInjector.MapCodeToVk("F25"));
    }
}
