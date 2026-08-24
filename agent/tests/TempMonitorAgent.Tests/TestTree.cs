using System.Security.AccessControl;
using System.Security.Principal;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Deletes a scratch directory tree that may have been through StateDirectory.Harden.
///
/// Harden leaves a tree writable only by SYSTEM and Administrators, and the test run is
/// neither -- so a plain Directory.Delete on a hardened tree fails, and a best-effort catch
/// turns that into a silent leak of one temp tree per run. The test user created these
/// directories and an owner always keeps WRITE_DAC, so access can be handed back first.
///
/// Shared by AssemblySetup (the redirected state root) and StateDirectoryTests (its own
/// fixture dir) so the two cannot drift: Program.cs hardens AgentConfig.ProgramDataDir,
/// which the state-root redirect now points at the scratch tree, so the day a test exercises
/// the startup path both callers need this and neither should have to remember why.
/// </summary>
internal static class TestTree
{
    internal static void Remove(string root)
    {
        if (!Directory.Exists(root)) return;
        ReclaimAccess(root);
        try { Directory.Delete(root, recursive: true); }
        catch { /* leave it for %TEMP% cleanup rather than failing a finished run */ }
    }

    private static void ReclaimAccess(string root)
    {
        try
        {
            // Harden keeps Users at read+execute, so enumerating a hardened tree still works;
            // it is only the write/delete that was taken away.
            foreach (var path in Directory
                         .EnumerateDirectories(root, "*", SearchOption.AllDirectories)
                         .Append(root))
            {
                var info = new DirectoryInfo(path);
                var security = info.GetAccessControl(AccessControlSections.Access);
                security.SetAccessRuleProtection(isProtected: false, preserveInheritance: true);
                security.AddAccessRule(new FileSystemAccessRule(
                    WindowsIdentity.GetCurrent().User!, FileSystemRights.FullControl,
                    InheritanceFlags.ObjectInherit | InheritanceFlags.ContainerInherit,
                    PropagationFlags.None, AccessControlType.Allow));
                info.SetAccessControl(security);
            }
        }
        catch { /* best effort: the delete below reports the real outcome */ }
    }
}
