using System.Management;

namespace TempMonitorAgent.Security;

/// <summary>
/// Suspend BitLocker on the OS volume for exactly one restart, ahead of a firmware flash
/// (roadmap #9), and resume it if the flash never got as far as needing that restart.
///
/// **Why this exists: no Dell flash on an encrypted machine ever applied.** The vendor tool
/// staged the image and exited "reboot required", the machine restarted, and it came back on
/// its old BIOS -- every time, on every Dell in the fleet, with nothing anywhere saying why
/// (FCOM1109, October 2026). Dell's own BIOS update guidance and the usual ConfigMgr BIOS
/// task sequence both suspend BitLocker first, because a firmware change alters the TPM
/// measurements the volume key is sealed to.
///
/// <c>DisableKeyProtectors(1)</c> rather than an unbounded suspend: Windows resumes
/// protection by itself after one restart, so an agent that crashes, or a machine that is
/// never flashed, cannot leave a volume suspended for good. The cost is real and named in
/// the hub setting that gates this (`firmware.suspend_bitlocker`): until that restart, the
/// volume key sits on the disk in the clear.
///
/// Not a *Reader -- it changes the machine -- so it is its own class beside
/// <see cref="BitLockerReader"/>, sharing only its packet-privacy connection.
/// </summary>
public static class BitLockerSuspender
{
    /// <summary>What happened, in the words the hub stores on the target row.</summary>
    public const string StateOff = "off";               // not protected; nothing to do
    public const string StateSuspended = "suspended";   // was on, now off for one restart
    public const string StateOnKept = "on";             // protected, and left that way
    public const string StateUnsupported = "unsupported";
    public const string StateError = "error";

    /// <summary>Suspend the system volume's protectors for one restart when
    /// <paramref name="suspend"/> is true; otherwise only report whether it is protected.
    /// Never throws: a flash that cannot read BitLocker is still worth running, and the
    /// state string tells the operator why it may not take.</summary>
    public static string Prepare(bool suspend)
    {
        try
        {
            using var volume = SystemVolume();
            if (volume is null) return StateError + ": the system volume was not found";
            var status = Convert.ToUInt32(volume["ProtectionStatus"] ?? 2u);
            if (status == 0) return StateOff;
            if (!suspend) return StateOnKept;

            using var args = volume.GetMethodParameters("DisableKeyProtectors");
            args["DisableCount"] = 1u;
            using var result = volume.InvokeMethod("DisableKeyProtectors", args, null);
            var code = Convert.ToUInt32(result?["ReturnValue"] ?? 1u);
            return code == 0 ? StateSuspended : $"{StateError}: DisableKeyProtectors returned 0x{code:X8}";
        }
        catch (BitLockerUnsupportedException)
        {
            return StateUnsupported;
        }
        catch (Exception e)
        {
            return $"{StateError}: {e.Message}";
        }
    }

    /// <summary>Put protection back at once, for a flash that failed before staging
    /// anything. Leaving it suspended until a restart nobody is waiting for would widen the
    /// window for no flash at all. Best effort, never throws.</summary>
    public static void Resume()
    {
        try
        {
            using var volume = SystemVolume();
            if (volume is null) return;
            using var result = volume.InvokeMethod("EnableKeyProtectors", null, null);
        }
        catch (Exception)
        {
            // Windows resumes it at the next restart regardless -- DisableCount was 1.
        }
    }

    private static ManagementObject? SystemVolume()
    {
        var drive = (Environment.GetEnvironmentVariable("SystemDrive") ?? "C:").TrimEnd('\\');
        var scope = BitLockerReader.ConnectedScope();
        using var searcher = new ManagementObjectSearcher(scope, new ObjectQuery(
            $"SELECT * FROM Win32_EncryptableVolume WHERE DriveLetter = '{drive.Replace("'", "")}'"));
        using var results = searcher.Get();
        foreach (ManagementBaseObject item in results)
        {
            if (item is ManagementObject volume) return volume;
            item.Dispose();
        }
        return null;
    }
}
