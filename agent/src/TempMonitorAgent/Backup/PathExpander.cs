using System.Text;
using System.Text.RegularExpressions;
using Microsoft.Win32;

namespace TempMonitorAgent.Backup;

/// <summary>One user profile on this machine, with its resolved known folders.</summary>
public sealed record UserProfile(string Name, string Sid, string Path,
                                 IReadOnlyDictionary<string, string> Folders);

/// <summary>What the machine looks like to the path grammar. Also what gets reported to
/// the hub on the heartbeat, so the console can preview what a pattern resolves to here.</summary>
public sealed record MachineProfiles(string ProfileRoot,
                                     IReadOnlyDictionary<string, string> Env,
                                     IReadOnlyList<UserProfile> Users);

/// <summary>
/// Expands backup path patterns against this machine's real profiles. Roadmap #1b.
///
/// SECOND IMPLEMENTATION OF backup_paths.py's grammar — the hub validates and previews
/// patterns, this expands them for real. Both are driven by the same fixture,
/// tests/backup_path_vectors.json, because two implementations of one grammar drift
/// silently and the symptom is a folder that quietly stops being backed up. Change the
/// grammar in both files and the fixture in the same commit, never one alone.
///
/// The single most important thing here is <see cref="ResolveKnownFolder"/>. A literal
/// C:\Users\bob\Desktop is an empty stub on any machine using OneDrive Known Folder Move,
/// and backing that up nightly while reporting success is the exact failure this feature
/// exists to prevent — so %Desktop% reads each user's own shell-folder registry instead of
/// assuming the folder sits under the profile. When a user's hive is not loaded (they are
/// not signed in) their NTUSER.DAT is mounted temporarily to read it; if even that fails
/// the folder is reported MISSING rather than guessed at, because a guess is what produces
/// the empty-stub backup.
/// </summary>
public static class PathExpander
{
    private const string ProfileListKey =
        @"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList";
    private const string ShellFoldersSubKey =
        @"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders";

    /// <summary>Token -> the registry value name under User Shell Folders. Mirrors
    /// backup_paths.KNOWN_FOLDERS exactly, including the fact that Documents is stored
    /// under the name "Personal".</summary>
    private static readonly Dictionary<string, string> KnownFolders = new(StringComparer.OrdinalIgnoreCase)
    {
        ["desktop"] = "Desktop",
        ["documents"] = "Personal",
        ["downloads"] = "{374DE290-123F-4565-9164-39C4925E467B}",
        ["pictures"] = "My Pictures",
        ["music"] = "My Music",
        ["videos"] = "My Video",
        ["favorites"] = "Favorites",
        ["appdata"] = "AppData",
        ["localappdata"] = "Local AppData",
    };

    private static readonly string[] MachineTokens =
        ["programdata", "systemdrive", "windir", "programfiles", "programfiles(x86)",
         "public", "systemroot"];

    /// <summary>Profile folders that are never a person. Mirrors backup_paths.NON_USER_PROFILES.</summary>
    private static readonly HashSet<string> NonUserProfiles = new(StringComparer.OrdinalIgnoreCase)
    {
        "public", "default", "default user", "all users", "defaultuser0",
        "systemprofile", "localservice", "networkservice",
    };

    private static readonly Regex TokenRe = new(@"%([^%]*)%", RegexOptions.Compiled);

    // ---------------------------------------------------------------- normalisation

    /// <summary>Windows separators, no trailing slash, no doubled separators. Mirrors
    /// backup_paths.normalize.</summary>
    public static string Normalize(string? path)
    {
        var text = (path ?? "").Trim().Replace('/', '\\');
        if (text.Length == 0) return "";
        bool unc = text.StartsWith(@"\\", StringComparison.Ordinal);
        text = Regex.Replace(text, @"\\{2,}", @"\");
        if (unc) text = @"\" + text;
        if (text.Length > 3 && text.EndsWith('\\')) text = text.TrimEnd('\\');
        return text;
    }

    // ---------------------------------------------------------------- discovery

    /// <summary>
    /// Read this machine's profiles and known folders. Never throws — a registry that
    /// cannot be read yields fewer users, and the run backs up what it can rather than
    /// failing wholesale.
    /// </summary>
    public static MachineProfiles Discover()
    {
        var env = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (var name in new[] { "ProgramData", "SystemDrive", "windir", "ProgramFiles",
                                     "ProgramFiles(x86)", "Public", "SystemRoot" })
        {
            var value = Environment.GetEnvironmentVariable(name);
            if (!string.IsNullOrWhiteSpace(value)) env[name] = Normalize(value);
        }

        var users = new List<UserProfile>();
        string profileRoot = Normalize(Environment.GetEnvironmentVariable("SystemDrive") + @"\Users");
        try
        {
            using var profileList = Registry.LocalMachine.OpenSubKey(ProfileListKey);
            if (profileList is not null)
            {
                foreach (var sid in profileList.GetSubKeyNames())
                {
                    // Only real interactive accounts: machine SIDs are S-1-5-21-...,
                    // while the built-in service profiles (S-1-5-18/19/20) are not people.
                    if (!sid.StartsWith("S-1-5-21-", StringComparison.OrdinalIgnoreCase)) continue;
                    using var entry = profileList.OpenSubKey(sid);
                    var path = Normalize(entry?.GetValue("ProfileImagePath") as string);
                    if (string.IsNullOrEmpty(path)) continue;

                    var name = path.Split('\\').LastOrDefault() ?? "";
                    if (name.Length == 0 || NonUserProfiles.Contains(name)) continue;

                    users.Add(new UserProfile(name, sid, path, ReadKnownFolders(sid, path)));
                }
            }
        }
        catch (Exception)
        {
            // Fall through with whatever was gathered. A machine we cannot enumerate is a
            // machine that backs up its literal paths only, which is still better than
            // failing the run.
        }

        return new MachineProfiles(profileRoot, env, users);
    }

    /// <summary>
    /// One user's known folders, resolved through their own registry hive.
    ///
    /// If HKU has the hive (the user is signed in) it is read directly. If not, NTUSER.DAT
    /// is mounted read-only under a temporary key and unmounted in a finally — this is what
    /// lets a machine back up the redirected folders of users who are not currently logged
    /// on, which on a shared PC is most of them. Requires SeRestorePrivilege, which the
    /// agent has as SYSTEM.
    ///
    /// A folder that cannot be resolved is OMITTED, never defaulted to
    /// &lt;profile&gt;\Desktop: the hub reports a missing folder as a problem an operator can
    /// see, whereas a guessed path silently backs up an empty stub.
    /// </summary>
    private static IReadOnlyDictionary<string, string> ReadKnownFolders(string sid, string profilePath)
    {
        var folders = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);

        using (var loaded = Registry.Users.OpenSubKey($@"{sid}\{ShellFoldersSubKey}"))
        {
            if (loaded is not null)
            {
                ReadFolderValues(loaded, profilePath, folders);
                return folders;
            }
        }

        var hive = Path.Combine(profilePath, "NTUSER.DAT");
        if (!File.Exists(hive)) return folders;

        var mountName = "FleetHubBackup_" + Guid.NewGuid().ToString("N")[..8];
        if (!HiveMount.TryLoad(mountName, hive)) return folders;
        try
        {
            using var mounted = Registry.Users.OpenSubKey($@"{mountName}\{ShellFoldersSubKey}");
            if (mounted is not null) ReadFolderValues(mounted, profilePath, folders);
        }
        finally
        {
            HiveMount.TryUnload(mountName);
        }
        return folders;
    }

    private static void ReadFolderValues(RegistryKey key, string profilePath,
                                         Dictionary<string, string> into)
    {
        foreach (var (token, valueName) in KnownFolders)
        {
            // DoNotExpandEnvironmentNames: User Shell Folders stores REG_EXPAND_SZ like
            // "%USERPROFILE%\Desktop", and %USERPROFILE% here means the PROFILE BEING
            // READ, not the agent's own (which is SYSTEM's). Letting the framework expand
            // it would point every user's Desktop at C:\Windows\system32\config\systemprofile.
            var raw = key.GetValue(valueName, null,
                                   RegistryValueOptions.DoNotExpandEnvironmentNames) as string;
            if (string.IsNullOrWhiteSpace(raw)) continue;
            var resolved = ExpandUserVariables(raw, profilePath);
            if (!string.IsNullOrWhiteSpace(resolved)) into[token] = Normalize(resolved);
        }
    }

    private static string ExpandUserVariables(string raw, string profilePath)
    {
        var text = raw.Replace("%USERPROFILE%", profilePath, StringComparison.OrdinalIgnoreCase);
        // Anything else (%OneDrive%, %SystemDrive%) is machine-level or already absolute.
        return Environment.ExpandEnvironmentVariables(text);
    }

    // ---------------------------------------------------------------- expansion

    /// <summary>Real profiles only — what %Users% fans out to.</summary>
    public static IReadOnlyList<UserProfile> RealUsers(MachineProfiles profiles) =>
        profiles.Users.Where(u => !string.IsNullOrWhiteSpace(u.Name)
                                  && !NonUserProfiles.Contains(u.Name)
                                  && !string.IsNullOrWhiteSpace(u.Path)).ToList();

    private static List<string> TokensIn(string pattern) =>
        TokenRe.Matches(pattern).Select(m => m.Groups[1].Value.Trim().ToLowerInvariant()).ToList();

    /// <summary>
    /// Every concrete path <paramref name="pattern"/> names on this machine.
    ///
    /// Empty when nothing resolves — a machine with no profiles, or a user missing the
    /// folder. <paramref name="problems"/> collects the reasons, which the executor
    /// reports back so an operator sees "carol has no %Documents% folder" rather than a
    /// silently short backup.
    /// </summary>
    public static List<string> Expand(string pattern, MachineProfiles profiles,
                                      List<string>? problems = null)
    {
        var text = Normalize(pattern);
        if (text.Length == 0) return [];

        var tokens = TokensIn(text);
        foreach (var token in tokens)
        {
            if (!KnownFolders.ContainsKey(token) && token != "users"
                && !MachineTokens.Contains(token))
            {
                // Refused rather than treated as a literal: an unknown token would match
                // nothing, forever, with a green run beside it.
                problems?.Add($"{text}: %{token}% is not a known token.");
                return [];
            }
        }

        var perUser = tokens.Where(t => t == "users" || KnownFolders.ContainsKey(t)).ToList();
        if (perUser.Count == 0)
        {
            var resolved = SubstituteMachine(text, profiles, problems);
            return resolved is null ? [] : [resolved];
        }

        var users = RealUsers(profiles);
        if (users.Count == 0)
        {
            problems?.Add($"{text}: this machine has no user profiles to expand %Users% to.");
            return [];
        }

        var output = new List<string>();
        foreach (var user in users)
        {
            var current = text;
            bool failed = false;
            foreach (var token in perUser.Distinct())
            {
                string? value;
                if (token == "users")
                {
                    value = user.Path;
                }
                else if (!user.Folders.TryGetValue(token, out value) || string.IsNullOrWhiteSpace(value))
                {
                    problems?.Add(
                        $"{text}: {user.Name} has no %{token}% folder recorded, so nothing " +
                        "is backed up there for them.");
                    failed = true;
                    break;
                }
                current = ReplaceToken(current, token, value!);
            }
            if (failed) continue;

            var resolved = SubstituteMachine(current, profiles, problems);
            if (resolved is not null) output.Add(resolved);
        }
        return output;
    }

    private static string? SubstituteMachine(string text, MachineProfiles profiles,
                                             List<string>? problems)
    {
        foreach (var token in TokensIn(text).Distinct())
        {
            if (!MachineTokens.Contains(token)) continue;
            if (!profiles.Env.TryGetValue(token, out var value) || string.IsNullOrWhiteSpace(value))
            {
                problems?.Add($"{text}: this machine did not report %{token}%.");
                return null;
            }
            text = ReplaceToken(text, token, value);
        }
        return Normalize(text);
    }

    private static string ReplaceToken(string text, string token, string value) =>
        Regex.Replace(text, "%" + Regex.Escape(token) + "%",
                      value.Replace("$", "$$"), RegexOptions.IgnoreCase);

    // ---------------------------------------------------------------- excludes

    /// <summary>Compiled exclude patterns. Mirrors backup_paths.ExcludeMatcher, including
    /// the basename rule and the ancestor walk.</summary>
    public sealed class ExcludeMatcher
    {
        private readonly List<Regex> _full = [];
        private readonly List<Regex> _names = [];

        public ExcludeMatcher(IEnumerable<string> patterns, MachineProfiles profiles)
        {
            foreach (var raw in patterns ?? [])
            {
                var text = Normalize(raw);
                if (text.Length == 0) continue;

                if (!text.Contains('\\'))
                {
                    // No separator: match on FILENAME anywhere, which is what someone
                    // typing an extension means.
                    _names.Add(Compile(text));
                    continue;
                }
                var expansions = TokensIn(text).Count > 0
                    ? Expand(text, profiles)
                    : [text];
                foreach (var expanded in expansions) _full.Add(Compile(expanded));
            }
        }

        public bool IsEmpty => _full.Count == 0 && _names.Count == 0;

        /// <summary>Excluded directly, by filename, or by an excluded ancestor. The
        /// ancestor walk is what makes excluding a FOLDER exclude its contents without the
        /// operator also writing a trailing \**.</summary>
        public bool Matches(string path)
        {
            var text = Normalize(path);
            if (text.Length == 0) return false;
            var lowered = text.ToLowerInvariant();

            var name = lowered[(lowered.LastIndexOf('\\') + 1)..];
            foreach (var rx in _names) if (rx.IsMatch(name)) return true;

            foreach (var rx in _full)
            {
                if (rx.IsMatch(lowered)) return true;
                var head = lowered;
                int cut;
                while ((cut = head.LastIndexOf('\\')) > 0)
                {
                    head = head[..cut];
                    if (rx.IsMatch(head)) return true;
                }
            }
            return false;
        }

        private static Regex Compile(string glob) =>
            new("^" + GlobToRegex(glob.ToLowerInvariant()) + "$",
                RegexOptions.Compiled | RegexOptions.IgnoreCase);
    }

    /// <summary>`**` crosses separators, `*` and `?` do not. Mirrors
    /// backup_paths._glob_to_regex, including `a\**\b` matching `a\b`.</summary>
    internal static string GlobToRegex(string pattern)
    {
        var sb = new StringBuilder();
        int i = 0;
        while (i < pattern.Length)
        {
            char c = pattern[i];
            if (c == '*')
            {
                if (i + 1 < pattern.Length && pattern[i + 1] == '*')
                {
                    sb.Append(".*");
                    i += 2;
                    if (i < pattern.Length && pattern[i] == '\\')
                    {
                        sb.Append("(?:\\\\)?");
                        i++;
                    }
                    continue;
                }
                sb.Append("[^\\\\]*");
            }
            else if (c == '?')
            {
                sb.Append("[^\\\\]");
            }
            else
            {
                sb.Append(Regex.Escape(c.ToString()));
            }
            i++;
        }
        return sb.ToString();
    }
}
