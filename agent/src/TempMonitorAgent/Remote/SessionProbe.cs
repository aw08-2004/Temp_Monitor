using System.Runtime.InteropServices;
using System.Text.Json.Serialization;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Enumerates Windows logon sessions -- who is signed in, where, and in what state.
///
/// Two consumers, deliberately sharing one implementation:
///   * <see cref="SessionInjector"/> picks which session to inject the capture helper into.
///   * The heartbeat reports the list so the hub's session switcher can show the operator a
///     real choice instead of silently auto-picking. On a machine with a console user and two
///     RDP sessions, "auto" is a guess; the operator usually knows which one they want.
///
/// A session with no username is not noise to be filtered out -- it is the logon screen, and on
/// a headless server that is precisely the session an operator needs to reach in order to sign
/// in. It is reported with <see cref="SessionInfo.IsLogonScreen"/> set.
/// </summary>
internal static class SessionProbe
{
    // WTS_CONNECTSTATE_CLASS
    internal const int WtsActive = 0;
    internal const int WtsConnected = 1;
    internal const int WtsConnectQuery = 2;
    internal const int WtsShadow = 3;
    internal const int WtsDisconnected = 4;
    internal const int WtsIdle = 5;
    internal const int WtsListen = 6;
    internal const int WtsReset = 7;
    internal const int WtsDown = 8;
    internal const int WtsInit = 9;

    /// <summary>One logon session, in the shape the hub and viewer consume.</summary>
    internal sealed class SessionInfo
    {
        [JsonPropertyName("id")] public uint SessionId { get; set; }
        [JsonPropertyName("user")] public string User { get; set; } = "";
        [JsonPropertyName("domain")] public string Domain { get; set; } = "";
        [JsonPropertyName("station")] public string StationName { get; set; } = "";
        [JsonPropertyName("client")] public string ClientName { get; set; } = "";
        [JsonPropertyName("state")] public int State { get; set; }
        [JsonPropertyName("state_name")] public string StateName => NameOfState(State);
        [JsonPropertyName("is_console")] public bool IsConsole { get; set; }
        /// <summary>No user is signed in, but the session has a window station -- i.e. it is
        /// sitting at the logon/lock screen. The session an operator wants on a headless box.</summary>
        [JsonPropertyName("is_logon_screen")] public bool IsLogonScreen =>
            string.IsNullOrEmpty(User) && !string.IsNullOrWhiteSpace(StationName) && SessionId != 0;

        /// <summary>DOMAIN\user, or empty when nobody is signed in.</summary>
        [JsonPropertyName("account")] public string Account =>
            string.IsNullOrEmpty(User) ? ""
            : string.IsNullOrEmpty(Domain) ? User : $"{Domain}\\{User}";
    }

    internal static string NameOfState(int state) => state switch
    {
        WtsActive => "active",
        WtsConnected => "connected",
        WtsConnectQuery => "connect-query",
        WtsShadow => "shadow",
        WtsDisconnected => "disconnected",
        WtsIdle => "idle",
        WtsListen => "listen",
        WtsReset => "reset",
        WtsDown => "down",
        WtsInit => "init",
        _ => $"unknown({state})",
    };

    /// <summary>Session id of the physical console, or 0xFFFFFFFF when nobody is at it.</summary>
    internal static uint ConsoleSessionId() => WTSGetActiveConsoleSessionId();

    /// <summary>
    /// All sessions on this machine. Returns an empty list rather than throwing if the
    /// enumeration fails -- every caller has a sane degraded answer, and a failed enumeration
    /// must not take down a heartbeat or a remote session.
    ///
    /// Safe to call from session 0: WTS enumeration is session-independent, unlike the display
    /// APIs (see <see cref="DisplayProbe"/>).
    /// </summary>
    internal static IReadOnlyList<SessionInfo> Enumerate()
    {
        var results = new List<SessionInfo>();
        uint console = WTSGetActiveConsoleSessionId();

        if (!WTSEnumerateSessionsW(IntPtr.Zero, 0, 1, out IntPtr buffer, out uint count)
            || buffer == IntPtr.Zero)
            return results;

        try
        {
            int size = Marshal.SizeOf<WTS_SESSION_INFO>();
            for (uint i = 0; i < count; i++)
            {
                var raw = Marshal.PtrToStructure<WTS_SESSION_INFO>(buffer + (int)(i * size));
                // Listener pseudo-sessions ("RDP-Tcp") are not somewhere a desktop can exist.
                if (raw.State == WtsListen) continue;

                results.Add(new SessionInfo
                {
                    SessionId = raw.SessionId,
                    State = raw.State,
                    IsConsole = raw.SessionId == console,
                    StationName = raw.pWinStationName != IntPtr.Zero
                        ? Marshal.PtrToStringUni(raw.pWinStationName) ?? "" : "",
                    User = Query(raw.SessionId, WTSUserName),
                    Domain = Query(raw.SessionId, WTSDomainName),
                    ClientName = Query(raw.SessionId, WTSClientName),
                });
            }
        }
        catch { /* partial results are better than none */ }
        finally
        {
            WTSFreeMemory(buffer);
        }
        return results;
    }

    private static string Query(uint session, int infoClass)
    {
        if (!WTSQuerySessionInformationW(IntPtr.Zero, session, infoClass,
                                         out IntPtr buffer, out uint bytes)
            || buffer == IntPtr.Zero)
            return "";
        try
        {
            // bytes includes the trailing NUL.
            return bytes <= 2 ? "" : Marshal.PtrToStringUni(buffer) ?? "";
        }
        finally
        {
            WTSFreeMemory(buffer);
        }
    }

    // WTS_INFO_CLASS
    private const int WTSUserName = 5;
    private const int WTSDomainName = 7;
    private const int WTSClientName = 10;

    [StructLayout(LayoutKind.Sequential)]
    private struct WTS_SESSION_INFO
    {
        public uint SessionId;
        public IntPtr pWinStationName;
        public int State;
    }

    [DllImport("wtsapi32.dll", SetLastError = true)]
    private static extern bool WTSEnumerateSessionsW(
        IntPtr server, uint reserved, uint version, out IntPtr sessionInfo, out uint count);

    [DllImport("wtsapi32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool WTSQuerySessionInformationW(
        IntPtr server, uint sessionId, int infoClass, out IntPtr buffer, out uint bytesReturned);

    [DllImport("wtsapi32.dll")]
    private static extern void WTSFreeMemory(IntPtr memory);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WTSGetActiveConsoleSessionId();
}
