using System.Text;
using Microsoft.Win32;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Network;

/// <summary>
/// Makes this machine wakeable: turns its wired adapters' wake flags on and Windows Fast
/// Startup off (roadmap #10).
///
/// **Most of a Wake-on-LAN rollout is preconditions, not code**, and a console that can only
/// NAME the reasons a machine will not wake leaves somebody visiting forty desks to fix
/// them. This is the remedy half of <c>NicReader</c>'s diagnosis, and it is what makes the
/// feature usable on a fleet rather than on one PC.
///
/// **Three settings, because all three have to be right and each is somewhere different:**
///
///   * <c>powercfg /deviceenablewake</c> -- the device's power policy, the "Allow this
///     device to wake the computer" checkbox. Driven through powercfg rather than a WMI
///     put because that is the documented, supported path, and it is the one that survives
///     a driver update reinstating its own defaults.
///   * <c>*WakeOnMagicPacket</c> -- the driver's own advanced property, which decides
///     whether a magic packet is one of the things the device wakes FOR. A NIC with the
///     checkbox ticked and this turned off looks perfectly configured in Device Manager and
///     ignores every packet the hub sends. **It takes effect when the adapter next
///     initialises**, which is said plainly in the output rather than left for somebody to
///     discover from a wake that does not work until the next reboot.
///   * <c>HiberbootEnabled</c> -- Fast Startup. Hybrid shutdown means a "shut down" machine
///     is really hibernating from a session that never ended, and wake-from-S5 fails on many
///     NICs in that state. The symptom is a machine that wakes from Sleep and not from Shut
///     Down, which reads as an intermittent fault rather than as a setting.
///
/// **Wireless adapters are never touched.** This mechanism cannot wake a laptop over Wi-Fi,
/// so enabling wake on a Wi-Fi NIC would change a machine's power behaviour -- and its
/// battery life -- for no benefit at all.
///
/// **Nothing here is fatal, and every step reports itself.** A machine where two of the
/// three worked is more wakeable than it was, and saying which one did not is what lets
/// somebody finish the job. The final act is to invalidate the inventory, so the console's
/// diagnosis reflects the change on the next heartbeat instead of a quarter of an hour later.
/// </summary>
public sealed class PrepareWakeExecutor : ICommandExecutor
{
    private const string NetClassKey =
        @"SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}";
    private const string PowerKey =
        @"SYSTEM\CurrentControlSet\Control\Session Manager\Power";

    public string Type => "prepare_wake";

    public async Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var log = new StringBuilder();
        void Say(string line) { log.AppendLine(line); onOutput?.Invoke(line + "\n"); }

        var report = NicReader.Read();
        var wired = report.Nics.Where(n => n.Kind == NicReader.KindWired).ToList();
        if (wired.Count == 0)
        {
            // Not a failure of this command: there is genuinely nothing here to enable, and
            // the machine simply cannot be woken by this mechanism. Reported as such so the
            // console's diagnosis and this result agree.
            return CommandResult.Ok("no wired network adapter on this machine; " +
                                    "Wake-on-LAN cannot reach it");
        }

        var changed = 0;

        foreach (var nic in wired)
        {
            ct.ThrowIfCancellationRequested();
            Say($"-- {nic.Name} ({nic.Description})");
            if (await EnableDeviceWakeAsync(nic, ct, Say)) changed++;
            if (EnableMagicPacket(nic, Say)) changed++;
        }

        if (DisableFastStartup(Say)) changed++;

        // The very flags this just changed are what the machine reports, so force the next
        // heartbeat to carry them. Without this an operator watches the old diagnosis for
        // another quarter of an hour and concludes the button did nothing.
        NetworkInventoryReporter.Invalidate();
        NetworkInventoryReporter.RefreshIfDue();

        Say(changed > 0
            ? $"{changed} setting(s) changed; the adapter properties apply when the adapter " +
              "next initialises (a restart is the reliable way)"
            : "everything was already set for Wake-on-LAN");
        return CommandResult.Ok(log.ToString());
    }

    /// <summary>Tick "allow this device to wake the computer" through powercfg.
    ///
    /// The device is named by its adapter DESCRIPTION, which is what powercfg's device list
    /// uses -- the connection Name ("Ethernet 2") is a user-facing label powercfg has never
    /// heard of. A device that is not in `wake_programmable` cannot be enabled at all, and
    /// saying so beats an opaque exit code.</summary>
    private static async Task<bool> EnableDeviceWakeAsync(
        NicInfo nic, CancellationToken ct, Action<string> say)
    {
        if (string.IsNullOrWhiteSpace(nic.Description))
        {
            say("   device wake: skipped (the adapter reports no description to name it by)");
            return false;
        }
        try
        {
            var programmable = await ProcessRunner.RunAsync(
                "powercfg.exe", "/devicequery wake_programmable", ct, timeoutSeconds: 60);
            var supported = programmable.Output
                .Split('\n')
                .Select(line => line.Trim())
                .Any(line => line.Equals(nic.Description, StringComparison.OrdinalIgnoreCase));
            if (!supported)
            {
                say("   device wake: this adapter is not wake-programmable (its firmware or " +
                    "driver does not offer it)");
                return false;
            }

            var result = await ProcessRunner.RunAsync(
                "powercfg.exe", $"/deviceenablewake \"{nic.Description}\"", ct,
                timeoutSeconds: 60);
            if (result.ExitCode == 0)
            {
                say("   device wake: enabled");
                return true;
            }
            say($"   device wake: powercfg exited {result.ExitCode} " +
                $"{result.Output.Trim()}");
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception e)
        {
            say($"   device wake: {e.Message}");
        }
        return false;
    }

    /// <summary>Set `*WakeOnMagicPacket` on the adapter's driver key.
    ///
    /// Written as a STRING, which is how every driver that publishes this property stores
    /// it: writing a REG_DWORD over a REG_SZ leaves a value the driver's own property page
    /// cannot read, and the adapter falls back to its default.</summary>
    private static bool EnableMagicPacket(NicInfo nic, Action<string> say)
    {
        try
        {
            using var classKey = Registry.LocalMachine.OpenSubKey(NetClassKey);
            if (classKey is null)
            {
                say("   magic packet: the network adapter class key is missing");
                return false;
            }
            foreach (var name in classKey.GetSubKeyNames())
            {
                using var probe = classKey.OpenSubKey(name);
                // Matched on the driver description rather than on a MAC: the key's
                // `NetworkAddress` value is the ADMINISTRATIVE override and is absent on
                // almost every adapter, so matching there would silently skip the normal
                // case and quietly enable nothing.
                var description = probe?.GetValue("DriverDesc") as string ?? "";
                if (!string.Equals(description, nic.Description, StringComparison.OrdinalIgnoreCase))
                    continue;

                using var writable = Registry.LocalMachine.OpenSubKey(
                    $@"{NetClassKey}\{name}", writable: true);
                if (writable is null)
                {
                    say("   magic packet: no permission to write the adapter's driver key");
                    return false;
                }
                if (writable.GetValue("*WakeOnMagicPacket") is null)
                {
                    // Absent means the driver does not offer the property. Creating it would
                    // put a value there that nothing reads, and would make the next
                    // inventory report a wake capability this NIC does not have.
                    say("   magic packet: this driver does not expose the setting");
                    return false;
                }
                if (writable.GetValue("*WakeOnMagicPacket")?.ToString()?.Trim() == "1")
                {
                    say("   magic packet: already on");
                    return false;
                }
                writable.SetValue("*WakeOnMagicPacket", "1", RegistryValueKind.String);
                say("   magic packet: enabled (applies when the adapter next initialises)");
                return true;
            }
            say("   magic packet: could not find this adapter's driver key");
        }
        catch (Exception e)
        {
            say($"   magic packet: {e.Message}");
        }
        return false;
    }

    /// <summary>Turn Fast Startup off, so "Shut down" really does mean S5.</summary>
    private static bool DisableFastStartup(Action<string> say)
    {
        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(PowerKey, writable: true);
            if (key is null)
            {
                say("fast startup: could not open the power policy key");
                return false;
            }
            var current = key.GetValue("HiberbootEnabled");
            if (current is not null && Convert.ToInt32(current) == 0)
            {
                say("fast startup: already off");
                return false;
            }
            key.SetValue("HiberbootEnabled", 0, RegistryValueKind.DWord);
            say("fast startup: turned off (a shutdown is now a real shutdown)");
            return true;
        }
        catch (Exception e)
        {
            say($"fast startup: {e.Message}");
            return false;
        }
    }
}
