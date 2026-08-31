using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace TempMonitorAgent.Patch;

/// <summary>
/// Carries this machine's available updates to the hub on the heartbeat (roadmap #14),
/// following <c>BiosInventoryReporter</c> and <c>NetworkInventoryReporter</c> exactly.
///
/// <para><b>Change-only, and scanned off the heartbeat path.</b> A Windows Update search
/// contacts WSUS or Microsoft Update and routinely takes tens of seconds; `winget upgrade`
/// refreshes its sources over the network. The hub's offline window is 90 s, so performing
/// either inside a heartbeat would eventually mark a healthy machine offline. The scan
/// therefore runs on the agent's inventory loop and merely leaves a payload behind.</para>
///
/// <para><b>The payload is an object, and an empty update list is still sent.</b> Both halves
/// of that sentence are load-bearing. The transition to "nothing available" is the most
/// important report this agent ever makes — it is the only honest evidence that an install
/// worked, and it is what closes out a patch run (hub/patches.py confirm_from_inventory). So
/// the wire shape is <c>{"updates": [...]}</c> rather than a bare array: the hub tests the key
/// with <c>is not None</c> and an object stays truthy when the list inside it is empty, where
/// a bare <c>[]</c> would be discarded by any truthiness check anywhere along the path. The
/// content hash makes "became empty" a change like any other, so it is sent once and then not
/// repeated.</para>
///
/// <para><b>Invalidate() after an install, because six hours is the wrong wait.</b> A machine
/// that has just installed patches and restarted must re-report promptly or its run sits in
/// REBOOTING until the hub's confirm timeout gives up on it. A restart re-launches the agent
/// and resets the interval anyway; Invalidate covers the case where nothing restarted.</para>
/// </summary>
public static class PatchInventoryReporter
{
    /// <summary>Much longer than the sessions/profiles reporters, and for the opposite reason
    /// to BIOS: this is not slow-because-rare, it is slow-because-networked. Six hours is
    /// roughly Windows' own detection cadence, and a patch that appeared five hours ago is not
    /// one anybody is waiting on — the events that DO need promptness (an install finishing, a
    /// restart) call Invalidate instead of being polled for.</summary>
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
        try { payload = ToPayload(PatchScanner.Read()); }
        catch
        {
            // Never fatal. A machine whose updates cannot be enumerated is still a machine the
            // console manages in every other way, and the next scan may well succeed.
            return;
        }

        var hash = Hash(payload.ToJsonString());
        lock (Gate)
        {
            if (hash == _lastSentHash) return;
            _pending = payload;
        }
    }

    /// <summary>Force the next inventory pass to re-scan and the next heartbeat to carry the
    /// result. Called after an install attempt, so the hub learns what actually applied
    /// without waiting out the refresh interval — that report is what closes the run.</summary>
    public static void Invalidate()
    {
        lock (Gate)
        {
            _lastScan = DateTimeOffset.MinValue;
            _lastSentHash = "";
        }
    }

    /// <summary>Hand the pending payload to a heartbeat, or null when nothing changed. The hash
    /// is recorded as sent only here, so a failed heartbeat re-sends next time.</summary>
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

    /// <summary>The wire shape. Separate and pure so a test can assert on what the hub will
    /// receive without a Windows Update Agent anywhere near it — the two halves of this feature
    /// are a C# scanner and a Python ingest, and the only thing binding them is this object.</summary>
    public static JsonObject ToPayload(PatchScanner.Scan scan)
    {
        var updates = new JsonArray();
        foreach (var update in scan.Updates)
        {
            updates.Add(new JsonObject
            {
                ["uid"] = update.Uid,
                ["native_id"] = update.NativeId,
                ["source"] = update.Source,
                ["kb"] = update.Kb,
                ["title"] = update.Title,
                ["classification"] = update.Classification,
                ["reboot_required"] = update.RebootRequired,
                ["size_bytes"] = update.SizeBytes,
            });
        }
        return new JsonObject
        {
            // Always present, even when empty. See the class remarks -- this is the whole
            // reason the payload is an object rather than the array itself.
            ["updates"] = updates,
            ["error"] = scan.Error,
        };
    }

    private static string Hash(string text) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text)));
}
