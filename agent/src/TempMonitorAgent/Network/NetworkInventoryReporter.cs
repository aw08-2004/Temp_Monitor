using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace TempMonitorAgent.Network;

/// <summary>
/// Carries this machine's adapters to the hub on the heartbeat (roadmap #10), following
/// <c>BackupProfileReporter</c>, <c>RemoteInventoryReporter</c> and
/// <c>BiosInventoryReporter</c> exactly.
///
/// **Change-only, and scanned off the heartbeat path.** The scan walks every adapter, two
/// WMI classes and the NIC class registry key; none of that belongs in front of the call
/// that decides whether this machine reads online. It runs on the agent's inventory loop
/// and leaves a payload behind, handed over only when its content hash changed.
///
/// **The cadence sits between the firmware one and the profile one, on purpose.** A MAC
/// never changes, but an IP does -- a laptop moving between the office and a home network
/// changes the subnet the hub would pick a relay on, and a stale subnet means a wake
/// broadcast onto a segment the machine is not on. Fifteen minutes bounds how wrong that
/// can be while still costing a settled desktop nothing: the payload is hashed, so a
/// machine that has not moved sends nothing at all.
/// </summary>
public static class NetworkInventoryReporter
{
    private static readonly TimeSpan RefreshInterval = TimeSpan.FromMinutes(15);

    private static readonly Lock Gate = new();
    private static DateTimeOffset _lastScan = DateTimeOffset.MinValue;
    private static string _lastSentHash = "";
    private static JsonObject? _pending;

    /// <summary>Re-scan if due. Cheap to call often; does nothing until the interval elapses.</summary>
    public static void RefreshIfDue()
    {
        lock (Gate)
        {
            if (DateTimeOffset.UtcNow - _lastScan < RefreshInterval) return;
            _lastScan = DateTimeOffset.UtcNow;
        }

        JsonObject payload;
        try { payload = Build(); }
        catch
        {
            // Never fatal. A machine whose adapters cannot be enumerated is still managed in
            // every other way; it simply cannot be woken, and the console says so from the
            // absence of an inventory rather than from an error.
            return;
        }

        var hash = Hash(payload.ToJsonString());
        lock (Gate)
        {
            if (hash == _lastSentHash) return;
            _pending = payload;
        }
    }

    /// <summary>Force the next heartbeat to carry a fresh payload. Used after `prepare_wake`
    /// changes the very flags this reports -- an operator who has just fixed a machine's wake
    /// settings should not watch the old diagnosis for another quarter of an hour.</summary>
    public static void Invalidate()
    {
        lock (Gate)
        {
            _lastScan = DateTimeOffset.MinValue;
            _lastSentHash = "";
        }
    }

    /// <summary>Hand the pending payload to a heartbeat, or null when nothing changed. The
    /// hash is recorded as sent only here, so a failed heartbeat re-sends next time.</summary>
    public static JsonObject? TakeIfChanged()
    {
        lock (Gate)
        {
            if (_pending is null) return null;
            var payload = _pending;
            _pending = null;
            _lastSentHash = Hash(payload.ToJsonString());
            return payload;
        }
    }

    /// <summary>Read the machine and build the wire payload.</summary>
    public static JsonObject Build() => ToPayload(NicReader.Read());

    /// <summary>The wire shape. Separate and pure so a test can assert on exactly what the
    /// hub will receive without a network adapter anywhere near it -- the two halves of this
    /// feature are a C# reader and a Python ingest, and this object is all that binds them.
    /// </summary>
    public static JsonObject ToPayload(NetworkReport report)
    {
        var nics = new JsonArray();
        foreach (var nic in report.Nics)
        {
            var entry = new JsonObject
            {
                ["mac"] = nic.Mac,
                ["name"] = nic.Name,
                ["description"] = nic.Description,
                ["ipv4"] = nic.Ipv4,
                ["kind"] = nic.Kind,
                ["link_up"] = nic.LinkUp,
            };
            // Null rather than a sentinel for both of these: the hub stores "unknown"
            // distinctly, and a 0 prefix or a false wake flag would be read as a fact.
            entry["prefix"] = nic.Prefix is null ? null : JsonValue.Create(nic.Prefix.Value);
            entry["wake_enabled"] = nic.WakeEnabled is null
                ? null
                : JsonValue.Create(nic.WakeEnabled.Value);
            nics.Add(entry);
        }

        var payload = new JsonObject { ["nics"] = nics };
        payload["fast_startup"] = report.FastStartup is null
            ? null
            : JsonValue.Create(report.FastStartup.Value);
        return payload;
    }

    private static string Hash(string json) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(json)));
}
