using System.Management;

namespace TempMonitorAgent.Bios;

/// <summary>
/// The decisions behind an <c>update_bios</c> flash, kept separate from the executor that
/// performs it so every one of them is testable on a machine that is none of these vendors
/// (roadmap #9).
///
/// **This class does not know how to flash a BIOS, and that is deliberate.** The payload IS
/// the vendor's own update executable -- a Dell Update Package, an HP SoftPaq, Lenovo's
/// WinUPTP -- so the work is running it with the right switches, not reimplementing it. What
/// the agent owns is the part that has to be right on every vendor: refusing the wrong
/// hardware, refusing a machine that might lose power, keeping the setup password off the
/// command line, and reading an exit code without inventing a success.
///
/// **The agent never reboots the machine.** A vendor tool exits after STAGING the image; the
/// firmware writes it during the next POST. Restarting somebody's PC as a side effect of an
/// update they were not told about is not a decision an agent should take, and the hub is
/// built for the alternative: the target sits in `rebooting` and is confirmed whenever the
/// machine next comes back reporting a version. A day is the default grace, so a PC flashed on
/// a Friday evening is confirmed on Monday morning.
///
/// **The vendor and model are checked AGAIN here.** The hub checked them against an inventory
/// that can be hours old; this checks what the hardware says about itself at the moment of the
/// flash. Between the two a chassis can be swapped, a machine re-imaged, or a hostname reused
/// -- and this is the one operation in the product with no undo.
/// </summary>
public static class FirmwareFlasher
{
    /// <summary>How the machine is powered, as far as we could tell. `OnBattery` null means we
    /// could not determine it -- which is NOT treated as mains power: see
    /// <see cref="CheckPower"/>.</summary>
    public sealed record PowerState(bool? OnBattery, int? BatteryPercent);

    /// <summary>What to run. `PasswordFileContent` is written to a temp file by the caller and
    /// substituted into the arguments, so the password never appears in the command line.</summary>
    public sealed record Plan(string FileName, string Arguments);

    /// <summary>The argument template each vendor's own updater is normally driven with, used
    /// when an operator left the arguments blank.
    ///
    /// These are DEFAULTS, not knowledge: the payload carries whatever the operator's own
    /// vendor documentation says, and that always wins. A per-vendor table of switches
    /// guessed from documentation is the same mistake as a per-vendor "requires reboot" table
    /// -- right on the vendor somebody tested, wrong on the third one -- so the only thing
    /// defaulted here is silence, which every vendor spells differently and every operator
    /// wants.
    ///
    /// {password_file} and {password} are substituted; a template that names neither simply
    /// runs without one, which is correct for a machine with no setup password.
    /// </summary>
    public static string DefaultArguments(string vendor) => Vendor(vendor) switch
    {
        // Dell Update Packages: /s silent. The password switch takes the value inline, which
        // is why Dell is the one vendor where {password} is used -- see PasswordToken.
        "dell" => "/s",
        // HP's updaters read the setup password from a FILE, which is the shape we would
        // choose anyway.
        "hp" => "-s",
        "lenovo" => "-s",
        _ => "",
    };

    /// <summary>The switch that supplies a BIOS setup password, per vendor, or "" where we do
    /// not know one. Kept beside the arguments so the two cannot disagree.</summary>
    public static string PasswordToken(string vendor) => Vendor(vendor) switch
    {
        // Inline, because Dell's updater has no password-file form. Named here rather than
        // buried in a string so the cost is visible: for the seconds this process runs, the
        // password is readable in the command line by any local process. Every other vendor
        // below avoids that, and an operator who cannot accept it can clear the stored
        // password and flash a machine that has none.
        "dell" => "/p={password}",
        "hp" => "-p\"{password_file}\"",
        "lenovo" => "-pass\"{password_file}\"",
        _ => "",
    };

    private static string Vendor(string vendor)
    {
        vendor = (vendor ?? "").Trim().ToLowerInvariant();
        if (vendor.Contains("dell")) return "dell";
        if (vendor.Contains("hewlett") || vendor.StartsWith("hp")) return "hp";
        if (vendor.Contains("lenovo")) return "lenovo";
        return "";
    }

    /// <summary>True when this vendor's password switch needs a file on disk.</summary>
    public static bool NeedsPasswordFile(string vendor)
        => PasswordToken(vendor).Contains("{password_file}");

    /// <summary>
    /// Refuse the flash, or return null to proceed. The reason is the answer, not a boolean:
    /// "this machine reports model 'OptiPlex 7010', which this image does not list" is
    /// something an operator can act on, and "refused" alone is not.
    /// </summary>
    public static string? CheckHardware(string reportedVendor, string reportedModel,
                                        string payloadVendor,
                                        IReadOnlyList<string> payloadModels)
    {
        static string Norm(string s) => (s ?? "").Trim();
        static bool Same(string a, string b)
            => string.Equals(Norm(a), Norm(b), StringComparison.OrdinalIgnoreCase);

        if (Norm(reportedVendor).Length == 0)
            return "this machine does not report a manufacturer, so the image cannot be matched "
                   + "to it";
        if (!Same(reportedVendor, payloadVendor))
            return $"this machine reports manufacturer '{Norm(reportedVendor)}', and the image "
                   + $"is for '{Norm(payloadVendor)}'";
        if (Norm(reportedModel).Length == 0)
            return "this machine does not report a model, so the image cannot be matched to it";
        if (payloadModels.Count == 0)
            return "the image lists no models, so there is nothing to match this machine against";
        if (!payloadModels.Any(m => Same(reportedModel, m)))
            return $"this machine reports model '{Norm(reportedModel)}', which is not one of "
                   + $"the models this image lists ({string.Join(", ", payloadModels)})";
        return null;
    }

    /// <summary>
    /// Refuse a flash the machine might not survive, or return null.
    ///
    /// **An unknown power state refuses when mains power is required.** A laptop whose battery
    /// WMI class is missing or throwing is exactly the machine this check exists for, and
    /// treating "we could not tell" as "it is plugged in" would spend the one guess that has
    /// no undo. A desktop reports no battery at all, which is a positive answer rather than an
    /// unknown one -- see <see cref="ReadPower"/>.
    /// </summary>
    public static string? CheckPower(PowerState power, bool requireAc, int minBatteryPercent)
    {
        if (requireAc)
        {
            if (power.OnBattery is null)
                return "this machine's power source could not be determined, and firmware "
                       + "updates are configured to require mains power";
            if (power.OnBattery == true)
                return "this machine is running on battery, and firmware updates are "
                       + "configured to require mains power";
        }
        if (minBatteryPercent > 0 && power.BatteryPercent is int percent
            && percent < minBatteryPercent)
        {
            return $"this machine's battery is at {percent}%, below the {minBatteryPercent}% "
                   + "required for a firmware update";
        }
        return null;
    }

    /// <summary>
    /// Read the power state through WMI. A machine with no <c>Win32_Battery</c> instance is a
    /// desktop: reported as NOT on battery with no percentage, which is a real answer. Only a
    /// throwing or unreadable class yields the unknown that <see cref="CheckPower"/> refuses on.
    /// </summary>
    public static PowerState ReadPower()
    {
        try
        {
            using var searcher = new ManagementObjectSearcher(
                @"root\cimv2", "SELECT BatteryStatus, EstimatedChargeRemaining FROM Win32_Battery");
            using var results = searcher.Get();
            bool? onBattery = null;
            int? percent = null;
            foreach (ManagementBaseObject row in results)
            {
                using (row)
                {
                    // BatteryStatus 1 is "discharging"; 2 is "on AC". The rest are charging
                    // and error states, all of which mean mains power is present.
                    var status = Convert.ToInt32(row["BatteryStatus"] ?? 0);
                    onBattery = status == 1;
                    if (row["EstimatedChargeRemaining"] is not null)
                        percent = Convert.ToInt32(row["EstimatedChargeRemaining"]);
                    break;
                }
            }
            // No battery instance at all -> a desktop. Positively not on battery.
            return new PowerState(onBattery ?? false, percent);
        }
        catch (Exception)
        {
            return new PowerState(null, null);
        }
    }

    /// <summary>
    /// Assemble the command line. `passwordFile` is a path already written by the caller (or
    /// null); `password` is the raw value, used only by vendors with no file form.
    /// </summary>
    public static Plan BuildPlan(string vendor, string imagePath, string? operatorArgs,
                                 string? password, string? passwordFile)
    {
        var args = string.IsNullOrWhiteSpace(operatorArgs)
            ? DefaultArguments(vendor)
            : operatorArgs.Trim();

        if (!string.IsNullOrEmpty(password))
        {
            var token = PasswordToken(vendor);
            // A vendor we have no password switch for still flashes -- plenty of machines have
            // no setup password, and refusing outright would turn "we do not know this
            // vendor's switch" into "this machine cannot be updated". If the firmware really
            // does demand one, its own tool refuses and that message is what gets reported.
            if (token.Length > 0)
                args = (args + " " + token).Trim();
        }

        args = args
            .Replace("{password_file}", passwordFile ?? "", StringComparison.Ordinal)
            .Replace("{password}", password ?? "", StringComparison.Ordinal)
            .Replace("{file}", imagePath, StringComparison.Ordinal);
        return new Plan(imagePath, args);
    }

    /// <summary>
    /// What an exit code means. Returns null when the flash was staged, or the reason it was
    /// not.
    ///
    /// **"Reboot required" is a SUCCESS here**, and on this feature it is the normal one: the
    /// image is staged and the firmware writes it during POST. Treating a reboot code as a
    /// failure would mark every successful flash red -- the same trap 3010 is for installers,
    /// which is why the deploy feature's default success set carries it too.
    /// </summary>
    public static string? ClassifyExit(string vendor, int exitCode)
    {
        // Vendor-specific staged-successfully codes, alongside the two universal ones.
        //   Dell Update Packages: 2 and 6 both mean the update is staged and needs a restart.
        //   HP and Lenovo updaters use the Windows installer convention.
        var staged = Vendor(vendor) switch
        {
            "dell" => new[] { 0, 2, 6, 3010 },
            _ => new[] { 0, 3010 },
        };
        if (staged.Contains(exitCode)) return null;
        return $"the manufacturer's update tool exited with code {exitCode}";
    }
}
