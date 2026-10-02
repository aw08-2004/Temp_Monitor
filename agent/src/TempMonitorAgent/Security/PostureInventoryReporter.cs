using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace TempMonitorAgent.Security;

/// <summary>
/// Carries this PC's security posture to the hub on the heartbeat (roadmap #25 D), following
/// <c>SoftwareInventoryReporter</c> exactly.
///
/// <para><b>Change-only, and scanned off the heartbeat path.</b> Four WMI namespaces, a COM
/// object, a NetAPI call and a few registry reads -- each quick, and together nothing that
/// belongs in front of the call that decides whether this machine reads online. It runs on
/// the inventory loop and leaves a payload behind, handed over only when its content hash
/// changed, so a settled PC sends this once.</para>
///
/// <para><b>Acknowledged only after the hub says it stored it</b>, the
/// <c>software_rejected</c> pattern: the heartbeat answers 200 even when one inventory write
/// fails, and acknowledging then would hash-suppress a posture the hub never kept until the
/// next time something on the PC changed.</para>
///
/// <para><b>An hour.</b> The interesting changes -- somebody switching the firewall off,
/// adding themselves to Administrators, a product's signatures going stale -- are the ones a
/// helpdesk wants within the hour and does not need within the minute. Signature age is the
/// one value that moves daily on its own, which costs one small payload a day per PC.</para>
/// </summary>
public static class PostureInventoryReporter
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
        try { payload = ToPayload(PostureReader.Read()); }
        catch
        {
            // PostureReader.Read does not throw; this is the reporter's own never-fatal rule,
            // kept so a bug in the payload builder costs a stale posture rather than the loop.
            return;
        }

        var hash = Hash(payload.ToJsonString());
        lock (Gate)
        {
            if (hash == _lastSentHash) return;
            _pending = payload;
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
    /// handed the heartbeat -- as delivered. The object is passed in rather than re-read, for
    /// the reason <c>SoftwareInventoryReporter.AckSent</c> gives: a scan that lands while the
    /// heartbeat is in flight must not be acknowledged unsent.</summary>
    public static void AckSent(JsonObject sent)
    {
        lock (Gate)
        {
            _lastSentHash = Hash(sent.ToJsonString());
            if (ReferenceEquals(_pending, sent)) _pending = null;
        }
    }

    /// <summary>The wire shape. Separate and pure so a test can assert on exactly what the hub
    /// will receive -- hub/posture.py parses this object and nothing else binds the two halves.
    ///
    /// Every area is always present with an `error` string, and an unknown value is JSON
    /// null, never a default: the hub tells "the firewall is off" from "we could not ask"
    /// by exactly that difference.</summary>
    public static JsonObject ToPayload(PostureReport report)
    {
        var products = new JsonArray();
        foreach (var p in report.Antivirus.Products)
        {
            products.Add(new JsonObject
            {
                ["name"] = p.Name,
                ["state"] = p.State,
                ["enabled"] = p.Enabled,
                ["up_to_date"] = p.UpToDate,
            });
        }

        var profiles = new JsonArray();
        foreach (var f in report.Firewall.Profiles)
            profiles.Add(new JsonObject { ["name"] = f.Name, ["enabled"] = f.Enabled });

        var users = new JsonArray();
        foreach (var u in report.SessionLock.Users)
        {
            users.Add(new JsonObject
            {
                ["sid"] = u.Sid,
                ["active"] = u.Active,
                ["secure"] = u.Secure,
                ["timeout_seconds"] = u.TimeoutSeconds,
            });
        }

        var local = new JsonArray();
        foreach (var a in report.Accounts.Local)
            local.Add(new JsonObject { ["name"] = a.Name, ["sid"] = a.Sid, ["enabled"] = a.Enabled });

        var admins = new JsonArray();
        foreach (var m in report.Accounts.Administrators)
        {
            admins.Add(new JsonObject
            {
                ["name"] = m.Name,
                ["sid"] = m.Sid,
                ["kind"] = m.Kind,
                ["local"] = m.Local,
            });
        }

        var d = report.Defender;
        var t = report.Tpm;
        return new JsonObject
        {
            ["antivirus"] = new JsonObject
            {
                ["supported"] = report.Antivirus.Supported,
                ["products"] = products,
                ["error"] = report.Antivirus.Error,
            },
            ["defender"] = new JsonObject
            {
                ["present"] = d.Present,
                ["antivirus_enabled"] = d.AntivirusEnabled,
                ["realtime"] = d.RealTime,
                ["signature_age_days"] = d.SignatureAgeDays,
                ["signature_updated"] = d.SignatureUpdated,
                ["mode"] = d.Mode,
                ["error"] = d.Error,
            },
            ["firewall"] = new JsonObject
            {
                ["profiles"] = profiles,
                ["error"] = report.Firewall.Error,
            },
            ["autorun"] = new JsonObject
            {
                ["no_drive_type_autorun"] = report.Autorun.NoDriveTypeAutoRun,
                ["no_autorun"] = report.Autorun.NoAutorun,
                ["error"] = report.Autorun.Error,
            },
            ["session_lock"] = new JsonObject
            {
                ["machine_inactivity_seconds"] = report.SessionLock.MachineInactivitySeconds,
                ["users"] = users,
                ["error"] = report.SessionLock.Error,
            },
            ["accounts"] = new JsonObject
            {
                ["local"] = local,
                ["administrators"] = admins,
                ["error"] = report.Accounts.Error,
            },
            ["secure_boot"] = new JsonObject
            {
                ["state"] = report.SecureBoot.State,
                ["error"] = report.SecureBoot.Error,
            },
            ["tpm"] = new JsonObject
            {
                ["present"] = t.Present,
                ["enabled"] = t.Enabled,
                ["activated"] = t.Activated,
                ["spec_version"] = t.SpecVersion,
                ["error"] = t.Error,
            },
        };
    }

    private static string Hash(string text) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text)));
}
