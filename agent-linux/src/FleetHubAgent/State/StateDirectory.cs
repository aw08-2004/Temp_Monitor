namespace FleetHubAgent.State;

/// <summary>
/// Creates the agent's state directory and takes its permissions down to root-only.
///
/// **This is the Linux half of the Windows agent's StateDirectory.Harden, and it exists for
/// the same reason.** There, the inherited ACL on C:\ProgramData let any local user create
/// files under the agent's tree, which is a privilege-escalation primitive against anything
/// the service later reads back. Here the equivalent hole is the process umask: /var/lib is
/// 0755, a directory created with the default 0022 umask comes out 0755 too, and the file
/// inside it 0644. That file is the agent's bearer token for the hub -- world-readable, it
/// lets any local account impersonate this machine to the fleet, read its queued commands and
/// answer them.
///
/// So the mode is set EXPLICITLY rather than left to the umask, on the directory and on the
/// file, every time. Setting it on creation only would be a subtle bug: the installer, an
/// operator, or a restore from backup can each leave the tree with different modes, and the
/// service is the thing that knows what they should be.
///
/// Fails soft, deliberately. A chmod that fails (an exotic filesystem, a container with the
/// tree bind-mounted read-only) is logged by the caller and does not stop the agent -- an
/// agent that will not run reports nothing at all, which is worse than one running with
/// permissions someone has to be told about.
/// </summary>
internal static class StateDirectory
{
    // rwx------ : only the user the service runs as (root) may even list the directory.
    private const UnixFileMode DirMode =
        UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute;

    // rw------- : same reasoning, minus the traverse bit that means nothing on a file.
    private const UnixFileMode FileMode =
        UnixFileMode.UserRead | UnixFileMode.UserWrite;

    /// <summary>Create the directory if absent and force its mode either way. Returns a note
    /// for the log when something could not be applied, or null when all was well.</summary>
    internal static string? Ensure(string path)
    {
        try
        {
            if (!Directory.Exists(path))
                Directory.CreateDirectory(path, DirMode);
            // ...and again on an existing directory, which CreateDirectory above would not
            // have touched. This is the case that actually bites: a tree created by hand or
            // by an older installer keeps whatever mode it was given.
            File.SetUnixFileMode(path, DirMode);
            return null;
        }
        catch (Exception e)
        {
            return $"Could not secure state directory {path}: {e.Message}. " +
                   "The enrollment token in it may be readable by other local users.";
        }
    }

    /// <summary>Force 0600 on a state file. Called after every write, not just the first --
    /// see the class note.</summary>
    internal static void RestrictFile(string path)
    {
        try { File.SetUnixFileMode(path, FileMode); }
        catch { /* see the class note: never fatal */ }
    }
}
