using System.Xml.Linq;

namespace FleetHubAgent.Core.Tests;

/// <summary>
/// The manifest declares what locating a device actually needs, and still does not declare what
/// it must never need.
///
/// **The silent failure this catches is the one phase B shipped with.** A dangerous permission
/// that is not in the manifest cannot be granted by anybody -- not by the person holding the
/// device, not by a Device Owner, not by an MDM -- and the platform says so by returning denied
/// rather than by failing anything. AndroidLocationReader then reports "the location permission
/// has not been granted on this device", which is true, which the hub files as a perfectly
/// ordinary `unavailable`, and which reads in the console like a device somebody locked down.
/// Every locate in the fleet answers that way, forever, with nothing in any log. Deleting a
/// line from the manifest is a one-character change that nothing else in this repo would
/// notice.
///
/// **ACCESS_BACKGROUND_LOCATION is asserted ABSENT, and that assertion is the point of this
/// file as much as the other two.** It is the permission a well-meaning fix reaches for the
/// first time a locate comes back empty from a phone in a pocket, and it is the one thing this
/// feature promised not to hold: a standing right to follow a device, rather than an answer to
/// a question somebody asked once and that the device announced. SECURITY.MD's personal-data
/// inventory and ROADMAP.MD #23 both rest on its absence. The narrow alternative it exists to
/// protect is FOREGROUND_SERVICE_LOCATION, which is why that one is checked in the same place:
/// drop it and the promotion throws SecurityException on Android 14, and the real fix looks
/// exactly like adding background location.
///
/// Read from the manifest on disk rather than from a built APK, so this runs with no Android
/// SDK, no workload and no device -- which is the whole reason FleetHubAgent.Core.Tests exists.
/// </summary>
public class AndroidManifestPermissionTests
{
    private const string AndroidNs = "http://schemas.android.com/apk/res/android";

    private static HashSet<string> DeclaredPermissions()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            var candidate = Path.Combine(dir.FullName, "src", "FleetHubAgent.Android",
                                         "Properties", "AndroidManifest.xml");
            if (File.Exists(candidate))
            {
                return XDocument.Load(candidate)
                    .Descendants("uses-permission")
                    .Select(e => (string?)e.Attribute(XName.Get("name", AndroidNs)))
                    .Where(name => !string.IsNullOrEmpty(name))
                    .Select(name => name!)
                    .ToHashSet(StringComparer.Ordinal);
            }
            dir = dir.Parent;
        }
        throw new FileNotFoundException(
            "Could not find the Android manifest above " + AppContext.BaseDirectory);
    }

    [Theory]
    [InlineData("android.permission.ACCESS_FINE_LOCATION")]
    [InlineData("android.permission.ACCESS_COARSE_LOCATION")]
    [InlineData("android.permission.FOREGROUND_SERVICE_LOCATION")]
    public void Locating_a_device_needs_these(string permission)
    {
        Assert.Contains(permission, DeclaredPermissions());
    }

    [Fact]
    public void Background_location_is_never_requested()
    {
        // Not a style rule. See the class docstring: this is the difference between answering
        // "where is this device" once, out loud, and holding the right to watch where it goes.
        Assert.DoesNotContain("android.permission.ACCESS_BACKGROUND_LOCATION",
                              DeclaredPermissions());
    }

    [Fact]
    public void The_manifest_was_actually_read()
    {
        // A guard against the guard. A path that stopped resolving, or an attribute namespace
        // that silently matched nothing, would make the absence test above pass vacuously --
        // and that is the assertion whose failure nobody would ever see coming.
        Assert.Contains("android.permission.INTERNET", DeclaredPermissions());
    }
}
