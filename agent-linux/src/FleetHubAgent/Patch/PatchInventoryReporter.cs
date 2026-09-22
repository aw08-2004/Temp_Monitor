using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Patch;

/// <summary>
/// Carries this machine's available updates to the hub on the heartbeat (roadmap #14's Linux
/// half, listed under #22), following the Windows agent's PatchInventoryReporter.
///
/// <para><b>Change-only, and scanned off the heartbeat path.</b> The scan itself is seconds of
/// dependency solving; the hub's offline window is 90 seconds, so performing it inside a
/// heartbeat would eventually mark a healthy machine offline. It therefore runs on the
/// agent's inventory loop and merely leaves a payload behind for the next heartbeat to
/// carry.</para>
///
/// <para><b>The payload is an object, and an empty update list is still sent.</b> Both halves
/// of that sentence are load-bearing, and they are the hub's requirement rather than a style
/// choice. The transition to "nothing available" is the most important report this agent ever
/// makes -- it is the only honest evidence that an install worked, and it is what closes out a
/// patch run (hub/patches.py confirm_from_inventory). So the wire shape is
/// <c>{"updates": [...]}</c> rather than a bare array: the hub tests the key with
/// <c>is not None</c>, and an object stays truthy when the list inside it is empty where a
/// bare <c>[]</c> would be discarded by any truthiness check anywhere along the path.</para>
///
/// <para><b>The hash is recorded only once a heartbeat has actually carried the payload</b>,
/// which is the one place this diverges from the Windows reporter. There, TakeIfChanged marks
/// the content sent as it hands it over, so a heartbeat that then fails loses the report until
/// the next scan finds DIFFERENT content -- and the report that matters most is precisely the
/// one whose content does not change again for hours. Here the two steps are separate:
/// <see cref="TakeIfChanged"/> hands over a payload, and <see cref="MarkSent"/> is called by
/// the heartbeat loop only after the hub answered. A failed heartbeat re-sends on the next
/// one.</para>
/// </summary>
public sealed class PatchInventoryReporter(ILogger<PatchInventoryReporter> log, PatchScanner scanner)
{
    /// <summary>Six hours, matching the Windows reporter. This is slow-because-expensive
    /// rather than slow-because-rare, and a package that appeared five hours ago is not one
    /// anybody is waiting on -- the event that DOES need promptness (an install having
    /// happened) calls <see cref="Invalidate"/> instead of being polled for.</summary>
    private static readonly TimeSpan RefreshInterval = TimeSpan.FromHours(6);

    private readonly object _gate = new();
    private DateTimeOffset _lastScan = DateTimeOffset.MinValue;
    private string _lastSentHash = "";
    private JsonObject? _pending;

    /// <summary>Re-scan if due. Cheap to call often; does nothing until the interval elapses.
    /// Never throws -- a machine whose packages cannot be enumerated is still a machine the
    /// console manages in every other way, and the next scan may well succeed.</summary>
    public async Task RefreshIfDueAsync(CancellationToken ct)
    {
        lock (_gate)
        {
            if (DateTimeOffset.UtcNow - _lastScan < RefreshInterval) return;
            // Claimed BEFORE the scan, not after: the scan takes seconds and the inventory
            // loop must not be able to start a second one on top of the first.
            _lastScan = DateTimeOffset.UtcNow;
        }

        JsonObject payload;
        try
        {
            var scan = await scanner.ReadAsync(ct);
            payload = ToPayload(scan);
            if (scan.Error is { Length: > 0 } error)
                log.LogWarning("Patch inventory scan failed: {Error}", error);
            else
                log.LogInformation("Patch inventory: {Count} update(s) available", scan.Updates.Count);
        }
        catch (OperationCanceledException) { return; }
        catch (Exception e)
        {
            log.LogWarning(e, "Patch inventory scan threw");
            return;
        }

        var hash = Hash(payload.ToJsonString());
        lock (_gate)
        {
            if (hash == _lastSentHash)
            {
                _pending = null;
                return;
            }
            _pending = payload;
        }
    }

    /// <summary>Force the next inventory pass to re-scan and the next heartbeat to carry the
    /// result. Nothing calls it yet -- installing patches from the console is not implemented
    /// on Linux, and the agent's capability report says so, which is what stops the hub
    /// queuing one. It exists because the moment that lands, the hub's run would otherwise sit
    /// in REBOOTING for up to six hours waiting for an inventory that had no reason to be
    /// early.</summary>
    public void Invalidate()
    {
        lock (_gate)
        {
            _lastScan = DateTimeOffset.MinValue;
            _lastSentHash = "";
        }
    }

    /// <summary>The payload the next heartbeat should carry, or null when nothing changed.
    /// Does NOT mark it sent -- see <see cref="MarkSent"/> and the class remarks.</summary>
    public JsonObject? TakeIfChanged()
    {
        lock (_gate) return _pending;
    }

    /// <summary>Record that the hub accepted <paramref name="payload"/>, so it is not sent
    /// again until its content changes. Compared by reference: a scan that completed while
    /// the heartbeat was in flight has already replaced _pending with something the hub has
    /// not seen, and marking THAT sent would drop it.</summary>
    public void MarkSent(JsonObject payload)
    {
        lock (_gate)
        {
            if (!ReferenceEquals(_pending, payload)) return;
            _pending = null;
            _lastSentHash = Hash(payload.ToJsonString());
        }
    }

    /// <summary>The wire shape. Separate and pure so a test can assert on what the hub will
    /// receive without a package manager anywhere near it -- the two halves of this feature
    /// are a C# scanner and a Python ingest, and the only thing binding them is this
    /// object.</summary>
    public static JsonObject ToPayload(PatchScan scan)
    {
        var updates = new JsonArray();
        foreach (var update in scan.Updates)
        {
            updates.Add(new JsonObject
            {
                ["uid"] = update.Uid,
                ["source"] = update.Source,
                ["title"] = update.Title,
                ["classification"] = update.Classification,
                ["reboot_required"] = update.RebootRequired,
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
