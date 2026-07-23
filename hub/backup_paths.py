"""Which files a per-PC backup covers -- the include/exclude path grammar (roadmap #1b).

An operator should never have to enumerate user profiles. "Back up everyone's Desktop and
Documents" is one line, not one line per person, and it has to keep being one line when
somebody new logs into the machine next week. So a pattern is written with tokens and
expanded against the machine's ACTUAL profiles at backup time.

Four ideas carry the design:

  * **Tokens fan out.** `%Users%` stands for every real profile on the box, so
    `%Users%\\Desktop` is one pattern that becomes four paths on a four-user machine, and
    five the day a fifth person signs in. Nothing has to be revisited.

  * **Known-folder tokens follow redirection, literal paths do not.** `%Desktop%` resolves
    through each user's *User Shell Folders* registry, which is where OneDrive Known Folder
    Move points a redirected desktop. `C:\\Users\\bob\\Desktop` on that same machine is an
    empty stub. Both spellings are supported because both are sometimes what you mean, but
    the token is the one that keeps working after somebody enables OneDrive -- and getting
    this wrong produces a backup that runs green every night and contains nothing.

  * **An unknown token is an error, never a literal.** `%Userss%` or `%Desktops%` would
    otherwise expand to nothing, match nothing, and back up nothing -- silently, forever,
    with a green run next to it. Validation refuses the pattern instead. This is the single
    most valuable rule in the file.

  * **Expansion is pure.** Nothing here touches the filesystem or the registry. The caller
    supplies a `profiles` dict describing the machine (the AGENT gathers it for real; the
    HUB uses the copy the agent last reported, to preview what a pattern would match). That
    is what lets the console answer "what would this actually back up on PC-1?" honestly,
    and what makes the whole grammar unit-testable without a Windows box.

THIS GRAMMAR IS IMPLEMENTED TWICE -- here, and in the agent's PathExpander.cs. Two
implementations of one grammar drift apart silently, and the symptom is a folder that
quietly stops being backed up. `tests/backup_path_vectors.json` is the shared fixture both
sides are tested against; add a case there before changing behaviour in either.

Flask-free and settings-free, like permissions.py and backups.py.
"""
import json
import re

# ================================
# VOCABULARY
# ================================
# The fan-out token: one pattern, one path per real profile on the machine.
#
# %User% and %Users% are the SAME token, deliberately. Both expand to one path per real
# profile -- `%Users%\Scripts` and `%User%\Scripts` each produce C:\Users\bob\Scripts and
# C:\Users\carol\Scripts. The alias exists because the two spellings read differently to
# an operator: `%Users%` reads right on its own ("back up the profiles"), while
# `%User%\Scripts` reads right for a subfolder ("each user's Scripts folder"), and someone
# who reaches for the singular should not get a "not a known token" error for guessing the
# more natural phrasing.
#
# Note what %User% is NOT: it is not "the currently logged-on user". A backup runs as
# SYSTEM on a schedule, frequently with nobody signed in at all, so a token meaning "the
# current user" would resolve to the service profile or to nothing -- and would silently
# back up one person's folder on a shared PC. Every per-user token here fans out.
USERS_TOKEN = "users"
USER_TOKEN = "user"

# Both spellings of the profile-directory token. Anything in here resolves to the user's
# profile path during fan-out.
PROFILE_TOKENS = frozenset({USERS_TOKEN, USER_TOKEN})

# Per-user known folders, resolved from that user's shell-folder registry rather than
# assumed to sit under the profile. These fan out per user exactly like %Users%.
#
# The mapping value is the registry value name under
# HKU\<sid>\Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders,
# which is what the agent reads. Kept here so the two implementations agree on the list
# and on the spelling.
KNOWN_FOLDERS = {
    "desktop": "Desktop",
    "documents": "Personal",          # yes, really -- the registry name is "Personal"
    "downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "pictures": "My Pictures",
    "music": "My Music",
    "videos": "My Video",
    "favorites": "Favorites",
    "appdata": "AppData",             # roaming
    "localappdata": "Local AppData",
}

# Machine-wide environment tokens. No fan-out -- one value per machine.
MACHINE_TOKENS = (
    "programdata",
    "systemdrive",
    "windir",
    "programfiles",
    "programfiles(x86)",
    "public",
    "systemroot",
)

ALL_TOKENS = frozenset(PROFILE_TOKENS | set(KNOWN_FOLDERS) | set(MACHINE_TOKENS))

# Profile directories that are never a person. Windows keeps several profile-shaped
# folders under C:\Users that would otherwise each get "their" Desktop backed up --
# Default is a template, Public is shared, defaultuser0 is an OOBE leftover.
NON_USER_PROFILES = frozenset({
    "public", "default", "default user", "all users", "defaultuser0",
    "systemprofile", "localservice", "networkservice",
})

MAX_PATTERN_CHARS = 260

_TOKEN_RE = re.compile(r"%([^%]*)%")
# A drive-letter path (C:\...) or a UNC share (\\server\share\...). Checked after
# expansion, so a pattern may legitimately START with a token.
_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+)")


class PatternError(ValueError):
    """A pattern an operator typed that cannot work. The message is shown verbatim next
    to the field, so it names the pattern and says what to do about it."""


# ================================
# NORMALISATION
# ================================
def normalize(path):
    """Windows-style separators, no trailing slash, no doubled separators.

    Comparison and matching happen on this form throughout, so `C:/Users/bob/` and
    `C:\\Users\\bob` are the same path rather than two.
    """
    text = str(path or "").strip().replace("/", "\\")
    # Collapse runs of separators, but keep a leading \\ so UNC paths survive.
    leading_unc = text.startswith("\\\\")
    text = re.sub(r"\\{2,}", "\\\\", text)
    if leading_unc:
        text = "\\" + text
    if len(text) > 3 and text.endswith("\\"):
        text = text.rstrip("\\")
    return text


def archive_member(path):
    """The name a file is stored under INSIDE a backup archive.

    `C:\\Users\\bob\\Desktop\\notes.txt` -> `C/Users/bob/Desktop/notes.txt`. The drive
    colon is dropped and separators become forward slashes, because tar member names are
    POSIX-shaped and a literal `C:\\...` unpacks to a mangled name (or is rejected outright)
    on anything but Windows -- and the whole point of tar here is that stdlib `tarfile`
    can open the archive anywhere.

    THIS IS A SHARED CONTRACT, implemented twice: here, and in the agent's
    BackupManifest.ArchiveMember (which is what TarGzipPipe names entries with). The hub
    builds a restore plan naming members it never wrote, the agent extracts them, and
    restore_backup.py maps them back to real paths -- so a drift between the two
    implementations is a restore that silently finds nothing. tests/test_backup_paths.py
    and the agent's BackupManifestTests both check the vectors in
    tests/backup_path_vectors.json ("members") for exactly that reason.

    A UNC path loses its leading slashes (`\\\\srv\\share\\f` -> `srv/share/f`), which is
    the same shape tar gives any absolute path. Two sources could in principle collide
    (`C:\\Users` and `\\\\C\\Users`), and that is accepted: a restore selects members from
    ONE machine's manifest, where a path is unique.
    """
    return normalize(path).replace(":", "").replace("\\", "/").lstrip("/")


def member_to_path(member):
    """The inverse of archive_member, for a restore that is putting files back.

    `C/Users/bob/notes.txt` -> `C:\\Users\\bob\\notes.txt`. Only the FIRST segment can have
    been a drive, so only it gets its colon back, and only when it is a single letter --
    a member from a UNC source (`srv/share/f`) is left as a relative path, which is what a
    restore-to-a-folder wants and what a restore-to-original-location refuses.
    """
    text = str(member or "").strip().replace("\\", "/").lstrip("/")
    if not text:
        return ""
    head, _, tail = text.partition("/")
    if len(head) == 1 and head.isalpha():
        head += ":"
    return normalize(head + ("\\" + tail if tail else "\\"))


def _tokens_in(pattern):
    return [m.group(1).strip().lower() for m in _TOKEN_RE.finditer(pattern)]


# ================================
# VALIDATION
# ================================
def validate_pattern(pattern, kind="include"):
    """Normalize one pattern, or raise PatternError explaining why it cannot work.

    `kind` is "include" or "exclude". The difference is anchoring: an include has to name
    a real place on disk (it is a starting point for a walk), while an exclude is a
    filter and may be a bare glob like `*.tmp` that matches anywhere.
    """
    text = normalize(pattern)
    if not text:
        raise PatternError("A path pattern cannot be empty.")
    if len(text) > MAX_PATTERN_CHARS:
        raise PatternError(f"{text[:40]}...: longer than {MAX_PATTERN_CHARS} characters.")

    # An unbalanced % is nearly always a typo for a token, and left alone it would be
    # treated as a literal path component that matches nothing.
    if text.count("%") % 2:
        raise PatternError(f"{text!r}: unmatched '%'. Tokens look like %Users%.")

    for token in _tokens_in(text):
        if token not in ALL_TOKENS:
            raise PatternError(
                f"{text!r}: %{token}% is not a known token. Use one of: "
                + ", ".join("%" + t + "%" for t in sorted(ALL_TOKENS)) + ".")

    if any(part == ".." for part in text.split("\\")):
        raise PatternError(f"{text!r}: '..' is not allowed in a backup path.")

    if kind == "include":
        # Must land somewhere absolute once expanded. A token-led pattern is fine (every
        # token expands to an absolute path); a bare `Desktop` or `*.docx` is not, because
        # there is no directory to start walking from.
        if not _tokens_in(text) and not _ABSOLUTE_RE.match(text):
            raise PatternError(
                f"{text!r}: an included path must be absolute (C:\\...) or start with a "
                f"token such as %Users% or %Desktop%.")
        if "*" in text or "?" in text:
            # Globs are a filter, not a starting point. Silently walking every match of
            # `C:\*\data` is a very different (and much more expensive) operation than the
            # operator thinks they asked for.
            raise PatternError(
                f"{text!r}: wildcards belong in the exclude list. An included path names "
                f"a folder to back up.")
    return text


def validate_patterns(patterns, kind="include"):
    """Normalize a whole list, dropping blanks and duplicates but keeping order."""
    seen = set()
    out = []
    for raw in patterns or []:
        if not str(raw or "").strip():
            continue
        clean = validate_pattern(raw, kind=kind)
        if clean.lower() in seen:
            continue
        seen.add(clean.lower())
        out.append(clean)
    return out


# ================================
# THE MACHINE DESCRIPTION
# ================================
# What `profiles` looks like -- gathered by the agent, cached by the hub:
#
#   {
#     "profile_root": "C:\\Users",
#     "env": {"ProgramData": "C:\\ProgramData", "SystemDrive": "C:", ...},
#     "users": [
#        {"name": "bob", "sid": "S-1-5-21-...", "path": "C:\\Users\\bob",
#         "folders": {"Desktop": "C:\\Users\\bob\\OneDrive\\Desktop", ...}},
#     ]
#   }
#
# `folders` keys are the lowercase token names (desktop, documents, ...), already
# resolved by whoever gathered them. A user missing a folder simply contributes no path
# for that token rather than a guessed one -- see _known_folder_for.
def real_users(profiles):
    """The profiles %Users% fans out to: everything that is actually a person."""
    users = []
    for user in (profiles or {}).get("users") or []:
        name = str(user.get("name") or "").strip()
        if not name or name.lower() in NON_USER_PROFILES:
            continue
        if not str(user.get("path") or "").strip():
            continue
        users.append(user)
    return users


def _known_folder_for(user, token):
    """One user's resolved known folder, or None.

    None rather than a guess at `<profile>\\Desktop`: if the shell-folder registry did not
    say, inventing the default is how a redirected folder gets "backed up" as an empty
    stub while the real data is missed. A missing folder is reported by preview(), which
    is a fixable state an operator can see.
    """
    folders = {str(k).lower(): v for k, v in (user.get("folders") or {}).items()}
    value = folders.get(token)
    return normalize(value) if value else None


def _machine_value(profiles, token):
    env = {str(k).lower(): v for k, v in ((profiles or {}).get("env") or {}).items()}
    value = env.get(token)
    return normalize(value) if value else None


# ================================
# EXPANSION
# ================================
def expand(pattern, profiles):
    """Every concrete path `pattern` names on this machine.

    Returns [] when nothing matches -- a machine with no interactive profiles, or a token
    the machine could not resolve. Callers that need to explain the emptiness to a human
    should use preview(), which keeps the reasons.
    """
    return [path for path, _ in _expand_with_reasons(pattern, profiles)[0]]


def _expand_with_reasons(pattern, profiles):
    """(resolved, problems) -- the paths, plus why any expansion produced nothing.

    Split out from expand() so preview() can tell "no users on this machine" apart from
    "Bob has no Documents folder", which are different problems with different fixes.
    """
    text = normalize(pattern)
    tokens = _tokens_in(text)
    problems = []

    per_user_tokens = [t for t in tokens if t in PROFILE_TOKENS or t in KNOWN_FOLDERS]
    if not per_user_tokens:
        resolved = _substitute_machine(text, profiles, problems)
        return ([(resolved, None)] if resolved else []), problems

    users = real_users(profiles)
    if not users:
        problems.append(f"{text}: this machine has no user profiles to expand "
                        f"%{per_user_tokens[0]}% to.")
        return [], problems

    out = []
    for user in users:
        current = text
        failed = False
        for token in set(per_user_tokens):
            if token in PROFILE_TOKENS:
                value = normalize(user.get("path"))
            else:
                value = _known_folder_for(user, token)
                if not value:
                    problems.append(
                        f"{text}: {user.get('name')} has no %{token}% folder recorded, "
                        f"so nothing is backed up there for them.")
                    failed = True
                    break
            current = _replace_token(current, token, value)
        if failed:
            continue
        current = _substitute_machine(current, profiles, problems)
        if current:
            out.append((current, user.get("name")))
    return out, problems


def _substitute_machine(text, profiles, problems):
    """Replace machine-wide tokens. Returns None if one could not be resolved."""
    for token in set(_tokens_in(text)):
        if token not in MACHINE_TOKENS:
            continue
        value = _machine_value(profiles, token)
        if not value:
            problems.append(f"{text}: this machine did not report %{token}%.")
            return None
        text = _replace_token(text, token, value)
    return normalize(text)


def _replace_token(text, token, value):
    """Case-insensitive replacement of %token% with value."""
    return re.sub(r"%" + re.escape(token) + r"%", lambda _: value, text,
                  flags=re.IGNORECASE)


# ================================
# EXCLUDES
# ================================
def _glob_to_regex(pattern):
    """Translate one glob to a regex over a normalized path.

    `**` crosses separators, `*` and `?` do not -- the same convention .gitignore and
    every developer already has in their head, so `**\\node_modules\\**` means what it
    looks like and `C:\\a\\*\\b` does not silently walk three levels deep.
    """
    out = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                # `**\` should also match zero directories, so `a\**\b` matches `a\b`.
                if pattern[i:i + 1] == "\\":
                    out.append("(?:\\\\)?")
                    i += 1
                continue
            out.append("[^\\\\]*")
        elif char == "?":
            out.append("[^\\\\]")
        else:
            out.append(re.escape(char))
        i += 1
    return "".join(out)


class ExcludeMatcher:
    """Compiled excludes. Built once per run, then asked about every file walked, so the
    per-path cost is a handful of anchored regex matches and nothing else."""

    def __init__(self, full_patterns, name_patterns, sources):
        self._full = full_patterns
        self._names = name_patterns
        self.sources = sources

    def __bool__(self):
        return bool(self._full or self._names)

    def matches(self, path):
        """Is `path` excluded -- directly, by basename, or by an excluded ancestor?

        The ancestor check is what makes excluding a FOLDER exclude its contents, so an
        operator writing `%LocalAppData%\\Temp` does not also have to write
        `%LocalAppData%\\Temp\\**`.
        """
        text = normalize(path)
        if not text:
            return False
        lowered = text.lower()

        name = lowered.rsplit("\\", 1)[-1]
        for rx in self._names:
            if rx.match(name):
                return True
        for rx in self._full:
            if rx.match(lowered):
                return True
            # Walk the ancestors: C:\a\b\c -> C:\a\b -> C:\a
            head = lowered
            while "\\" in head:
                head = head.rsplit("\\", 1)[0]
                if rx.match(head):
                    return True
        return False


def compile_excludes(patterns, profiles):
    """Build an ExcludeMatcher from operator patterns.

    A pattern with no separator (`*.tmp`, `thumbs.db`) matches on BASENAME anywhere --
    which is what someone typing an extension means. Anything else is matched against the
    whole path, after token expansion.
    """
    full, names, sources = [], [], []
    for raw in patterns or []:
        text = normalize(raw)
        if not text:
            continue
        sources.append(text)
        if "\\" not in text:
            names.append(re.compile(_glob_to_regex(text.lower()) + r"\Z"))
            continue
        # Token-bearing excludes expand exactly like includes -- one exclude per user for
        # a per-user token -- so `%LocalAppData%\Temp` covers everybody's temp folder.
        expansions = expand(text, profiles) if _tokens_in(text) else [text]
        if not expansions and _tokens_in(text):
            # Nothing to expand to on this machine (no profiles yet). Dropping it is
            # right: there is no path it could match either.
            continue
        for expanded in expansions:
            full.append(re.compile(_glob_to_regex(expanded.lower()) + r"\Z"))
    return ExcludeMatcher(full, names, sources)


# ================================
# PREVIEW
# ================================
def preview(includes, excludes, profiles):
    """What these patterns resolve to on one machine -- the console's honest answer.

    Returns {"roots": [{pattern, path, user}], "problems": [...], "excludes": [...],
    "users": [...]}. `problems` is the load-bearing half: a pattern that expands to
    nothing is the failure mode this whole module exists to make visible, so it comes back
    as a sentence an operator can act on rather than an empty list.
    """
    roots = []
    problems = []
    for pattern in includes or []:
        try:
            clean = validate_pattern(pattern, kind="include")
        except PatternError as e:
            problems.append(str(e))
            continue
        resolved, reasons = _expand_with_reasons(clean, profiles)
        problems.extend(reasons)
        if not resolved and not reasons:
            problems.append(f"{clean}: resolves to nothing on this machine.")
        for path, user in resolved:
            roots.append({"pattern": clean, "path": path, "user": user})

    matcher = compile_excludes(excludes, profiles)
    # An exclude that swallows an entire included root is legal but almost never intended,
    # and it is invisible until a restore comes up empty.
    for root in roots:
        if matcher.matches(root["path"]):
            problems.append(
                f"{root['path']} is included but also excluded, so nothing under it "
                f"will be backed up.")

    return {
        "roots": roots,
        "problems": problems,
        "excludes": matcher.sources,
        "users": [u.get("name") for u in real_users(profiles)],
    }


# ================================
# TOKEN REFERENCE (for the UI)
# ================================
# Rendered in the Backup Settings tab. Here rather than in the template for the same
# reason permissions.CAPABILITY_LABELS is: the API describes itself, so adding a token is
# one edit.
TOKEN_HELP = [
    ("%Users%", "Every real user profile on the machine (skips Public, Default and "
                "service accounts). One pattern covers everyone, including people who "
                "sign in for the first time next week."),
    ("%User%", "The same as %Users% -- one path per real profile. Reads better for a "
               "custom subfolder: %User%\\Scripts backs up every user's Scripts folder. "
               "It does NOT mean 'whoever is logged in now'; backups run with nobody "
               "signed in, so every per-user token covers all of them."),
    ("%Desktop%", "Each user's actual Desktop -- follows OneDrive folder redirection, "
                  "unlike a literal C:\\Users\\name\\Desktop."),
    ("%Documents%", "Each user's actual Documents folder."),
    ("%Downloads%", "Each user's actual Downloads folder."),
    ("%Pictures%", "Each user's actual Pictures folder."),
    ("%Favorites%", "Each user's browser favourites folder."),
    ("%AppData%", "Each user's roaming AppData."),
    ("%LocalAppData%", "Each user's local AppData."),
    ("%ProgramData%", "C:\\ProgramData -- machine-wide, no per-user expansion."),
    ("%SystemDrive%", "Usually C:."),
]


def load_vectors(path):
    """Read the shared expansion fixture. Used by the test suite here and mirrored by the
    agent's own tests, so both implementations answer to the same cases."""
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)
