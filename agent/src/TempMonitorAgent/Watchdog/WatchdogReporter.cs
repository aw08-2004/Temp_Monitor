using System.Text.Json.Nodes;

namespace TempMonitorAgent.Watchdog;

/// <summary>
/// Carries what the watchdogs have been doing to the hub on the heartbeat (roadmap #20),
/// following <c>BiosInventoryReporter</c> and <c>RemoteInventoryReporter</c> exactly.
///
/// **Change-only, like its neighbours, and here that is not merely about bandwidth.** A
/// healthy fleet reports `ok` on every watchdog forever; sending that every ten seconds would
/// be most of the heartbeat body and would write a row per tick into the hub's event history.
/// What the hub needs is the transitions -- and the hub writes an event only on a change too,
/// so the two ends agree about what "something happened" means.
///
/// **A restart is always a change**, even when the status word did not move: two `restarted`
/// reports in a row are two restarts, and collapsing them would lose exactly the evidence
/// somebody goes looking for. That is why the pending payload is keyed on the last restart
/// timestamp as well as the status -- see <see cref="Record"/>.
///
/// Static, like the other reporters, because it is written by the watchdog loop and read by
/// the heartbeat loop and those are different threads with no object in common.
/// </summary>
public static class WatchdogReporter
{
    private static readonly Lock Gate = new();
    private static readonly Dictionary<int, JsonObject> Pending = [];
    private static readonly Dictionary<int, string> LastSent = [];

    /// <summary>Note one watchdog's current state. Cheap to call every tick: a state matching
    /// what the hub was last told is dropped here rather than travelling.</summary>
    public static void Record(int id, WatchdogStatus status, int restarts, long? lastRestartUnix,
                              string detail)
    {
        var signature = $"{status.Wire()}|{restarts}|{lastRestartUnix}";
        lock (Gate)
        {
            if (LastSent.TryGetValue(id, out var sent) && sent == signature)
            {
                // Already reported and unchanged. Any payload still pending for this id is
                // left alone -- it is a change the hub has not acknowledged yet.
                return;
            }
            Pending[id] = new JsonObject
            {
                ["id"] = id,
                ["status"] = status.Wire(),
                ["restarts"] = restarts,
                ["last_restart_at"] = lastRestartUnix,
                ["detail"] = detail,
            };
        }
    }

    /// <summary>Forget everything reported so far, so the next tick re-sends.
    ///
    /// Called when a new document is adopted: the hub has just changed what this machine
    /// watches, and it needs to hear the current state of every watchdog against the NEW ids
    /// rather than inferring silence as health.</summary>
    public static void Invalidate()
    {
        lock (Gate)
        {
            LastSent.Clear();
        }
    }

    /// <summary>Hand the pending states to a heartbeat, or null when nothing changed.
    ///
    /// The signatures are recorded as sent only here, so a heartbeat that fails re-sends the
    /// same states next time -- the same discipline BiosInventoryReporter follows, and for the
    /// same reason: a dropped report must cost a refresh, never the record of a restart.</summary>
    public static JsonObject? TakeIfChanged()
    {
        lock (Gate)
        {
            if (Pending.Count == 0) return null;
            var states = new JsonArray();
            foreach (var (id, payload) in Pending)
            {
                states.Add(payload.DeepClone());
                LastSent[id] = $"{payload["status"]}|{payload["restarts"]}|"
                             + $"{payload["last_restart_at"]}";
            }
            Pending.Clear();
            return new JsonObject { ["states"] = states };
        }
    }
}
