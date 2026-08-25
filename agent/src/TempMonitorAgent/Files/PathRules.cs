namespace TempMonitorAgent.Files;

/// <summary>
/// What a path from the hub is allowed to be, checked on the machine that will act on it.
///
/// The hub validates every one of these before it queues a command (see hub/files.py's
/// validate_path), and this is not that check repeated for tidiness. It is the check that
/// counts. The hub's copy exists so a malformed request is refused in the console
/// immediately, with a sentence about what is wrong; THIS copy is what stands between a
/// command arriving on the wire and a delete running as SYSTEM, and it is the one that has
/// to hold if the hub is ever wrong, out of date, or not the hub we think it is.
///
/// The rules are deliberately few, because the interesting ones are not here. There is no
/// list of folders an operator may not touch, and adding one would be theatre: the same
/// operator, with the same capability, already has a SYSTEM shell on this machine through
/// the Terminal tab. What these rules stop is a path that means something DIFFERENT to
/// Windows than it does to the audit record — a relative path resolved against whatever the
/// service's working directory happens to be, a `..` that walks out of the folder the
/// operator was looking at, a device name that is not a file at all.
/// </summary>
public static class PathRules
{
    /// <summary>Same ceiling the hub applies. Long-path support is on by default on current
    /// builds, so MAX_PATH is not the limit; this is where a path stops being plausible.</summary>
    public const int MaxPathChars = 1024;
    public const int MaxNameChars = 260;

    /// <summary>Reject, with a reason, or null when the path is acceptable.
    ///
    /// Returns a REASON rather than throwing because every caller turns it straight into a
    /// CommandResult.Fail that an operator reads — and "path may not contain '..'" is a
    /// sentence somebody can act on, where a stack trace is not.</summary>
    public static string? Reject(string? path)
    {
        var text = (path ?? "").Trim();
        if (text.Length == 0) return "No path was supplied.";
        if (text.Length > MaxPathChars) return $"Path is longer than {MaxPathChars} characters.";
        if (text.Contains('\0')) return "Path contains an invalid character.";

        text = text.Replace('/', '\\');

        var isUnc = text.StartsWith(@"\\", StringComparison.Ordinal);
        if (!isUnc)
        {
            // A drive letter and a colon, and nothing else may open the string. "C:folder"
            // (no separator) is a path relative to the current directory ON C:, which is
            // whatever the service last happened to set — a genuinely different file from
            // the one the operator clicked.
            if (text.Length < 2 || !char.IsLetter(text[0]) || text[1] != ':')
                return "Path must be absolute, like C:\\Users or \\\\server\\share.";
            if (text.Length > 2 && text[2] != '\\')
                return "Path must be absolute, like C:\\Users or \\\\server\\share.";
        }

        foreach (var part in text.Split('\\'))
        {
            if (part == "..") return "Path may not contain '..'.";
        }

        // Win32 device namespaces. \\.\PhysicalDrive0 and \\?\C:\ are not files, and the
        // second is specifically the syntax that turns OFF the normalization every rule
        // above depends on.
        if (text.StartsWith(@"\\.\", StringComparison.Ordinal)
            || text.StartsWith(@"\\?\", StringComparison.Ordinal))
            return "Device paths cannot be browsed.";

        return null;
    }

    /// <summary>Reject one file or folder NAME — no separators, nothing Windows reserves.</summary>
    public static string? RejectName(string? name)
    {
        var text = (name ?? "").Trim();
        if (text.Length == 0) return "No name was supplied.";
        if (text.Length > MaxNameChars) return $"Name is longer than {MaxNameChars} characters.";
        if (text is "." or "..") return "That is not a name.";
        if (text.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0)
            return "Name contains a character Windows does not allow in a filename.";
        // Windows silently drops a trailing dot or space, producing a file under a name
        // nobody typed and which cannot then be found by the name they did.
        if (text[^1] is '.' or ' ') return "A name may not end with a space or a dot.";
        return null;
    }

    /// <summary>The path in the one spelling everything downstream agrees on: backslashes,
    /// no trailing separator except on a drive root.</summary>
    public static string Normalize(string path)
    {
        var text = (path ?? "").Trim().Replace('/', '\\');
        if (text.Length == 3 && text[1] == ':' && text[2] == '\\') return text.ToUpperInvariant();
        if (text.Length == 2 && text[1] == ':') return text.ToUpperInvariant() + "\\";
        return text.TrimEnd('\\');
    }
}
