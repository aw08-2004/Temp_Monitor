using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace TempMonitorAgent.Security;

/// <summary>
/// Carries this machine's encryption posture to the hub on the heartbeat (roadmap #19),
/// following <c>NetworkInventoryReporter</c> and <c>BiosInventoryReporter</c> exactly.
///
/// **Change-only, and scanned off the heartbeat path.** Enumerating volumes, their protectors
/// and each protector's type is a handful of WMI round trips against a provider that insists
/// on an encrypted connection; none of that belongs in front of the call that decides whether
/// this machine reads online. It runs on the agent's inventory loop and leaves a payload
/// behind, handed over only when its content hash changed.
///
/// **Nothing this class sends is a secret.** The payload is volumes, protection states and
/// protector IDs. The recovery passwords are collected by <c>FleetClient</c> only for the IDs
/// the hub answers with in <c>bitlocker_escrow_wanted</c>, and they go to their own endpoint.
///
/// **An hour, which is the longest cadence of any reporter here.** Encryption state changes
/// when somebody turns BitLocker on, suspends it for a firmware update, or adds a protector --
/// events measured in days, not minutes. The payload is hashed, so a settled machine sends
/// nothing at all, and <see cref="Invalidate"/> exists for the one case where waiting an hour
/// would be wrong: a machine that has just told the hub about a protector it could not then
/// hand over should re-offer promptly rather than at the top of the next hour.
/// </summary>
public static class BitLockerInventoryReporter
{
    private static readonly TimeSpan RefreshInterval = TimeSpan.FromHours(1);

    private static readonly Lock Gate = new();
    private static DateTimeOffset _lastScan = DateTimeOffset.MinValue;
    private static string _lastSentHash = "";
    private static JsonObject? _pending;
    private static IReadOnlyList<string> _escrowWanted = [];

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
            // Never fatal, like every reporter here. A machine whose volumes cannot be read is
            // still managed in every other way, and the console says so from the absence of a
            // posture rather than from a broken heartbeat.
            return;
        }

        var hash = Hash(payload.ToJsonString());
        lock (Gate)
        {
            if (hash == _lastSentHash) return;
            _pending = payload;
        }
    }

    /// <summary>Force the next heartbeat to carry a fresh payload.</summary>
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

    public static JsonObject Build() => ToPayload(BitLockerReader.Read());

    /// <summary>The wire shape. Separate and pure so a test can assert on exactly what the hub
    /// will receive without BitLocker anywhere near it -- the two halves of this feature are a
    /// C# reader and a Python ingest, and this object is all that binds them.</summary>
    public static JsonObject ToPayload(BitLockerReport report)
    {
        var volumes = new JsonArray();
        foreach (var volume in report.Volumes)
        {
            var protectors = new JsonArray();
            foreach (var protector in volume.Protectors)
            {
                protectors.Add(new JsonObject
                {
                    ["id"] = protector.Id,
                    ["kind"] = protector.Kind,
                    ["label"] = protector.Label,
                });
            }
            var entry = new JsonObject
            {
                ["mount"] = volume.Mount,
                ["device_id"] = volume.DeviceId,
                ["protection"] = volume.Protection,
                ["conversion"] = volume.Conversion,
                ["method"] = volume.Method,
                ["protectors"] = protectors,
            };
            // Null rather than a sentinel: the hub stores "we were not told" distinctly, and a
            // 0 here would render as a disk that is 0% encrypted.
            entry["percentage"] = volume.Percentage is null
                ? null
                : JsonValue.Create(volume.Percentage.Value);
            volumes.Add(entry);
        }

        return new JsonObject
        {
            ["support"] = report.Support,
            ["error"] = report.Error,
            ["volumes"] = volumes,
        };
    }

    /// <summary>Record what the hub says it is missing, from the heartbeat reply.
    ///
    /// **Stored rather than acted on**, following <c>ProcessReporter.SetWanted</c>: reading a
    /// recovery password is several WMI round trips against a provider that wants an encrypted
    /// connection, and the heartbeat loop is the one thing in this agent that decides whether
    /// the machine reads online. The inventory loop picks this up within its next tick.
    ///
    /// An empty list clears the request, which is how a machine learns to stop offering a key
    /// that has since been escrowed.</summary>
    public static void SetEscrowWanted(IReadOnlyCollection<string> ids)
    {
        lock (Gate) { _escrowWanted = [.. ids]; }
    }

    /// <summary>Take the pending escrow request, or an empty list. Taking clears it, so a hub
    /// that has stopped asking is not answered again from a stale copy -- the next heartbeat
    /// re-states what is still missing, and that is the retry.</summary>
    public static IReadOnlyList<string> TakeEscrowWanted()
    {
        lock (Gate)
        {
            var wanted = _escrowWanted;
            _escrowWanted = [];
            return wanted;
        }
    }

    /// <summary>The escrow submission body for the protector IDs the hub asked for, or null
    /// when there is nothing to send.
    ///
    /// Built here rather than in FleetClient so that the one place that ever holds a recovery
    /// password in this agent is a function with no network in it, and so a test can prove
    /// that an empty request produces no body at all.</summary>
    public static JsonObject? BuildEscrow(IReadOnlyCollection<string> wanted)
    {
        if (wanted.Count == 0) return null;
        var found = BitLockerReader.ReadRecoveryPasswords(wanted);
        if (found.Count == 0) return null;

        var keys = new JsonArray();
        foreach (var (id, entry) in found)
        {
            keys.Add(new JsonObject
            {
                ["protector_id"] = id,
                ["volume"] = entry.Volume,
                ["recovery_password"] = entry.Password,
            });
        }
        return new JsonObject { ["keys"] = keys };
    }

    private static string Hash(string json) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(json)));
}
