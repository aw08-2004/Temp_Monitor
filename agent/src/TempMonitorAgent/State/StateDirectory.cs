using System.Runtime.Versioning;
using System.Security.AccessControl;
using System.Security.Principal;

namespace TempMonitorAgent.State;

/// <summary>
/// Locks down the agent's %ProgramData% state directory so that only SYSTEM and
/// Administrators may write into it.
///
/// WHY THIS EXISTS. Nothing ever set an ACL here, so the directory inherited the default
/// one from C:\ProgramData -- and that default is not what it looks like:
///
///     BUILTIN\Users:(OI)(CI)(RX)          read+execute on files and folders, as expected
///     BUILTIN\Users:(CI)(WD,AD,WEA,WA)    ...and CREATE FILES / CREATE FOLDERS in every
///                                            subdirectory, which is not
///
/// The second ACE is container-inherit, so it lands on the state dir and on every
/// subdirectory the agent makes under it -- `update`, `packages`, `firmware`, `drivers`,
/// `backup\staging`, `backup\restore`. Existing files written by the service stay safe
/// (Users hold only RX on those), but any authenticated local user could DROP A NEW FILE
/// into any of those directories, and would own what they dropped -- CREATOR OWNER carries
/// full control, so they keep write access to it afterwards.
///
/// That is a local privilege escalation, and the self-updater is the path: it stages the
/// next agent binary at update\TempMonitorAgent-&lt;version&gt;.exe, where the version comes
/// from a manifest published on GitHub. A standard user could pre-create that exact path,
/// retain ownership through the service's write, and overwrite the verified bytes in the
/// window before the swap -- landing arbitrary code at the service's own exe path, which
/// the SCM then runs as SYSTEM. (SelfUpdater re-checks the hash on disk immediately before
/// the swap, which closes that window; this removes the write primitive that opens it.)
///
/// Applied at every service boot rather than only at install time: the agent self-updates
/// itself across the fleet in minutes, while the installer runs once and may never run
/// again on a machine that is already deployed.
///
/// SAFE TO TIGHTEN. Every process that writes here runs as SYSTEM -- the service itself,
/// and the remote helper, which SessionInjector launches as SYSTEM-in-session (a duplicated
/// SYSTEM token retargeted at the console session) rather than with the logged-in user's
/// token. Users keep read+execute so companion.log stays readable for support.
/// </summary>
[SupportedOSPlatform("windows")]
public static class StateDirectory
{
    /// <summary>
    /// Ensure <paramref name="path"/> exists and that only SYSTEM/Administrators may write
    /// to it. Returns a note worth logging, or null when the ACL was already correct.
    ///
    /// Never throws: a hub that cannot re-ACL its state directory must still start and
    /// report telemetry. The caller logs what came back.
    /// </summary>
    public static string? Harden(string path)
    {
        try
        {
            var info = Directory.CreateDirectory(path);
            var security = info.GetAccessControl(AccessControlSections.Access);

            // Already protected AND carrying no write for non-admins: nothing to do. This is
            // the steady state on every boot after the first, and it is what keeps this from
            // rewriting the ACL (and logging about it) on every service start.
            if (security.AreAccessRulesProtected && !GrantsNonAdminWrite(security))
                return null;

            // Drop inheritance rather than filter it: the offending ACE is inherited from
            // C:\ProgramData, and an inherited ACE cannot be removed in place -- it would
            // come straight back the next time Windows recomputed it.
            security.SetAccessRuleProtection(isProtected: true, preserveInheritance: false);

            // ...and then clear what remains, because protection strips only the INHERITED
            // ACEs. An explicit grant survives it, and AddAccessRule below would merge into
            // such an ACE rather than replace it -- leaving exactly the write bit this
            // exists to remove. The ACL is rebuilt from nothing so the result depends on
            // this code alone and not on what the directory happened to carry before. That
            // does mean a deliberate widening by an admin is reverted at the next boot,
            // which is the intended trade for a privilege boundary.
            foreach (FileSystemAccessRule existing in security.GetAccessRules(
                         includeExplicit: true, includeInherited: false, typeof(SecurityIdentifier)))
                security.PurgeAccessRules(existing.IdentityReference);

            foreach (var (sid, rights) in new[]
            {
                (WellKnownSidType.LocalSystemSid, FileSystemRights.FullControl),
                (WellKnownSidType.BuiltinAdministratorsSid, FileSystemRights.FullControl),
                (WellKnownSidType.BuiltinUsersSid, FileSystemRights.ReadAndExecute),
            })
            {
                security.AddAccessRule(new FileSystemAccessRule(
                    new SecurityIdentifier(sid, null),
                    rights,
                    // Inheritable, so the subdirectories the executors create (packages,
                    // firmware, update, backup\staging, ...) get the same ACL without each
                    // of them having to remember to ask for it.
                    InheritanceFlags.ObjectInherit | InheritanceFlags.ContainerInherit,
                    PropagationFlags.None,
                    AccessControlType.Allow));
            }

            info.SetAccessControl(security);
            return $"Hardened agent state directory ACL ({path}): removed inherited write for non-administrators";
        }
        catch (Exception e)
        {
            return $"Could not harden the agent state directory ACL ({path}): {e.Message}";
        }
    }

    /// <summary>Does this ACL let anyone outside SYSTEM/Administrators/CREATOR OWNER write?</summary>
    private static bool GrantsNonAdminWrite(FileSystemSecurity security)
    {
        // The rights that matter are the ones that let a caller introduce or alter content.
        // Delete/DeleteSubdirectoriesAndFiles are in here too: being able to unlink the
        // staged binary is enough to win the same race by substitution.
        const FileSystemRights Writey =
            FileSystemRights.WriteData |          // == CreateFiles
            FileSystemRights.AppendData |         // == CreateDirectories
            FileSystemRights.Delete |
            FileSystemRights.DeleteSubdirectoriesAndFiles |
            FileSystemRights.ChangePermissions |
            FileSystemRights.TakeOwnership;

        var privileged = new[]
        {
            new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null),
            new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null),
            new SecurityIdentifier(WellKnownSidType.CreatorOwnerSid, null),
        };

        foreach (FileSystemAccessRule rule in security.GetAccessRules(
                     includeExplicit: true, includeInherited: true, typeof(SecurityIdentifier)))
        {
            if (rule.AccessControlType != AccessControlType.Allow) continue;
            if ((rule.FileSystemRights & Writey) == 0) continue;
            if (privileged.Any(p => p.Equals(rule.IdentityReference))) continue;
            return true;
        }
        return false;
    }
}
