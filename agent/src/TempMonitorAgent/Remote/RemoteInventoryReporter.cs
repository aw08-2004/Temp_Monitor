using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Reports what a remote session would find on this machine: which logon sessions exist, and
/// whether there is anything to capture.
///
/// **Why the hub needs this.** Without it the operator gets two blind spots. First, the target
/// session is auto-picked -- on a machine with a console user and two RDP sessions that is a
/// guess, and the operator usually knows which one they want. Second, a machine with no monitor
/// looks identical to a working one right up until the stream comes back black; reporting zero
/// display outputs turns that into a badge on the machine page and a button that fixes it.
///
/// **Only on change**, following <c>BackupProfileReporter</c>: the payload is handed to a
/// heartbeat only when its content hash differs from what was last sent, so the steady-state
/// 10-second heartbeat stays tiny. Sessions get a shorter refresh than displays because people
/// sign in and out far more often than they plug in monitors.
///
/// Everything here is session-0-safe: WTS enumeration is session-independent, and the display
/// side deliberately uses PnP rather than the session-scoped display APIs (see
/// <see cref="DisplayProbe"/>).
/// </summary>
public static class RemoteInventoryReporter
{
    private static readonly TimeSpan RefreshInterval = TimeSpan.FromSeconds(60);

    private static readonly Lock Gate = new();
    private static DateTimeOffset _lastScan = DateTimeOffset.MinValue;
    private static string _lastSentHash = "";
    private static JsonObject? _pending;

    /// <summary>Re-scan if due. Cheap to call often; does nothing until the interval elapses.
    /// Called from the agent's main loop, off the heartbeat path.</summary>
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
            // Never fatal: a machine whose sessions cannot be enumerated still works remotely,
            // the operator just gets "auto" instead of a picker.
            return;
        }

        var hash = Hash(payload.ToJsonString());
        lock (Gate)
        {
            if (hash == _lastSentHash) return;
            _pending = payload;
        }
    }

    /// <summary>Force the next heartbeat to carry a fresh payload. Used by the refresh command,
    /// so an operator staring at a stale session list can do something about it.</summary>
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

    /// <summary>Build the payload. Public for the tests and for the refresh command's output.</summary>
    public static JsonObject Build()
    {
        var sessions = new JsonArray();
        foreach (var s in SessionProbe.Enumerate())
            sessions.Add(JsonNode.Parse(JsonSerializer.Serialize(s)));

        var displays = JsonNode.Parse(JsonSerializer.Serialize(DisplayProbe.ProbeFromService()));

        return new JsonObject
        {
            ["sessions"] = sessions,
            ["displays"] = displays,
        };
    }

    private static string Hash(string json) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(json)));
}
