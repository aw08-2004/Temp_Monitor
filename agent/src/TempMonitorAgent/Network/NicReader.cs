using System.Management;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using Microsoft.Win32;

namespace TempMonitorAgent.Network;

/// <summary>One adapter, in the shape the hub stores (roadmap #10).</summary>
public sealed record NicInfo(
    string Mac,
    string Name,
    string Description,
    string Ipv4,
    int? Prefix,
    string Kind,
    bool LinkUp,
    /// <summary>Whether this adapter is allowed to wake the machine, or null when we could
    /// not tell. The three-way distinction is load-bearing: "this NIC cannot wake the PC"
    /// and "the driver does not expose the setting" send an operator to different places,
    /// and collapsing the second into false would have them fixing something that is not
    /// broken.</summary>
    bool? WakeEnabled);

/// <summary>This machine's network inventory, plus the machine-level facts that decide
/// whether a magic packet would do anything when it arrives.</summary>
public sealed record NetworkReport(IReadOnlyList<NicInfo> Nics, bool? FastStartup);

/// <summary>
/// Reads the adapters the hub needs in order to wake this machine (roadmap #10).
///
/// **Nothing in the product collected a MAC or an IP before this.** The hub groups machines
/// into subnets so it can pick an awake peer to relay a magic packet through, and that
/// grouping has to come from the machine itself -- the only address the hub can otherwise
/// see is the NAT'd site edge, which every PC at an office shares and which would fold a
/// whole building into one fictional subnet.
///
/// **The addresses come from .NET, the wake flags from WMI and the registry.**
/// <c>NetworkInterface</c> answers identity, address, prefix and link state on every
/// Windows without a WMI round trip. It has nothing to say about wake capability, and the
/// two places that do are the device's power policy
/// (<c>root\wmi:MSPower_DeviceWakeEnable</c>, the "Allow this device to wake the computer"
/// checkbox) and the driver's own <c>*WakeOnMagicPacket</c> advanced property. BOTH have to
/// be on: the first lets the device wake the machine, the second decides whether a magic
/// packet is one of the things it wakes for, and a NIC with the first and not the second
/// looks perfectly configured and ignores every packet sent to it.
///
/// **Every lookup here fails soft to null.** A machine whose WMI repository is unhappy
/// still has adapters worth reporting, and reporting them without a wake flag is strictly
/// better than reporting nothing -- the hub can still find a relay and still send a packet.
/// </summary>
public static class NicReader
{
    public const string KindWired = "wired";
    public const string KindWireless = "wireless";
    public const string KindOther = "other";

    /// <summary>The NIC class GUID, under which every adapter's driver keeps its advanced
    /// properties. Stable across every Windows release.</summary>
    private const string NetClassKey =
        @"SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}";

    /// <summary>Read every usable adapter on this machine.</summary>
    public static NetworkReport Read()
    {
        var wakeByPnpId = ReadDeviceWakeEnable();
        var guidToPnpId = ReadAdapterPnpIds();

        var nics = new List<NicInfo>();
        foreach (var adapter in SafeAdapters())
        {
            var mac = FormatMac(adapter.GetPhysicalAddress()?.GetAddressBytes());
            if (mac.Length == 0) continue;

            var kind = Classify(adapter);
            // Loopback and tunnels are dropped outright rather than reported as `other`:
            // they have no MAC anybody can wake and they would appear on the machine page
            // as adapters an operator has to reason about.
            if (adapter.NetworkInterfaceType is NetworkInterfaceType.Loopback
                or NetworkInterfaceType.Tunnel) continue;

            var (ipv4, prefix) = PrimaryIpv4(adapter);
            nics.Add(new NicInfo(
                Mac: mac,
                Name: adapter.Name ?? "",
                Description: adapter.Description ?? "",
                Ipv4: ipv4,
                Prefix: prefix,
                Kind: kind,
                LinkUp: adapter.OperationalStatus == OperationalStatus.Up,
                // Only asked of wired adapters. This mechanism cannot wake a laptop over
                // Wi-Fi at all, so a wake flag on a wireless NIC is a fact with no
                // consequence, and showing one would suggest the opposite.
                WakeEnabled: kind == KindWired
                    ? WakeFlagFor(adapter, guidToPnpId, wakeByPnpId)
                    : null));
        }
        return new NetworkReport(nics, ReadFastStartup());
    }

    private static IEnumerable<NetworkInterface> SafeAdapters()
    {
        try { return NetworkInterface.GetAllNetworkInterfaces(); }
        catch { return Array.Empty<NetworkInterface>(); }
    }

    /// <summary>`AA:BB:CC:DD:EE:FF`, or "" for anything that is not a six-byte MAC.
    ///
    /// An all-zero address is rejected: Hyper-V's placeholder adapters report one, and
    /// stored it would look exactly like a wakeable target while every packet aimed at it
    /// went nowhere. The hub rejects it again on ingest -- two checks, because this is the
    /// value the whole feature is keyed on.</summary>
    public static string FormatMac(byte[]? bytes)
    {
        if (bytes is null || bytes.Length != 6) return "";
        if (bytes.All(b => b == 0)) return "";
        return string.Join(":", bytes.Select(b => b.ToString("X2")));
    }

    private static string Classify(NetworkInterface adapter) => adapter.NetworkInterfaceType switch
    {
        NetworkInterfaceType.Ethernet or NetworkInterfaceType.GigabitEthernet
            or NetworkInterfaceType.FastEthernetT or NetworkInterfaceType.FastEthernetFx
            or NetworkInterfaceType.Ethernet3Megabit => KindWired,
        NetworkInterfaceType.Wireless80211 => KindWireless,
        _ => KindOther,
    };

    /// <summary>The adapter's IPv4 address and prefix length, or ("", null).
    ///
    /// APIPA (169.254/16) is skipped: an adapter whose DHCP failed would otherwise be
    /// grouped with every OTHER machine whose DHCP failed, and the hub would pick a relay
    /// in a different building on the strength of a shared link-local range.</summary>
    private static (string, int?) PrimaryIpv4(NetworkInterface adapter)
    {
        try
        {
            foreach (var unicast in adapter.GetIPProperties().UnicastAddresses)
            {
                if (unicast.Address.AddressFamily != AddressFamily.InterNetwork) continue;
                var text = unicast.Address.ToString();
                if (text.StartsWith("169.254.", StringComparison.Ordinal)) continue;
                if (text.StartsWith("127.", StringComparison.Ordinal)) continue;
                var prefix = PrefixFrom(unicast);
                if (prefix is null) continue;
                return (text, prefix);
            }
        }
        catch { /* an adapter that will not describe itself is still worth its MAC */ }
        return ("", null);
    }

    /// <summary>Prefix length, preferring the OS-reported value and deriving it from the
    /// mask otherwise -- <c>PrefixLength</c> is unavailable on some adapter types, and a
    /// mask with no prefix is exactly as useless to the hub as no address at all.</summary>
    private static int? PrefixFrom(UnicastIPAddressInformation unicast)
    {
        try
        {
            if (unicast.PrefixLength is > 0 and < 32) return unicast.PrefixLength;
        }
        catch { /* not supported on this adapter; fall through to the mask */ }

        try
        {
            if (unicast.IPv4Mask is null) return null;
            var bits = unicast.IPv4Mask.GetAddressBytes()
                .Sum(b => System.Numerics.BitOperations.PopCount((uint)b));
            return bits is > 0 and < 32 ? bits : null;
        }
        catch { return null; }
    }

    // ------------------------------------------------------------------ wake flags

    /// <summary>`{PNP device id (upper) -> allowed to wake}` from the device power policy.
    ///
    /// This is the "Allow this device to wake the computer" checkbox, and `root\wmi` is the
    /// only place it is readable. Keyed on the PNP id because that is the only identifier
    /// <c>MSPower_DeviceWakeEnable</c> carries -- the join back to a NIC goes through
    /// <c>Win32_NetworkAdapter</c>, which knows both it and the interface GUID.</summary>
    private static Dictionary<string, bool> ReadDeviceWakeEnable()
    {
        var map = new Dictionary<string, bool>(StringComparer.OrdinalIgnoreCase);
        try
        {
            using var searcher = new ManagementObjectSearcher(
                new ManagementScope(@"\\.\root\wmi"),
                new ObjectQuery("SELECT InstanceName, Enable FROM MSPower_DeviceWakeEnable"));
            foreach (var item in searcher.Get().Cast<ManagementBaseObject>())
            {
                using (item)
                {
                    var instance = (item["InstanceName"] as string ?? "").Trim();
                    if (instance.Length == 0) continue;
                    // WMI instance names carry a trailing enumerator suffix ("_0") that the
                    // PNP id does not. Trimmed here rather than matched loosely later: a
                    // prefix match would let one device's policy answer for another's.
                    if (instance.EndsWith("_0", StringComparison.Ordinal))
                        instance = instance[..^2];
                    map[instance] = item["Enable"] is bool enabled && enabled;
                }
            }
        }
        catch { /* no root\wmi, or a driver that does not publish policy: unknown, not false */ }
        return map;
    }

    /// <summary>`{interface GUID -> PNP device id}`, the join between .NET's adapters and
    /// WMI's device policy.</summary>
    private static Dictionary<string, string> ReadAdapterPnpIds()
    {
        var map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT GUID, PNPDeviceID FROM Win32_NetworkAdapter WHERE PNPDeviceID IS NOT NULL");
            foreach (var item in searcher.Get().Cast<ManagementBaseObject>())
            {
                using (item)
                {
                    var guid = (item["GUID"] as string ?? "").Trim();
                    var pnp = (item["PNPDeviceID"] as string ?? "").Trim();
                    if (guid.Length > 0 && pnp.Length > 0) map[guid] = pnp;
                }
            }
        }
        catch { /* unknown, not false */ }
        return map;
    }

    /// <summary>Whether this adapter would actually wake the machine on a magic packet.
    ///
    /// The AND of two independent settings, and the conjunction is the point: a NIC allowed
    /// to wake the machine but with magic-packet wake turned off in its driver looks
    /// correctly configured in Device Manager and ignores every packet the hub sends. If
    /// NEITHER can be read the answer is null -- unknown, so the console says nothing rather
    /// than accusing a working machine.</summary>
    private static bool? WakeFlagFor(NetworkInterface adapter,
                                     IReadOnlyDictionary<string, string> guidToPnpId,
                                     IReadOnlyDictionary<string, bool> wakeByPnpId)
    {
        bool? devicePolicy = null;
        if (guidToPnpId.TryGetValue(adapter.Id ?? "", out var pnpId)
            && wakeByPnpId.TryGetValue(pnpId, out var enabled))
            devicePolicy = enabled;

        var magicPacket = ReadMagicPacketProperty(adapter.Id ?? "");

        if (devicePolicy is null && magicPacket is null) return null;
        return (devicePolicy ?? true) && (magicPacket ?? true);
    }

    /// <summary>The driver's `*WakeOnMagicPacket` advanced property, or null.
    ///
    /// Lives in the adapter's own subkey under the NIC class, found by matching
    /// `NetCfgInstanceId` to the interface GUID. Stored as a string ("0"/"1") by every
    /// driver that publishes it, though a REG_DWORD is accepted too -- reading it back
    /// wrongly would report a wakeable machine as unwakeable, and the console would send
    /// somebody to a desk for nothing.</summary>
    private static bool? ReadMagicPacketProperty(string interfaceGuid)
    {
        if (interfaceGuid.Length == 0) return null;
        try
        {
            using var classKey = Registry.LocalMachine.OpenSubKey(NetClassKey);
            if (classKey is null) return null;
            foreach (var name in classKey.GetSubKeyNames())
            {
                using var adapterKey = classKey.OpenSubKey(name);
                var instanceId = adapterKey?.GetValue("NetCfgInstanceId") as string;
                if (!string.Equals(instanceId, interfaceGuid, StringComparison.OrdinalIgnoreCase))
                    continue;
                var value = adapterKey!.GetValue("*WakeOnMagicPacket");
                if (value is null) return null;
                return value.ToString()?.Trim() == "1";
            }
        }
        catch { /* unknown, not false */ }
        return null;
    }

    /// <summary>Whether Windows Fast Startup is on, or null when the value is absent.
    ///
    /// Hybrid shutdown means a "shut down" machine is really hibernating from a session
    /// that never fully ended, and wake-from-S5 fails on many NICs in that state. The
    /// symptom is a machine that wakes from Sleep and not from Shut Down, which reads as an
    /// intermittent fault rather than as a setting -- which is exactly why it is inventory
    /// here instead of a line in a runbook.
    ///
    /// Absent is null rather than a guess in either direction: the default has moved between
    /// Windows releases and SKUs, and defaulting to "on" would raise this diagnosis against
    /// every machine that never had the value written.</summary>
    public static bool? ReadFastStartup()
    {
        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(
                @"SYSTEM\CurrentControlSet\Control\Session Manager\Power");
            var value = key?.GetValue("HiberbootEnabled");
            if (value is null) return null;
            return Convert.ToInt32(value) != 0;
        }
        catch { return null; }
    }
}
