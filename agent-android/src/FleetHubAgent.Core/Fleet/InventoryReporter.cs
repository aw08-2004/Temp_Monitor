using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace FleetHubAgent.Fleet;

/// <summary>
/// One change-only inventory block on the heartbeat.
///
/// The Windows agent has five of these -- BIOS settings, network adapters, backup profiles,
/// available patches, logon sessions -- and each is a static class that repeats the same
/// twenty lines. Here it is written once and tested once, because it is a pattern with three
/// separate ways to be subtly wrong and none of them announce themselves:
///
/// **1. A block is not "sent" until a heartbeat has actually succeeded**, which is one step
/// stricter than the Windows original this is modelled on. There, TakeIfChanged records the
/// hash as it hands the payload over, so a heartbeat that fails a moment later leaves the
/// agent convinced it has already told the hub -- and because the next scan finds the same
/// content, the block is never re-sent until it changes again. One network blip therefore costs
/// a report, and the symptom is an inventory in the console that is stale with nothing
/// anywhere saying so. Here the payload stays pending until <see cref="MarkSent"/> is called,
/// which the heartbeat only does on a 2xx.
///
/// **2. "Became empty" is a change like any other.** A machine that had forty apps and now has
/// none must report that, and the transition is often the most important report the block ever
/// makes -- on Windows, an empty patch list is the only honest evidence an install worked. So
/// the payload is always a JSON OBJECT wrapping the list rather than a bare array: the hub
/// tests these keys with `is not None`, and an object stays truthy when the list inside it is
/// empty where a bare `[]` would be discarded by any truthiness check along the way. That is
/// PatchInventoryReporter's lesson, and this class is where the Android agent inherits it.
///
/// **3. Reading is slow and must not happen on the heartbeat path.** Enumerating installed
/// packages walks every app on the device. The hub's offline window is 90 seconds, so doing
/// that inside a heartbeat would eventually mark a healthy device offline. The inventory loop
/// does the reading and leaves a payload behind; the heartbeat only picks it up.
///
/// Instance-based rather than static, unlike the Windows originals, so a test can hold several
/// and so the composition root owns their lifetime like everything else here.
/// </summary>
public sealed class InventoryReporter(string key, TimeSpan refreshInterval)
{
    private readonly object _gate = new();
    private DateTimeOffset _lastRead = DateTimeOffset.MinValue;
    private string _lastSentHash = "";
    private JsonObject? _pending;

    /// <summary>The heartbeat key this block travels under, and the name the hub ingests it by
    /// (see hub/fleet_web.py's agent_heartbeat).</summary>
    public string Key { get; } = key;

    /// <summary>Whether a re-read is due. Asked by the inventory loop so the READING -- which
    /// is the expensive half -- happens on that loop's cadence rather than on every tick.</summary>
    public bool IsDue(DateTimeOffset now)
    {
        lock (_gate) return now - _lastRead >= refreshInterval;
    }

    /// <summary>Take a fresh reading. `payload` is null when the read failed, which is never
    /// fatal: a device whose apps cannot be enumerated is still a device the console manages in
    /// every other way, and the next pass may well succeed. A failed read does NOT reset the
    /// interval, so a transient failure is retried on the next tick rather than in six hours.</summary>
    public void Offer(JsonObject? payload, DateTimeOffset now)
    {
        if (payload is null) return;
        var hash = Hash(payload.ToJsonString());
        lock (_gate)
        {
            _lastRead = now;
            if (hash == _lastSentHash) return;
            _pending = payload;
        }
    }

    /// <summary>The pending payload, or null when nothing has changed since the hub last
    /// accepted one.
    ///
    /// **Does not clear it.** Looking at a payload is not the same as delivering one -- see
    /// <see cref="MarkSent"/> and the class summary. Safe to call repeatedly; a heartbeat that
    /// fails simply sees the same block again next time.</summary>
    public JsonObject? Pending()
    {
        lock (_gate) return _pending;
    }

    /// <summary>The heartbeat carrying this block came back 2xx. Only now is the content
    /// recorded as delivered, and only now does the block stop being sent.
    ///
    /// The payload it was called for is passed back rather than assumed, so a re-read that
    /// landed between the take and the acknowledgement is not silently marked as sent -- that
    /// race is rare and its symptom would be one lost change, which is exactly the class of bug
    /// this whole design exists to avoid.</summary>
    public void MarkSent(JsonObject payload)
    {
        var hash = Hash(payload.ToJsonString());
        lock (_gate)
        {
            if (_pending is not null && Hash(_pending.ToJsonString()) != hash) return;
            _pending = null;
            _lastSentHash = hash;
        }
    }

    /// <summary>Force the next pass to re-read and the next heartbeat to carry the result.
    ///
    /// For the events that must not wait out the interval: an app policy has just been applied,
    /// so the console has to learn what is actually installed and suspended rather than showing
    /// yesterday's list beside today's policy. The Windows agent calls the equivalent after a
    /// patch install for the same reason.</summary>
    public void Invalidate()
    {
        lock (_gate)
        {
            _lastRead = DateTimeOffset.MinValue;
            _lastSentHash = "";
        }
    }

    private static string Hash(string text) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text)));
}

/// <summary>
/// Something slow and local worth telling the hub about, read on the inventory loop.
///
/// Deliberately tiny and deliberately in Core: the loop, the change detection and the wire
/// contract all live here where they are testable on a workstation, and the platform half only
/// has to answer "here is what I found".
/// </summary>
public interface IInventorySource
{
    /// <summary>The heartbeat key this block travels under. Must match the name
    /// hub/fleet_web.py ingests, or the payload is sent, accepted and silently ignored.</summary>
    string Key { get; }

    /// <summary>How often to re-read. Slow-because-local rather than slow-because-networked, so
    /// this is measured in minutes rather than the Windows patch scanner's hours.</summary>
    TimeSpan RefreshInterval { get; }

    /// <summary>Read it, or return null if the read failed.
    ///
    /// **Must not throw, and must not return null merely because the answer is empty.** Null
    /// means "I could not look"; an object wrapping an empty list means "I looked, and there is
    /// nothing", and the hub treats those as completely different reports.</summary>
    JsonObject? Read();
}
