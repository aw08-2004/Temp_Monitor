using System.Security.AccessControl;
using System.Security.Principal;
using TempMonitorAgent.State;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Covers the agent state directory's ACL, which is a privilege boundary: %ProgramData%
/// inherits a container-inherit ACE granting BUILTIN\Users create-files/create-folders into
/// every subdirectory, and the self-updater stages the next SYSTEM-service binary in one of
/// them. StateDirectory.Harden is what removes that.
///
/// The ACE under test is reproduced explicitly here rather than by creating a directory
/// under the real C:\ProgramData: the assertion is about what Harden removes, and a test
/// that depended on the host's ProgramData ACL would pass or fail for reasons unrelated to
/// this code (and would need to write outside the temp tree to run at all).
/// </summary>
public class StateDirectoryTests : IDisposable
{
    private readonly string _dir = Path.Combine(
        Path.GetTempPath(), "fleethub-acl-test-" + Guid.NewGuid().ToString("n"));

    private static readonly SecurityIdentifier Users =
        new(WellKnownSidType.BuiltinUsersSid, null);

    public StateDirectoryTests() => Directory.CreateDirectory(_dir);

    public void Dispose()
    {
        // Harden leaves the tree writable only by SYSTEM/Administrators, and the test run is
        // neither -- so hand access back before deleting, or every run leaks a temp tree.
        // The test user created these directories, and an owner always keeps WRITE_DAC.
        try
        {
            foreach (var path in Directory.EnumerateDirectories(_dir, "*", SearchOption.AllDirectories)
                         .Append(_dir))
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
        catch { /* best effort */ }
        try { Directory.Delete(_dir, recursive: true); } catch { /* best effort */ }
        GC.SuppressFinalize(this);
    }

    /// <summary>Add the ACE that C:\ProgramData actually hands down: Users may create files
    /// and folders (WD|AD), container-inherit, with no matching read/modify grant.</summary>
    private void GrantUsersCreate()
    {
        var info = new DirectoryInfo(_dir);
        var security = info.GetAccessControl(AccessControlSections.Access);
        security.AddAccessRule(new FileSystemAccessRule(
            Users,
            FileSystemRights.WriteData | FileSystemRights.AppendData,
            InheritanceFlags.ContainerInherit,
            PropagationFlags.None,
            AccessControlType.Allow));
        info.SetAccessControl(security);
    }

    private FileSystemRights RightsFor(SecurityIdentifier sid, string? path = null)
    {
        var security = new DirectoryInfo(path ?? _dir).GetAccessControl(AccessControlSections.Access);
        var rights = default(FileSystemRights);
        foreach (FileSystemAccessRule rule in security.GetAccessRules(true, true, typeof(SecurityIdentifier)))
        {
            if (rule.AccessControlType == AccessControlType.Allow && sid.Equals(rule.IdentityReference))
                rights |= rule.FileSystemRights;
        }
        return rights;
    }

    [Fact]
    public void Harden_removes_the_create_files_grant_users_inherit_from_programdata()
    {
        GrantUsersCreate();
        Assert.NotEqual(default, RightsFor(Users) & FileSystemRights.WriteData);

        var note = StateDirectory.Harden(_dir);

        Assert.NotNull(note);
        var users = RightsFor(Users);
        Assert.Equal(default, users & FileSystemRights.WriteData);
        Assert.Equal(default, users & FileSystemRights.AppendData);
        Assert.Equal(default, users & FileSystemRights.Delete);
    }

    [Fact]
    public void Harden_leaves_users_able_to_read_so_support_can_still_open_the_log()
    {
        GrantUsersCreate();
        StateDirectory.Harden(_dir);

        Assert.NotEqual(default, RightsFor(Users) & FileSystemRights.ReadData);
    }

    [Fact]
    public void Harden_keeps_system_and_administrators_in_full_control()
    {
        GrantUsersCreate();
        StateDirectory.Harden(_dir);

        foreach (var sid in new[] { WellKnownSidType.LocalSystemSid, WellKnownSidType.BuiltinAdministratorsSid })
        {
            var rights = RightsFor(new SecurityIdentifier(sid, null));
            Assert.Equal(FileSystemRights.FullControl, rights & FileSystemRights.FullControl);
        }
    }

    /// <summary>The subdirectories the executors create (update, packages, firmware,
    /// backup\staging, ...) must come out locked down without each of them asking, because
    /// the staging dir the self-updater writes into is one of them.
    ///
    /// Asserted on the parent's inheritance FLAGS rather than by creating a child and
    /// reading its ACL: after Harden only SYSTEM and Administrators may create anything
    /// here, and the test run is neither, so making the child is precisely what this ACL is
    /// supposed to refuse. The flags are what Windows propagates, so they are the property
    /// worth pinning anyway.</summary>
    [Fact]
    public void Harden_grants_are_inheritable_so_subdirectories_get_the_same_acl()
    {
        GrantUsersCreate();
        StateDirectory.Harden(_dir);

        const InheritanceFlags Both = InheritanceFlags.ObjectInherit | InheritanceFlags.ContainerInherit;
        var security = new DirectoryInfo(_dir).GetAccessControl(AccessControlSections.Access);
        var rules = security.GetAccessRules(true, true, typeof(SecurityIdentifier))
            .Cast<FileSystemAccessRule>().ToList();

        Assert.NotEmpty(rules);
        Assert.All(rules, r =>
        {
            Assert.Equal(AccessControlType.Allow, r.AccessControlType);
            Assert.Equal(Both, r.InheritanceFlags);
            Assert.Equal(PropagationFlags.None, r.PropagationFlags);
        });
        // ...and the only identity in that inheritable set that is not an admin holds no write.
        Assert.Equal(default, RightsFor(Users) & FileSystemRights.WriteData);
    }

    /// <summary>Runs on every service boot, so a correct ACL must be a no-op -- both to keep
    /// the log quiet and so an admin's own widening is not reverted on each restart.</summary>
    [Fact]
    public void Harden_is_idempotent_and_reports_nothing_the_second_time()
    {
        GrantUsersCreate();
        Assert.NotNull(StateDirectory.Harden(_dir));
        Assert.Null(StateDirectory.Harden(_dir));
    }

    [Fact]
    public void Harden_creates_the_directory_when_it_is_missing()
    {
        var fresh = Path.Combine(_dir, "nested", "state");

        StateDirectory.Harden(fresh);

        Assert.True(Directory.Exists(fresh));
        Assert.Equal(default, RightsFor(Users, fresh) & FileSystemRights.WriteData);
    }
}
