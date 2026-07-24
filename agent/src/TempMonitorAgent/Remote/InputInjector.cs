using System.Runtime.InteropServices;
using System.Text.Json;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Turns the browser's input events (arriving over the WebRTC control DataChannel) into real
/// input on the remote desktop via <c>SendInput</c> (roadmap #2, phase 5).
///
/// Mouse coordinates arrive NORMALISED (0..1 of the captured monitor) and are mapped to the
/// monitor's rectangle and then to the 0..65535 virtual-desktop space SendInput's absolute mode
/// uses -- so the pointer lands in the right place regardless of resolution or which monitor is
/// being viewed. Keyboard events map the browser's <c>KeyboardEvent.code</c> to a Windows
/// virtual-key where we know it, and fall back to Unicode injection for any printable character
/// we don't have a VK for, which keeps text entry working without an exhaustive keymap.
///
/// The message shapes are documented on <see cref="Apply"/>. Ctrl+Alt+Del is a special case:
/// it can't be synthesised with SendInput (the secure attention sequence is intercepted by the
/// kernel), so it goes through <c>SendSAS</c>, which a SYSTEM process in the interactive session
/// is allowed to call (policy permitting -- validated on-device).
/// </summary>
public sealed class InputInjector
{
    private readonly MonitorRect _monitor;

    public InputInjector(int monitorIndex)
    {
        _monitor = MonitorRect.ForIndex(monitorIndex);
    }

    /// <summary>Apply one control message. Shapes (JSON):
    /// <list type="bullet">
    /// <item><c>{"t":"m","x":0..1,"y":0..1}</c> — move</item>
    /// <item><c>{"t":"d"|"u","b":0|1|2,"x":..,"y":..}</c> — button down/up (0=left,1=middle,2=right)</item>
    /// <item><c>{"t":"w","dy":n}</c> — wheel (n notches, +up)</item>
    /// <item><c>{"t":"k","code":"KeyA","key":"a","down":true}</c> — key down/up</item>
    /// <item><c>{"t":"cad"}</c> — Ctrl+Alt+Del</item>
    /// </list>
    /// Unknown/malformed messages are ignored.</summary>
    public void Apply(string json)
    {
        JsonElement e;
        try { e = JsonDocument.Parse(json).RootElement; }
        catch { return; }
        if (e.ValueKind != JsonValueKind.Object || !e.TryGetProperty("t", out var tEl)) return;

        switch (tEl.GetString())
        {
            case "m": MouseMove(GetD(e, "x"), GetD(e, "y")); break;
            case "d": MouseMoveIfPresent(e); MouseButton(GetI(e, "b"), down: true); break;
            case "u": MouseMoveIfPresent(e); MouseButton(GetI(e, "b"), down: false); break;
            case "w": MouseWheel(GetI(e, "dy")); break;
            case "k": Key(GetS(e, "code"), GetS(e, "key"), GetBool(e, "down")); break;
            case "cad": SendSecureAttention(); break;
        }
    }

    private void MouseMoveIfPresent(JsonElement e)
    {
        if (e.TryGetProperty("x", out _) && e.TryGetProperty("y", out _))
            MouseMove(GetD(e, "x"), GetD(e, "y"));
    }

    private void MouseMove(double nx, double ny)
    {
        var (ax, ay) = _monitor.ToVirtualAbsolute(nx, ny);
        SendMouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, ax, ay, 0);
    }

    private static void MouseButton(int button, bool down)
    {
        uint flag = button switch
        {
            1 => down ? MOUSEEVENTF_MIDDLEDOWN : MOUSEEVENTF_MIDDLEUP,
            2 => down ? MOUSEEVENTF_RIGHTDOWN : MOUSEEVENTF_RIGHTUP,
            _ => down ? MOUSEEVENTF_LEFTDOWN : MOUSEEVENTF_LEFTUP,
        };
        SendMouse(flag, 0, 0, 0);
    }

    private static void MouseWheel(int notches) =>
        SendMouse(MOUSEEVENTF_WHEEL, 0, 0, notches * WHEEL_DELTA);

    private static void Key(string? code, string? key, bool down)
    {
        ushort vk = MapCodeToVk(code);
        if (vk != 0)
        {
            SendKey(vk, down, IsExtended(code));
            return;
        }
        // No VK mapping: inject the character directly (Unicode). Only meaningful for a single
        // printable character and only on key-down (the up is a no-op for Unicode injection).
        if (down && key is { Length: 1 })
            SendUnicode(key[0]);
    }

    // ------------------------------------------------------------------ SendInput plumbing
    private static void SendMouse(uint flags, int ax, int ay, int data)
    {
        var input = new INPUT
        {
            type = INPUT_MOUSE,
            U = new InputUnion
            {
                mi = new MOUSEINPUT { dx = ax, dy = ay, mouseData = (uint)data, dwFlags = flags },
            },
        };
        SendInput(1, new[] { input }, Marshal.SizeOf<INPUT>());
    }

    private static void SendKey(ushort vk, bool down, bool extended)
    {
        uint flags = 0;
        if (!down) flags |= KEYEVENTF_KEYUP;
        if (extended) flags |= KEYEVENTF_EXTENDEDKEY;
        var input = new INPUT
        {
            type = INPUT_KEYBOARD,
            U = new InputUnion { ki = new KEYBDINPUT { wVk = vk, dwFlags = flags } },
        };
        SendInput(1, new[] { input }, Marshal.SizeOf<INPUT>());
    }

    private static void SendUnicode(char c)
    {
        void One(uint extra) => SendInput(1, new[]
        {
            new INPUT
            {
                type = INPUT_KEYBOARD,
                U = new InputUnion { ki = new KEYBDINPUT { wScan = c, dwFlags = KEYEVENTF_UNICODE | extra } },
            },
        }, Marshal.SizeOf<INPUT>());
        One(0);
        One(KEYEVENTF_KEYUP);
    }

    private static void SendSecureAttention()
    {
        // asUser = false: send to the SAS-eligible desktop. Requires the SoftwareSASGeneration
        // policy to permit services; harmless (no-op) where it doesn't. Never throws.
        try { SendSAS(false); } catch { /* sas.dll missing / policy off -- validate on-device */ }
    }

    // ------------------------------------------------------------------ key mapping
    /// <summary>Map a browser <c>KeyboardEvent.code</c> to a Windows virtual-key, or 0 when we
    /// don't have one (the caller falls back to Unicode). Public/static for unit testing.</summary>
    internal static ushort MapCodeToVk(string? code)
    {
        if (string.IsNullOrEmpty(code)) return 0;
        if (code.Length == 4 && code.StartsWith("Key")) return (ushort)code[3];      // KeyA..KeyZ -> 'A'..'Z'
        if (code.Length == 6 && code.StartsWith("Digit")) return (ushort)code[5];    // Digit0..9 -> '0'..'9'
        if (code.Length >= 2 && code[0] == 'F' && int.TryParse(code[1..], out var fn) && fn is >= 1 and <= 24)
            return (ushort)(VK_F1 + (fn - 1));
        return code switch
        {
            "Enter" or "NumpadEnter" => 0x0D,
            "Escape" => 0x1B,
            "Backspace" => 0x08,
            "Tab" => 0x09,
            "Space" => 0x20,
            "ShiftLeft" => 0xA0, "ShiftRight" => 0xA1,
            "ControlLeft" => 0xA2, "ControlRight" => 0xA3,
            "AltLeft" => 0xA4, "AltRight" => 0xA5,
            "MetaLeft" => 0x5B, "MetaRight" => 0x5C,
            "CapsLock" => 0x14,
            "ArrowLeft" => 0x25, "ArrowUp" => 0x26, "ArrowRight" => 0x27, "ArrowDown" => 0x28,
            "Home" => 0x24, "End" => 0x23, "PageUp" => 0x21, "PageDown" => 0x22,
            "Insert" => 0x2D, "Delete" => 0x2E,
            "Minus" => 0xBD, "Equal" => 0xBB, "BracketLeft" => 0xDB, "BracketRight" => 0xDD,
            "Backslash" => 0xDC, "Semicolon" => 0xBA, "Quote" => 0xDE, "Backquote" => 0xC0,
            "Comma" => 0xBC, "Period" => 0xBE, "Slash" => 0xBF,
            _ => 0,
        };
    }

    private static bool IsExtended(string? code) => code switch
    {
        "ArrowLeft" or "ArrowUp" or "ArrowRight" or "ArrowDown" or
        "Home" or "End" or "PageUp" or "PageDown" or "Insert" or "Delete" or
        "NumpadEnter" or "ControlRight" or "AltRight" => true,
        _ => false,
    };

    private static double GetD(JsonElement e, string k) =>
        e.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetDouble() : 0;
    private static int GetI(JsonElement e, string k) =>
        e.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.Number ? v.GetInt32() : 0;
    private static string? GetS(JsonElement e, string k) =>
        e.TryGetProperty(k, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
    private static bool GetBool(JsonElement e, string k) =>
        e.TryGetProperty(k, out var v) && (v.ValueKind == JsonValueKind.True);

    // ------------------------------------------------------------------ monitor geometry
    /// <summary>One monitor's pixel rectangle and the virtual-desktop bounds, with the mapping
    /// from normalised (0..1) capture coordinates to SendInput's 0..65535 absolute space.</summary>
    private readonly struct MonitorRect
    {
        private readonly int _left, _top, _width, _height;
        private readonly int _vLeft, _vTop, _vWidth, _vHeight;

        private MonitorRect(int left, int top, int width, int height)
        {
            _left = left; _top = top; _width = Math.Max(1, width); _height = Math.Max(1, height);
            _vLeft = GetSystemMetrics(SM_XVIRTUALSCREEN);
            _vTop = GetSystemMetrics(SM_YVIRTUALSCREEN);
            _vWidth = Math.Max(1, GetSystemMetrics(SM_CXVIRTUALSCREEN));
            _vHeight = Math.Max(1, GetSystemMetrics(SM_CYVIRTUALSCREEN));
        }

        public (int ax, int ay) ToVirtualAbsolute(double nx, double ny)
        {
            nx = Math.Clamp(nx, 0, 1);
            ny = Math.Clamp(ny, 0, 1);
            double px = _left + nx * _width;   // pixel on the virtual desktop
            double py = _top + ny * _height;
            int ax = (int)Math.Round((px - _vLeft) * 65535.0 / _vWidth);
            int ay = (int)Math.Round((py - _vTop) * 65535.0 / _vHeight);
            return (Math.Clamp(ax, 0, 65535), Math.Clamp(ay, 0, 65535));
        }

        /// <summary>Bounds of the monitor at <paramref name="index"/> (DXGI/enumeration order),
        /// falling back to the primary if the index is out of range.</summary>
        public static MonitorRect ForIndex(int index)
        {
            var monitors = new List<(int l, int t, int w, int h)>();
            MonitorEnumProc cb = (IntPtr h, IntPtr dc, ref RECT r, IntPtr d) =>
            {
                monitors.Add((r.left, r.top, r.right - r.left, r.bottom - r.top));
                return true;
            };
            EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, cb, IntPtr.Zero);
            if (index < 0 || index >= monitors.Count)
                return new MonitorRect(0, 0,
                    GetSystemMetrics(SM_CXSCREEN), GetSystemMetrics(SM_CYSCREEN));
            var m = monitors[index];
            return new MonitorRect(m.l, m.t, m.w, m.h);
        }
    }

    // ------------------------------------------------------------------ P/Invoke
    private const uint INPUT_MOUSE = 0, INPUT_KEYBOARD = 1;
    private const uint MOUSEEVENTF_MOVE = 0x0001, MOUSEEVENTF_ABSOLUTE = 0x8000, MOUSEEVENTF_VIRTUALDESK = 0x4000;
    private const uint MOUSEEVENTF_LEFTDOWN = 0x0002, MOUSEEVENTF_LEFTUP = 0x0004;
    private const uint MOUSEEVENTF_RIGHTDOWN = 0x0008, MOUSEEVENTF_RIGHTUP = 0x0010;
    private const uint MOUSEEVENTF_MIDDLEDOWN = 0x0020, MOUSEEVENTF_MIDDLEUP = 0x0040;
    private const uint MOUSEEVENTF_WHEEL = 0x0800;
    private const int WHEEL_DELTA = 120;
    private const uint KEYEVENTF_KEYUP = 0x0002, KEYEVENTF_UNICODE = 0x0004, KEYEVENTF_EXTENDEDKEY = 0x0001;
    private const ushort VK_F1 = 0x70;
    private const int SM_CXSCREEN = 0, SM_CYSCREEN = 1;
    private const int SM_XVIRTUALSCREEN = 76, SM_YVIRTUALSCREEN = 77, SM_CXVIRTUALSCREEN = 78, SM_CYVIRTUALSCREEN = 79;

    [StructLayout(LayoutKind.Sequential)]
    private struct INPUT { public uint type; public InputUnion U; }
    [StructLayout(LayoutKind.Explicit)]
    private struct InputUnion
    {
        [FieldOffset(0)] public MOUSEINPUT mi;
        [FieldOffset(0)] public KEYBDINPUT ki;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct MOUSEINPUT
    {
        public int dx, dy;
        public uint mouseData, dwFlags, time;
        public IntPtr dwExtraInfo;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct KEYBDINPUT
    {
        public ushort wVk, wScan;
        public uint dwFlags, time;
        public IntPtr dwExtraInfo;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct RECT { public int left, top, right, bottom; }

    private delegate bool MonitorEnumProc(IntPtr hMonitor, IntPtr hdc, ref RECT rect, IntPtr data);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);

    [DllImport("user32.dll")]
    private static extern int GetSystemMetrics(int index);

    [DllImport("user32.dll")]
    private static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, MonitorEnumProc callback, IntPtr data);

    [DllImport("sas.dll")]
    private static extern void SendSAS(bool asUser);
}
