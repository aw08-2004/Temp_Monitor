using System.Security.Cryptography;
using System.Text;

namespace TempMonitorAgent.Fleet;

/// <summary>
/// Turns an operator's email into a short, stable handle for the agent log.
///
/// The agent log lives in the state directory, where StateDirectory.Harden deliberately
/// leaves BUILTIN\Users at read so support can still open it. That makes anything written
/// there readable by whoever is sitting at the managed PC -- so the log used to hand every
/// standard user the email addresses of the IT staff who had opened a shell on their
/// machine. Who did what is the hub's audit_log's job, and the hub records it against the
/// trusted session; this file only ever needed to tell one operator's lines apart from
/// another's, which a derived tag does just as well.
///
/// The tag is deterministic, so the same operator reads as the same tag across every line,
/// across reboots, and across agent updates -- which is what makes a log still followable.
///
/// It is NOT a secret and is not offered as one: an IT team is small and its address format
/// predictable, so anyone determined could hash the candidates and match a tag back. What it
/// buys is that *reading* the log discloses nothing, which is the case that actually happens.
/// If you need to know which human a tag is, join it to the hub's audit log.
/// </summary>
internal static class OperatorTag
{
    /// <summary>Shown when a command arrives with no issuer -- the hub sets issued_by from
    /// the session, so in practice this means a locally-issued or malformed command.</summary>
    internal const string Unknown = "unknown";

    /// <summary>
    /// Tag for one operator email. Normalised the same way ShellSessionManager.Key does
    /// (trim + lowercase) so the same person tags identically whichever path logged them --
    /// otherwise a differently-cased address would read as a second operator.
    /// </summary>
    internal static string For(string? email)
    {
        var normalised = (email ?? "").Trim().ToLowerInvariant();
        if (normalised.Length == 0) return Unknown;
        var digest = SHA256.HashData(Encoding.UTF8.GetBytes(normalised));
        // Four bytes is plenty to keep a handful of concurrent operators apart in a log, and
        // short enough to stay readable inline.
        return Convert.ToHexString(digest, 0, 4).ToLowerInvariant();
    }
}
