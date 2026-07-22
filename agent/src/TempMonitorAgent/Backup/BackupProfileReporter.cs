using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace TempMonitorAgent.Backup;

/// <summary>
/// Reports this machine's user profiles and resolved known folders to the hub, so the
/// console can preview what a backup pattern actually covers here. Roadmap #1b.
///
/// **Why the hub needs this at all.** `%Users%\Desktop` tells an operator nothing about
/// whether it covers anything on a given PC. The resolved list does — and it is the only
/// way to notice, before the first restore, that a machine's Documents folder is
/// redirected into OneDrive or that one user has no Pictures folder recorded.
///
/// **Only on change.** Profile discovery reads the registry and may mount a logged-off
/// user's hive, so it is far too expensive for a 10-second heartbeat, and the payload
/// would be pure noise on a machine where nothing has changed. Discovery runs on a slow
/// timer and the result is only handed to a heartbeat when its content hash differs from
/// what was last sent — the same content-hash discipline the hub uses for agent config,
/// and for the same reason: a counter would churn the fleet on every restart.
/// </summary>
public static class BackupProfileReporter
{
    /// <summary>How often discovery re-runs. A new user signing in or OneDrive redirecting
    /// a folder is a once-in-a-while event; an hour is far more often than it needs to be
    /// and still cheap.</summary>
    private static readonly TimeSpan RefreshInterval = TimeSpan.FromHours(1);

    private static readonly Lock Gate = new();
    private static DateTimeOffset _lastScan = DateTimeOffset.MinValue;
    private static string _lastSentHash = "";
    private static JsonObject? _pending;

    /// <summary>Re-scan if due. Cheap to call often; does nothing until the interval
    /// elapses. Called from the agent's main loop, off the heartbeat path, so a slow
    /// registry read can never delay a heartbeat.</summary>
    public static void RefreshIfDue()
    {
        lock (Gate)
        {
            if (DateTimeOffset.UtcNow - _lastScan < RefreshInterval) return;
            _lastScan = DateTimeOffset.UtcNow;
        }

        JsonObject payload;
        try
        {
            payload = Build(PathExpander.Discover());
        }
        catch (Exception)
        {
            // Never fatal: a machine whose profiles cannot be read still backs up its
            // literal paths, and the console simply cannot preview for it.
            return;
        }

        var hash = Hash(payload.ToJsonString());
        lock (Gate)
        {
            if (hash == _lastSentHash) return;
            _pending = payload;
        }
    }

    /// <summary>The payload to attach to the next heartbeat, or null if nothing changed.
    /// Marks it as sent — a failed heartbeat therefore drops one report, which the next
    /// refresh re-offers.</summary>
    public static JsonNode? TakeIfChanged()
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

    /// <summary>Force the next refresh to re-scan. Used after an update, where the
    /// previous build's cadence should not delay the first report.</summary>
    public static void Invalidate()
    {
        lock (Gate) { _lastScan = DateTimeOffset.MinValue; }
    }

    private static JsonObject Build(MachineProfiles profiles)
    {
        var env = new JsonObject();
        foreach (var (key, value) in profiles.Env) env[key] = value;

        var users = new JsonArray();
        foreach (var user in PathExpander.RealUsers(profiles))
        {
            var folders = new JsonObject();
            foreach (var (token, path) in user.Folders) folders[token] = path;
            users.Add(new JsonObject
            {
                ["name"] = user.Name,
                ["sid"] = user.Sid,
                ["path"] = user.Path,
                ["folders"] = folders,
            });
        }

        return new JsonObject
        {
            ["profile_root"] = profiles.ProfileRoot,
            ["env"] = env,
            ["users"] = users,
        };
    }

    private static string Hash(string text) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text)))[..16];
}
