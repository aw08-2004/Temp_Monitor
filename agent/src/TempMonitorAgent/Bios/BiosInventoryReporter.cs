using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace TempMonitorAgent.Bios;

/// <summary>
/// Carries this machine's firmware settings to the hub on the heartbeat (roadmap #9),
/// following <c>BackupProfileReporter</c> and <c>RemoteInventoryReporter</c> exactly.
///
/// **Change-only, and scanned off the heartbeat path.** A BIOS enumeration is slow -- WMI
/// namespace connect plus three class queries, seconds on some vendors -- and the hub's
/// offline window is 90 s. Performing it inside a heartbeat would eventually mark a healthy
/// machine offline; performing it every 10 s would do nothing else. So the scan runs on the
/// agent's inventory loop and merely leaves a payload behind, and the payload is handed over
/// only when its content hash changed.
///
/// **Firmware changes when a human changes it**, which is rarely and never on a schedule, so
/// the refresh interval is much longer than the sessions/profiles ones. The steady state for
/// the whole fleet is one payload per machine, ever -- including the VMs, which report
/// `unsupported` once and then go quiet.
/// </summary>
public static class BiosInventoryReporter
{
    private static readonly TimeSpan RefreshInterval = TimeSpan.FromHours(6);

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
            // Never fatal. A machine whose firmware cannot be enumerated is still a machine
            // the console manages in every other way; the tab says "could not read" and the
            // rest of the page is unaffected.
            return;
        }

        var hash = Hash(payload.ToJsonString());
        lock (Gate)
        {
            if (hash == _lastSentHash) return;
            _pending = payload;
        }
    }

    /// <summary>Force the next heartbeat to carry a fresh payload. Used by the
    /// refresh_bios_inventory command, so an operator who has just changed a setting in the
    /// BIOS by hand does not wait six hours to see it.</summary>
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

    /// <summary>Read the machine and build the wire payload. Public for the tests and for the
    /// refresh command, which returns it inline so the operator sees the answer without
    /// waiting for a heartbeat.</summary>
    public static JsonObject Build() =>
        ToPayload(BiosReader.Read(BiosReader.Manufacturer(), BiosReader.BiosVersion(),
                                  BiosReader.Query));

    /// <summary>The wire shape. Separate and pure so a test can assert on what the hub will
    /// receive without a vendor namespace anywhere near it -- the two halves of this feature
    /// are a C# reader and a Python ingest, and the only thing binding them is this object.</summary>
    public static JsonObject ToPayload(BiosReport report)
    {
        var settings = new JsonArray();
        foreach (var item in report.Items)
        {
            var values = new JsonArray();
            foreach (var value in item.PossibleValues) values.Add(value);
            settings.Add(new JsonObject
            {
                ["name"] = item.Name,
                ["value"] = item.Value,
                ["kind"] = item.Kind.ToString().ToLowerInvariant(),
                ["possible_values"] = values,
                ["read_only"] = item.ReadOnly,
                ["display_name"] = item.DisplayName,
            });
        }

        var payload = new JsonObject
        {
            ["support"] = report.Support.ToString().ToLowerInvariant(),
            ["vendor"] = report.Vendor,
            ["interface"] = report.Interface,
            ["bios_version"] = report.BiosVersion,
            ["error"] = report.Error,
            ["settings"] = settings,
        };
        // Null, not false, when the vendor gave us no way to ask -- the hub stores the
        // distinction and the console needs it before telling anyone a write will work.
        payload["password_set"] = report.PasswordSet is null
            ? null
            : JsonValue.Create(report.PasswordSet.Value);
        return payload;
    }

    private static string Hash(string json) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(json)));
}
