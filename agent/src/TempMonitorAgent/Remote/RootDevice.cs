using System.Runtime.InteropServices;
using System.Text;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Creates, restarts and removes a <b>root-enumerated</b> device node.
///
/// This exists because <c>pnputil /add-driver ... /install</c> cannot install the virtual
/// display. That flag tells PnP "find the hardware this driver matches and bind it" -- and there
/// is no hardware. An indirect display driver is software pretending to be a monitor, enumerated
/// under the ROOT bus, so the device node has to be created explicitly before any driver can
/// bind to it. This is what <c>devcon install</c> (and the driver project's bundled nefcon) do.
///
/// Reimplementing it here rather than shipping one of those tools is a deliberate trade: ~150
/// lines of documented SetupAPI against a third-party binary executing as SYSTEM on every
/// machine in the fleet, versioned separately and signed by someone else.
///
/// All entry points fail soft with a described error -- the caller turns that into a command
/// result an operator can read and act on.
/// </summary>
internal static class RootDevice
{
    /// <summary>
    /// Create a root-enumerated device node for <paramref name="hardwareId"/> and bind
    /// <paramref name="infPath"/> to it.
    /// </summary>
    internal static (bool ok, bool rebootRequired, string? error) Create(
        string hardwareId, string infPath)
    {
        Guid classGuid = GUID_DEVCLASS_DISPLAY;
        IntPtr set = SetupDiCreateDeviceInfoList(ref classGuid, IntPtr.Zero);
        if (set == InvalidHandle)
            return (false, false, $"SetupDiCreateDeviceInfoList failed (win32 {LastError()})");

        try
        {
            var info = new SP_DEVINFO_DATA { cbSize = Marshal.SizeOf<SP_DEVINFO_DATA>() };
            // "Display" is the class NAME matching GUID_DEVCLASS_DISPLAY; DICD_GENERATE_ID lets
            // Windows pick the instance id (ROOT\DISPLAY\0000, 0001, ...).
            if (!SetupDiCreateDeviceInfoW(set, "Display", ref classGuid, null, IntPtr.Zero,
                                          DICD_GENERATE_ID, ref info))
                return (false, false, $"SetupDiCreateDeviceInfo failed (win32 {LastError()})");

            // SPDRP_HARDWAREID takes a REG_MULTI_SZ: the id, then two NULs.
            byte[] hardwareIds = Encoding.Unicode.GetBytes(hardwareId + "\0\0");
            if (!SetupDiSetDeviceRegistryPropertyW(set, ref info, SPDRP_HARDWAREID,
                                                   hardwareIds, hardwareIds.Length))
                return (false, false, $"SetupDiSetDeviceRegistryProperty failed (win32 {LastError()})");

            if (!SetupDiCallClassInstaller(DIF_REGISTERDEVICE, set, ref info))
                return (false, false, $"DIF_REGISTERDEVICE failed (win32 {LastError()})");

            // The node now exists but has no driver. Bind the INF to it. INSTALLFLAG_FORCE
            // installs even if the ranking would prefer another driver -- there is no other.
            if (!UpdateDriverForPlugAndPlayDevicesW(IntPtr.Zero, hardwareId, infPath,
                                                    INSTALLFLAG_FORCE, out bool rebootRequired))
            {
                int error = LastError();
                // The node was registered but is now driverless; leave nothing half-built.
                SetupDiCallClassInstaller(DIF_REMOVE, set, ref info);
                return (false, false, $"UpdateDriverForPlugAndPlayDevices failed (win32 {error})");
            }

            return (true, rebootRequired, null);
        }
        finally
        {
            SetupDiDestroyDeviceInfoList(set);
        }
    }

    /// <summary>Remove every device node matching <paramref name="hardwareId"/>.</summary>
    internal static (bool ok, bool rebootRequired, string? error) Remove(string hardwareId)
    {
        return ForEachMatching(hardwareId, (set, info) =>
        {
            var parameters = new SP_REMOVEDEVICE_PARAMS
            {
                ClassInstallHeader = new SP_CLASSINSTALL_HEADER
                {
                    cbSize = Marshal.SizeOf<SP_CLASSINSTALL_HEADER>(),
                    InstallFunction = DIF_REMOVE,
                },
                Scope = DI_REMOVEDEVICE_GLOBAL,
                HwProfile = 0,
            };
            if (!SetupDiSetClassInstallParamsW(set, ref info, ref parameters,
                                               Marshal.SizeOf<SP_REMOVEDEVICE_PARAMS>()))
                return $"SetupDiSetClassInstallParams failed (win32 {LastError()})";
            if (!SetupDiCallClassInstaller(DIF_REMOVE, set, ref info))
                return $"DIF_REMOVE failed (win32 {LastError()})";
            return null;
        });
    }

    /// <summary>Stop and restart the device so a changed settings file takes effect without a
    /// reboot.</summary>
    internal static (bool ok, string? error) Restart(string hardwareId)
    {
        var (ok, _, error) = ForEachMatching(hardwareId, (set, info) =>
            SetupDiCallClassInstaller(DIF_PROPERTYCHANGE, set, ref info)
                ? null
                : $"DIF_PROPERTYCHANGE failed (win32 {LastError()})",
            beforeCall: (set, info) =>
            {
                // A stop followed by a start; a plain restart is not exposed, and a
                // start-without-stop is a no-op on a running device.
                Propagate(set, ref info, DICS_STOP);
                Propagate(set, ref info, DICS_START);
            });
        return (ok, error);
    }

    private static void Propagate(IntPtr set, ref SP_DEVINFO_DATA info, uint stateChange)
    {
        var parameters = new SP_PROPCHANGE_PARAMS
        {
            ClassInstallHeader = new SP_CLASSINSTALL_HEADER
            {
                cbSize = Marshal.SizeOf<SP_CLASSINSTALL_HEADER>(),
                InstallFunction = DIF_PROPERTYCHANGE,
            },
            StateChange = stateChange,
            Scope = DICS_FLAG_CONFIGSPECIFIC,
            HwProfile = 0,
        };
        if (SetupDiSetClassInstallParamsW(set, ref info, ref parameters,
                                          Marshal.SizeOf<SP_PROPCHANGE_PARAMS>()))
            SetupDiCallClassInstaller(DIF_PROPERTYCHANGE, set, ref info);
    }

    /// <summary>Run <paramref name="action"/> against every present device whose hardware ids
    /// include <paramref name="hardwareId"/>.</summary>
    private static (bool ok, bool rebootRequired, string? error) ForEachMatching(
        string hardwareId,
        Func<IntPtr, SP_DEVINFO_DATA, string?> action,
        Action<IntPtr, SP_DEVINFO_DATA>? beforeCall = null)
    {
        IntPtr set = SetupDiGetClassDevsW(IntPtr.Zero, "ROOT", IntPtr.Zero,
                                          DIGCF_ALLCLASSES | DIGCF_PRESENT);
        if (set == InvalidHandle)
            return (false, false, $"SetupDiGetClassDevs failed (win32 {LastError()})");

        try
        {
            var info = new SP_DEVINFO_DATA { cbSize = Marshal.SizeOf<SP_DEVINFO_DATA>() };
            bool matched = false;
            string? lastError = null;

            for (uint i = 0; SetupDiEnumDeviceInfo(set, i, ref info); i++)
            {
                info.cbSize = Marshal.SizeOf<SP_DEVINFO_DATA>();
                if (!HasHardwareId(set, ref info, hardwareId)) continue;

                matched = true;
                beforeCall?.Invoke(set, info);
                lastError = action(set, info) ?? lastError;
            }

            if (!matched) return (false, false, $"no device with hardware id {hardwareId} is present");
            return (lastError is null, false, lastError);
        }
        finally
        {
            SetupDiDestroyDeviceInfoList(set);
        }
    }

    private static bool HasHardwareId(IntPtr set, ref SP_DEVINFO_DATA info, string hardwareId)
    {
        var buffer = new byte[2048];
        if (!SetupDiGetDeviceRegistryPropertyW(set, ref info, SPDRP_HARDWAREID, out _,
                                               buffer, buffer.Length, out int size) || size <= 2)
            return false;
        return Encoding.Unicode.GetString(buffer, 0, size)
            .Split('\0', StringSplitOptions.RemoveEmptyEntries)
            .Any(id => id.Equals(hardwareId, StringComparison.OrdinalIgnoreCase));
    }

    private static int LastError() => Marshal.GetLastWin32Error();

    // ------------------------------------------------------------------ P/Invoke
    private static readonly IntPtr InvalidHandle = new(-1);

    private static Guid GUID_DEVCLASS_DISPLAY = new("4d36e968-e325-11ce-bfc1-08002be10318");

    private const uint DICD_GENERATE_ID = 0x00000001;
    private const uint SPDRP_HARDWAREID = 0x00000001;
    private const uint DIF_REGISTERDEVICE = 0x00000019;
    private const uint DIF_REMOVE = 0x00000005;
    private const uint DIF_PROPERTYCHANGE = 0x00000012;
    private const uint DI_REMOVEDEVICE_GLOBAL = 0x00000001;
    private const uint DICS_START = 0x00000004;
    private const uint DICS_STOP = 0x00000005;
    private const uint DICS_FLAG_CONFIGSPECIFIC = 0x00000002;
    private const uint DIGCF_PRESENT = 0x00000002;
    private const uint DIGCF_ALLCLASSES = 0x00000004;
    private const uint INSTALLFLAG_FORCE = 0x00000001;

    [StructLayout(LayoutKind.Sequential)]
    private struct SP_DEVINFO_DATA
    {
        public int cbSize;
        public Guid ClassGuid;
        public uint DevInst;
        public IntPtr Reserved;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct SP_CLASSINSTALL_HEADER
    {
        public int cbSize;
        public uint InstallFunction;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct SP_REMOVEDEVICE_PARAMS
    {
        public SP_CLASSINSTALL_HEADER ClassInstallHeader;
        public uint Scope;
        public uint HwProfile;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct SP_PROPCHANGE_PARAMS
    {
        public SP_CLASSINSTALL_HEADER ClassInstallHeader;
        public uint StateChange;
        public uint Scope;
        public uint HwProfile;
    }

    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern IntPtr SetupDiCreateDeviceInfoList(ref Guid classGuid, IntPtr parent);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetupDiCreateDeviceInfoW(
        IntPtr deviceInfoSet, string deviceName, ref Guid classGuid, string? description,
        IntPtr parent, uint creationFlags, ref SP_DEVINFO_DATA deviceInfoData);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetupDiSetDeviceRegistryPropertyW(
        IntPtr deviceInfoSet, ref SP_DEVINFO_DATA deviceInfoData, uint property,
        byte[] propertyBuffer, int propertyBufferSize);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetupDiGetDeviceRegistryPropertyW(
        IntPtr deviceInfoSet, ref SP_DEVINFO_DATA deviceInfoData, uint property,
        out uint propertyRegDataType, byte[] propertyBuffer, int propertyBufferSize,
        out int requiredSize);

    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern bool SetupDiCallClassInstaller(
        uint installFunction, IntPtr deviceInfoSet, ref SP_DEVINFO_DATA deviceInfoData);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetupDiSetClassInstallParamsW(
        IntPtr deviceInfoSet, ref SP_DEVINFO_DATA deviceInfoData,
        ref SP_REMOVEDEVICE_PARAMS classInstallParams, int size);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool SetupDiSetClassInstallParamsW(
        IntPtr deviceInfoSet, ref SP_DEVINFO_DATA deviceInfoData,
        ref SP_PROPCHANGE_PARAMS classInstallParams, int size);

    [DllImport("setupapi.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr SetupDiGetClassDevsW(
        IntPtr classGuid, string? enumerator, IntPtr parent, uint flags);

    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern bool SetupDiEnumDeviceInfo(
        IntPtr deviceInfoSet, uint index, ref SP_DEVINFO_DATA deviceInfoData);

    [DllImport("setupapi.dll", SetLastError = true)]
    private static extern bool SetupDiDestroyDeviceInfoList(IntPtr deviceInfoSet);

    [DllImport("newdev.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern bool UpdateDriverForPlugAndPlayDevicesW(
        IntPtr parent, string hardwareId, string infPath, uint installFlags,
        out bool rebootRequired);
}
