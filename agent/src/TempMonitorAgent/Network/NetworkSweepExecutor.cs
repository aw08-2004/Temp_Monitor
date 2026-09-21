using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Network;

/// <summary>
/// network_sweep: what is on one of this machine's own subnets (roadmap #18).
///
/// **This command is about a network, not about this PC.** The hub is almost never on the
/// segment it is being asked about -- for a multi-site helpdesk it sits in one office and
/// the subnet in question is behind a router in another -- so it picks a machine that is
/// already there and asks it to look. Same peer-relay reasoning as WakeMachineExecutor, and
/// for the same reason: there is no vantage point but this one.
///
/// **The probe is ARP, and that choice is the feature.** A ping sweep of an office subnet
/// finds the printers and misses the PCs, because a Windows machine with the firewall at
/// its defaults does not answer ICMP from an unknown host -- the exact inverse of what a
/// shadow-IT hunt is for. A host cannot decline to answer ARP and stay reachable on the
/// segment, so ARP is the one probe whose silence really does mean nothing is there.
/// <c>SendARP</c> also needs no raw socket and no elevation beyond what the service already
/// has, which keeps this from being a packet crafter living inside the agent.
///
/// **ARP does not cross a router, and so neither does this.** The subnet is re-checked here
/// against the adapters this machine actually has right now -- the hub refuses an
/// off-segment range when the sweep is requested, but a command queued before somebody
/// unplugged a dock would arrive asking about a segment this machine has since left, and
/// sweeping it would report an empty network rather than an unanswerable question.
///
/// **The host list is POSTed, not returned as command output**, for the reason
/// ListDirectoryExecutor gives: a full /22 is a thousand rows and the command channel's
/// output is a terminal transcript. Keeping them apart is also what lets the console tell a
/// subnet with nothing on it from a sweep whose report was lost.
/// </summary>
public sealed class NetworkSweepExecutor : ICommandExecutor
{
    /// <summary>Matches hub/discovery.py MAX_SWEEP_ADDRESSES. The hub refuses a wider
    /// subnet when the sweep is asked for; this is the cap that stops the machine spending
    /// ten minutes on one if a wider one ever reaches it.</summary>
    private const int MaxAddresses = 1024;

    /// <summary>How many probes are in flight at once. <c>SendARP</c> blocks for its own
    /// retry timeout (around a second) on an address nothing answers from, so a sequential
    /// /24 would take minutes -- long enough that an operator would assume it had hung. The
    /// number is a compromise against the other failure: a few hundred simultaneous ARP
    /// requests is a broadcast storm on a segment somebody else is trying to work on.</summary>
    private const int Parallelism = 32;

    /// <summary>Total budget for reverse DNS across every responder. Names are a courtesy --
    /// they make a list of addresses readable -- and a WINS or DNS server that is not
    /// answering must not turn a successful sweep into a command that timed out. Whatever
    /// resolves inside the budget gets a name; the rest are reported as addresses.</summary>
    private static readonly TimeSpan NameBudget = TimeSpan.FromSeconds(10);

    private readonly ILogger<NetworkSweepExecutor> _log;
    private readonly FleetClient _fleet;

    public NetworkSweepExecutor(ILogger<NetworkSweepExecutor> log, FleetClient fleet)
    {
        _log = log;
        _fleet = fleet;
    }

    public string Type => "network_sweep";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                                  CancellationToken ct)
    {
        var scanId = cmd.Params.GetString("scan_id") ?? "";
        if (scanId.Length == 0)
            return CommandResult.Fail("No scan id was supplied.");

        var subnet = (cmd.Params.GetString("subnet") ?? "").Trim();
        if (!TryParseCidr(subnet, out var network, out var prefix))
        {
            await ReportFailureAsync(scanId, $"not a subnet: {subnet}", ct);
            return CommandResult.Fail($"not a subnet: {subnet}");
        }

        // Defence in depth against a dock unplugged between the click and here -- see the
        // class docstring. Reported as a failure rather than swept anyway, because the
        // wrong answer (an empty network) is indistinguishable from a real finding.
        if (!IsLocalSubnet(network, prefix))
        {
            var refusal = $"this machine is not on {subnet} any more, so an ARP sweep of it "
                          + "would reach nothing";
            await ReportFailureAsync(scanId, refusal, ct);
            return CommandResult.Fail(refusal);
        }

        var addresses = HostAddresses(network, prefix);
        if (addresses.Count == 0)
        {
            await ReportFailureAsync(scanId, $"{subnet} has no host addresses to sweep", ct);
            return CommandResult.Fail($"{subnet} has no host addresses to sweep");
        }

        onOutput?.Invoke($"sweeping {subnet} ({addresses.Count} addresses)\n");

        List<SweepHit> hits;
        try
        {
            hits = Probe(addresses, ct);
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception e)
        {
            await ReportFailureAsync(scanId, e.Message, ct);
            return CommandResult.Fail($"the sweep could not run: {e.Message}");
        }

        await ResolveNamesAsync(hits, ct);

        var payload = new JsonObject
        {
            ["subnet"] = subnet,
            ["probed"] = addresses.Count,
            ["hosts"] = new JsonArray(hits.Select(h => (JsonNode)new JsonObject
            {
                ["ip"] = h.Address.ToString(),
                ["mac"] = h.Mac,
                ["hostname"] = h.Hostname,
            }).ToArray()),
        };

        if (!await _fleet.ReportSweepAsync(scanId, payload, ct))
            return CommandResult.Fail("The hub would not accept the sweep results.");
        return CommandResult.Ok(
            $"{hits.Count} host(s) answered on {subnet} out of {addresses.Count} probed");
    }

    /// <summary>One address that answered, and what it answered with.</summary>
    private sealed class SweepHit
    {
        public required IPAddress Address { get; init; }
        public required string Mac { get; init; }
        public string Hostname { get; set; } = "";
    }

    // ---------------------------------------------------------------- addressing
    /// <summary>Parse an IPv4 CIDR string into its supplied address and prefix length.
    /// Returns false for malformed input, non-IPv4 addresses, and prefixes outside
    /// 1 through 31.</summary>
    public static bool TryParseCidr(string cidr, out IPAddress network, out int prefix)
    {
        network = IPAddress.None;
        prefix = 0;
        var parts = (cidr ?? "").Split('/');
        if (parts.Length != 2) return false;
        if (!IPAddress.TryParse(parts[0], out var parsed)) return false;
        if (parsed.AddressFamily != AddressFamily.InterNetwork) return false;
        if (!int.TryParse(parts[1], out prefix) || prefix is < 1 or > 31) return false;
        network = parsed;
        return true;
    }

    /// <summary>Every host address in the subnet -- network and broadcast excluded.
    /// Capped, and the cap is silent on purpose: the hub refuses a wider subnet before it
    /// gets here, so reaching the cap means something is already wrong upstream and the
    /// useful thing to do is sweep what we can rather than refuse the lot.</summary>
    public static List<IPAddress> HostAddresses(IPAddress network, int prefix)
    {
        var baseValue = ToUInt32(network);
        var mask = prefix == 0 ? 0u : uint.MaxValue << (32 - prefix);
        // A /31 is the exception (RFC 3021): a point-to-point link has no network or
        // broadcast address, so both of its two addresses are hosts. Skipping them the way
        // the general rule does would leave nothing to sweep and report the link as empty.
        var first = prefix == 31 ? (baseValue & mask) : (baseValue & mask) + 1;
        var last = prefix == 31 ? (baseValue | ~mask) : (baseValue | ~mask) - 1;
        var addresses = new List<IPAddress>();
        for (var value = first; value <= last && addresses.Count < MaxAddresses; value++)
            addresses.Add(FromUInt32(value));
        return addresses;
    }

    private static uint ToUInt32(IPAddress address)
    {
        var bytes = address.GetAddressBytes();
        return ((uint)bytes[0] << 24) | ((uint)bytes[1] << 16)
               | ((uint)bytes[2] << 8) | bytes[3];
    }

    private static IPAddress FromUInt32(uint value) => new(new[]
    {
        (byte)(value >> 24), (byte)(value >> 16), (byte)(value >> 8), (byte)value,
    });

    /// <summary>Whether this machine has an address inside that subnet right now.</summary>
    private static bool IsLocalSubnet(IPAddress network, int prefix)
    {
        var mask = prefix == 0 ? 0u : uint.MaxValue << (32 - prefix);
        var wanted = ToUInt32(network) & mask;
        foreach (var adapter in SafeAdapters())
        {
            foreach (var unicast in adapter.GetIPProperties().UnicastAddresses)
            {
                if (unicast.Address.AddressFamily != AddressFamily.InterNetwork) continue;
                if ((ToUInt32(unicast.Address) & mask) == wanted) return true;
            }
        }
        return false;
    }

    private static IEnumerable<NetworkInterface> SafeAdapters()
    {
        NetworkInterface[] adapters;
        try
        {
            adapters = NetworkInterface.GetAllNetworkInterfaces();
        }
        catch (NetworkInformationException)
        {
            yield break;
        }
        foreach (var adapter in adapters)
            if (adapter.OperationalStatus == OperationalStatus.Up
                && adapter.NetworkInterfaceType != NetworkInterfaceType.Loopback)
                yield return adapter;
    }

    // ---------------------------------------------------------------- the probe
    /// <summary>ARP every address, in parallel, and keep the ones that answered.</summary>
    private static List<SweepHit> Probe(IReadOnlyList<IPAddress> addresses,
                                        CancellationToken ct)
    {
        var hits = new List<SweepHit>();
        var options = new ParallelOptions
        {
            MaxDegreeOfParallelism = Parallelism,
            CancellationToken = ct,
        };
        Parallel.ForEach(addresses, options, address =>
        {
            var mac = ArpFor(address);
            if (mac.Length == 0) return;
            // The list is the only shared state, and a sweep produces at most a thousand
            // appends -- a lock is cheaper to read than a concurrent collection that would
            // then have to be ordered anyway.
            lock (hits) hits.Add(new SweepHit { Address = address, Mac = mac });
        });
        return hits;
    }

    /// <summary>The MAC that answers for one address, or "" when no response can be
    /// obtained.</summary>
    public static string ArpFor(IPAddress address)
    {
        var mac = new byte[6];
        var length = (uint)mac.Length;
        try
        {
            // Source address 0 lets Windows pick the interface from its own routing table,
            // which is the right answer on a machine with a dock, a Wi-Fi adapter and a VPN
            // all up at once -- and the only answer we could give instead would be a guess.
            if (SendARP(BitConverter.ToUInt32(address.GetAddressBytes(), 0), 0, mac,
                        ref length) != 0)
                return "";
        }
        catch (Exception e) when (e is DllNotFoundException or EntryPointNotFoundException)
        {
            // Not fatal per address, but it will be for every address, so let the caller's
            // empty result speak rather than throwing a thousand times.
            return "";
        }
        if (length < 6) return "";
        return string.Join(":", mac.Take(6).Select(b => b.ToString("X2")));
    }

    [DllImport("iphlpapi.dll", ExactSpelling = true)]
    private static extern int SendARP(uint destIp, uint srcIp, byte[] macAddr,
                                      ref uint macAddrLen);

    // ---------------------------------------------------------------- names
    /// <summary>Best-effort reverse DNS for the responders, inside one shared budget.
    ///
    /// Bounded as a whole rather than per lookup because the failure mode is a DNS server
    /// that is not answering at all, in which case every lookup costs its full timeout and
    /// a per-lookup bound would still add up to minutes.</summary>
    private async Task ResolveNamesAsync(List<SweepHit> hits, CancellationToken ct)
    {
        if (hits.Count == 0) return;
        using var budget = CancellationTokenSource.CreateLinkedTokenSource(ct);
        budget.CancelAfter(NameBudget);
        try
        {
            await Parallel.ForEachAsync(hits,
                new ParallelOptions
                {
                    MaxDegreeOfParallelism = Parallelism,
                    CancellationToken = budget.Token,
                },
                async (hit, token) =>
                {
                    try
                    {
                        // The address is passed as a STRING, not as an IPAddress: only
                        // the string overloads take a CancellationToken, and without one a
                        // DNS server that has stopped answering holds this task for its own
                        // full timeout regardless of the budget below.
                        var entry = await Dns.GetHostEntryAsync(hit.Address.ToString(), token);
                        hit.Hostname = entry.HostName ?? "";
                    }
                    catch (Exception e) when (e is SocketException or OperationCanceledException
                                                or ArgumentException)
                    {
                        // An address with no PTR record is the common case on a DHCP
                        // segment, not an error worth reporting anywhere.
                    }
                });
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            // The budget ran out. The sweep itself succeeded; some rows simply have no name.
            _log.LogDebug("Reverse DNS budget expired with {Count} host(s) swept", hits.Count);
        }
    }

    private async Task ReportFailureAsync(string scanId, string error, CancellationToken ct)
    {
        try
        {
            await _fleet.ReportSweepAsync(scanId, new JsonObject { ["error"] = error }, ct);
        }
        catch (Exception e)
        {
            // Best effort by design, exactly as ListDirectoryExecutor's is: the command
            // result still carries the reason, and failing over a failed report would tell
            // the operator the sweep broke twice when it broke once.
            _log.LogDebug("Could not report a sweep failure: {Msg}", e.Message);
        }
    }
}
