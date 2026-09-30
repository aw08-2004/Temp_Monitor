using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace TempMonitorAgent.Software;

/// <summary>
/// Carries this PC's installed programs to the hub on the heartbeat (roadmap #25 B), following
/// <c>BitLockerInventoryReporter</c> exactly.
///
/// <para><b>Change-only, and scanned off the heartbeat path.</b> A registry walk of three
/// Uninstall roots plus every loaded profile is a few hundred key opens -- well under a second
/// on a healthy machine, and nothing that belongs in front of the call that decides whether
/// this machine reads online. It runs on the inventory loop and leaves a payload behind,
/// handed over only when its content hash changed. A settled PC sends this once.</para>
///
/// <para><b>The payload is an object, and an empty list is still sent</b> -- the shape
/// <c>PatchInventoryReporter</c> documents at length. "This PC now has nothing installed from
/// that publisher" is a real transition, and the hub tests the key with <c>is not None</c>;
/// a bare <c>[]</c> would be dropped by any truthiness check along the way.</para>
///
/// <para><b>Acknowledged only after a heartbeat succeeds</b>, the BitLocker reporter's
/// <see cref="AckSent"/> pattern rather than the patch reporter's take-and-forget: a failed
/// heartbeat leaves the same payload eligible for the next one, so a network blip at the
/// wrong second cannot leave the hub a whole refresh interval behind.</para>
///
/// <para><b>An hour, plus <see cref="Invalidate"/> after a deploy.</b> What is installed
/// changes when somebody installs something, and the one route this agent knows about -- a
/// <c>deploy_package</c> -- invalidates on every exit, so the device sheet is right within one
/// inventory pass of a push rather than at the top of the next hour.</para>
/// </summary>
public static class SoftwareInventoryReporter
{
    private static readonly TimeSpan RefreshInterval = TimeSpan.FromHours(1);

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
        try { payload = ToPayload(SoftwareReader.Read()); }
        catch
        {
            // Never fatal, and -- the important half -- nothing is sent. SoftwareReader throws
            // when it could not read HKLM at all, and an empty list in its place would tell
            // the hub this PC has nothing installed.
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
    /// result, whether or not it changed.</summary>
    public static void Invalidate()
    {
        lock (Gate)
        {
            _lastScan = DateTimeOffset.MinValue;
            _lastSentHash = "";
        }
    }

    /// <summary>The pending payload for a heartbeat, or null when nothing changed. Does not
    /// clear it -- see <see cref="AckSent"/>.</summary>
    public static JsonObject? TakeIfChanged()
    {
        lock (Gate)
        {
            return _pending;
        }
    }

    /// <summary>Mark <paramref name="sent"/> -- the exact object <see cref="TakeIfChanged"/>
    /// handed the heartbeat -- as delivered.
    ///
    /// <b>The object is passed in, not re-read from <see cref="_pending"/>.</b> The inventory
    /// loop can replace the pending payload while a heartbeat is in flight; acknowledging
    /// whatever is pending at the time the reply lands would clear the NEWER scan, which the
    /// hub never received, and leave the device sheet a whole refresh interval behind. So the
    /// sent content's hash is recorded, and the pending slot is cleared only if it still holds
    /// that same object. Found in review.</summary>
    public static void AckSent(JsonObject sent)
    {
        lock (Gate)
        {
            _lastSentHash = Hash(sent.ToJsonString());
            if (ReferenceEquals(_pending, sent)) _pending = null;
        }
    }

    /// <summary>The wire shape. Separate and pure so a test can assert on exactly what the hub
    /// will receive -- the two halves of this feature are a C# registry walk and a Python
    /// ingest (hub/software.py), and this object is all that binds them.</summary>
    public static JsonObject ToPayload(SoftwareReport report)
    {
        var items = new JsonArray();
        foreach (var s in report.Items)
        {
            items.Add(new JsonObject
            {
                ["id"] = s.Id,
                ["name"] = s.Name,
                ["version"] = s.Version,
                ["publisher"] = s.Publisher,
                ["install_date"] = s.InstallDate,
                ["install_location"] = s.InstallLocation,
                ["uninstall_string"] = s.UninstallString,
                ["scope"] = s.Scope,
                ["user_sid"] = s.UserSid,
                ["arch"] = s.Arch,
            });
        }
        return new JsonObject
        {
            // Always present, even when empty. See the class remarks.
            ["software"] = items,
            ["error"] = report.Error,
        };
    }

    private static string Hash(string text) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text)));
}
