using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json.Serialization;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Answers "does this machine have anything to capture?".
///
/// The reason this is not one method is a Windows fact that costs a lot of debugging time to
/// rediscover: <c>EnumDisplayDevices</c>, <c>GetSystemMetrics</c>, <c>QueryDisplayConfig</c> and
/// DXGI's <c>EnumOutputs</c> are all <b>session-scoped</b>. The agent service lives in session 0,
/// which has its own never-rendered desktop, so what the service reads there says nothing about
/// what the interactive session can see. Reporting session 0's answer to the hub would mark
/// every machine in the fleet as headless.
///
/// So there are two probes:
///   * <see cref="ProbeFromService"/> uses PnP only, which IS session-independent. It answers
///     "is a physical monitor attached to this hardware?" -- exactly the question that decides
///     whether a machine needs a virtual display, and answerable with nobody signed in.
///   * <see cref="ProbeFromSession"/> runs in the injected helper on a desktop-attached thread
///     and is authoritative about what the capture pipeline will actually get.
/// </summary>
internal static class DisplayProbe
{
    /// <summary>Hardware id of the bundled IddCx virtual display adapter.</summary>
    internal const string VirtualDisplayHardwareId = @"Root\MttVDD";

    internal sealed class DisplayReport
    {
        /// <summary>Physical monitors present as PnP devices. Zero means genuinely headless --
        /// no panel, no KVM, nothing plugged in.</summary>
        [JsonPropertyName("physical_monitors")] public int PhysicalMonitors { get; set; }

        /// <summary>Capturable outputs seen from the interactive session, or -1 when the probe
        /// ran in session 0 and therefore cannot know.</summary>
        [JsonPropertyName("active_outputs")] public int ActiveOutputs { get; set; } = -1;

        [JsonPropertyName("virtual_display_present")] public bool VirtualDisplayPresent { get; set; }
        [JsonPropertyName("virtual_display_started")] public bool VirtualDisplayStarted { get; set; }
        [JsonPropertyName("output_names")] public List<string> OutputNames { get; set; } = new();

        /// <summary>Nothing to capture and nothing virtual standing in for it -- the machine an
        /// operator would open and find a black screen.</summary>
        [JsonPropertyName("headless")] public bool Headless =>
            PhysicalMonitors == 0 && !VirtualDisplayPresent;
    }

    /// <summary>
    /// Probe from the service (session 0). PnP only -- see the class remarks for why the display
    /// APIs are useless here. <see cref="DisplayReport.ActiveOutputs"/> stays -1.
    /// </summary>
    internal static DisplayReport ProbeFromService()
    {
        var report = new DisplayReport { PhysicalMonitors = CountMonitorInterfaces() };
        var (present, started) = DetectVirtualDisplay();
        report.VirtualDisplayPresent = present;
        report.VirtualDisplayStarted = started;
        return report;
    }

    /// <summary>
    /// Probe from inside the interactive session. Must run on a thread already attached to the
    /// input desktop, or it reports the wrong desktop's displays.
    /// </summary>
    internal static DisplayReport ProbeFromSession()
    {
        var report = ProbeFromService();
        report.OutputNames = OutputNames();
        report.ActiveOutputs = report.OutputNames.Count;
        return report;
    }

    /// <summary>Number of DXGI outputs on the primary adapter. Used by the capture pipeline to
    /// tell the viewer how many monitors it could pick between; 0 on a headless box.</summary>
    internal static int OutputCount()
    {
        try { return OutputNames().Count; }
        catch { return 0; }
    }

    /// <summary>Device names of the desktop-attached displays, in enumeration order -- the same
    /// order the monitor index in <see cref="StreamSettings.Monitor"/> refers to.</summary>
    internal static List<string> OutputNames()
    {
        var names = new List<string>();
        var device = new DISPLAY_DEVICE { cb = Marshal.SizeOf<DISPLAY_DEVICE>() };
        for (uint i = 0; EnumDisplayDevicesW(null, i, ref device, 0); i++)
        {
            device.cb = Marshal.SizeOf<DISPLAY_DEVICE>();
            if ((device.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP) == 0) continue;
            names.Add(string.IsNullOrWhiteSpace(device.DeviceString)
                ? device.DeviceName : device.DeviceString);
        }
        return names;
    }

    /// <summary>Count present PnP monitor interfaces. Session-independent, which is the whole
    /// reason this is the service-side signal.</summary>
    private static int CountMonitorInterfaces()
    {
        var guid = GUID_DEVINTERFACE_MONITOR;
        if (CM_Get_Device_Interface_List_SizeW(out uint length, ref guid, null,
                                               CM_GET_DEVICE_INTERFACE_LIST_PRESENT) != 0
            || length <= 1)
            return 0;

        var buffer = new char[length];
        if (CM_Get_Device_Interface_ListW(ref guid, null, buffer, length,
                                          CM_GET_DEVICE_INTERFACE_LIST_PRESENT) != 0)
            return 0;

        // A REG_MULTI_SZ-style double-NUL-terminated list; count the non-empty entries.
        int count = 0, start = 0;
        for (int i = 0; i < buffer.Length; i++)
        {
            if (buffer[i] != '\0') continue;
            if (i > start) count++;
            start = i + 1;
            if (i + 1 < buffer.Length && buffer[i + 1] == '\0') break;
        }
        return count;
    }

    /// <summary>Is our virtual display adapter installed, and is its devnode started?</summary>
    internal static (bool present, bool started) DetectVirtualDisplay()
    {
        IntPtr set = SetupDiGetClassDevsW(IntPtr.Zero, "ROOT", IntPtr.Zero,
                                          DIGCF_ALLCLASSES | DIGCF_PRESENT);
        if (set == InvalidHandle) return (false, false);
        try
        {
            var data = new SP_DEVINFO_DATA { cbSize = Marshal.SizeOf<SP_DEVINFO_DATA>() };
            for (uint i = 0; SetupDiEnumDeviceInfo(set, i, ref data); i++)
            {
                data.cbSize = Marshal.SizeOf<SP_DEVINFO_DATA>();
                if (!HardwareIds(set, ref data).Any(
                        id => id.Equals(VirtualDisplayHardwareId, StringComparison.OrdinalIgnoreCase)))
                    continue;

                // CM_DEVCAP/status: a devnode that exists but failed to start is a very different
                // situation from one that is running, and the operator needs to be told which.
                bool started = CM_Get_DevNode_Status(out uint status, out _, data.DevInst, 0) == 0
                               && (status & DN_STARTED) != 0;
                return (true, started);
            }
        }
        catch { /* treat an unreadable device tree as "not installed" */ }
        finally
        {
            SetupDiDestroyDeviceInfoList(set);
        }
        return (false, false);
    }

    private static IEnumerable<string> HardwareIds(IntPtr set, ref SP_DEVINFO_DATA data)
    {
        var buffer = new byte[2048];
        if (!SetupDiGetDeviceRegistryPropertyW(set, ref data, SPDRP_HARDWAREID, out _,
                                               buffer, buffer.Length, out int size) || size <= 2)
            return Array.Empty<string>();
        return Encoding.Unicode.GetString(buffer, 0, size)
            .Split('\0', StringSplitOptions.RemoveEmptyEntries);
    }

    // ------------------------------------------------------------------ P/Invoke
    private static readonly IntPtr InvalidHandle = new(-1);

    private static Guid GUID_DEVINTERFACE_MONITOR =
        new("e6f07b5f-ee97-4a90-b076-33f57bf4eaa7");

    private const uint CM_GET_DEVICE_INTERFACE_LIST_PRESENT = 0;
    private const uint DIGCF_PRESENT = 0x02;
    private const uint DIGCF_ALLCLASSES = 0x04;
    private const uint SPDRP_HARDWAREID = 0x01;
    private const uint DN_STARTED = 0x00000008;
    private const int DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x00000001;

    [StructLayout(LayoutKind.Sequential)]
    private struct SP_DEVINFO_DATA
    {
        public int cbSize;
        public Guid ClassGuid;
        public uint DevInst;
        public IntPtr Reserved;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct DISPLAY_DEVICE
    {
        public int cb;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DeviceName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceString;
        public int StateFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceID;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceKey;
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool EnumDisplayDevicesW(
        string? device, uint deviceIndex, ref DISPLAY_DEVICE displayDevice, uint flags);

    [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
    private static extern int CM_Get_Device_Interface_List_SizeW(
        out uint length, ref Guid interfaceClass, string? deviceId, uint flags);

    [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
    private static extern int CM_Get_Device_Interface_ListW(
        ref Guid interfaceClass, string? deviceId, char[] buffer, uint bufferLength, uint flags);

    [DllImport("cfgmgr32.dll")]
    private static extern int CM_Get_DevNode_Status(
        out uint status, out uint problemNumber, uint devInst, uint flags);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr SetupDiGetClassDevsW(
        IntPtr classGuid, string? enumerator, IntPtr parent, uint flags);

    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern bool SetupDiEnumDeviceInfo(
        IntPtr deviceInfoSet, uint index, ref SP_DEVINFO_DATA deviceInfoData);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetupDiGetDeviceRegistryPropertyW(
        IntPtr deviceInfoSet, ref SP_DEVINFO_DATA deviceInfoData, uint property,
        out uint propertyRegDataType, byte[] propertyBuffer, int propertyBufferSize,
        out int requiredSize);

    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern bool SetupDiDestroyDeviceInfoList(IntPtr deviceInfoSet);
}
