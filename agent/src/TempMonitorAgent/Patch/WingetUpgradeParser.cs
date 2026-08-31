namespace TempMonitorAgent.Patch;

/// <summary>
/// Turns `winget upgrade` console output into update rows.
///
/// <para><b>This exists as a pure function because winget has no machine-readable output for
/// this command.</b> `winget upgrade` prints a fixed-width table for humans, and every
/// consumer of it in the world is parsing that table. Since the parse is the fragile part and
/// the process launch is not, the two are separated: this half takes a string and is covered
/// by unit tests against real captured output, and <c>PatchScanner</c> only has to get the
/// process invocation right.</para>
///
/// <para><b>Columns are located from the header row, never assumed.</b> winget localises its
/// headers and pads columns to the widest value present, so fixed offsets work on the machine
/// they were written against and silently mis-slice everywhere else — which would show
/// operators an update whose "version" is half a package name. The header's own column
/// positions are the only reliable guide, so the header is found by the `Id` column marker
/// and the offsets are read off it.</para>
///
/// <para><b>Anything that does not parse cleanly is dropped, never guessed at.</b> winget
/// mixes progress spinners, source agreements and a trailing "N upgrades available" line into
/// the same stream. A row that does not yield an id and a name is not an update, and inventing
/// one would put a phantom entry in front of an operator to approve.</para>
/// </summary>
public static class WingetUpgradeParser
{
    /// <summary>Rows winget prints that are not packages. `winget upgrade` ends with a list of
    /// packages it cannot upgrade, under its own heading; those genuinely have no upgrade
    /// available through winget and must not be offered as if they did.</summary>
    private const string UnavailableMarker = "following packages";

    public static IReadOnlyList<AvailableUpdate> Parse(string? output)
    {
        var results = new List<AvailableUpdate>();
        if (string.IsNullOrWhiteSpace(output)) return results;

        var lines = output.Replace("\r\n", "\n").Split('\n');

        // Find the header, and with it the column starts. The `Id` column is the anchor
        // because it is the one header winget does not translate.
        var headerIndex = -1;
        var idStart = -1;
        var versionStart = -1;
        var availableStart = -1;
        for (var i = 0; i < lines.Length; i++)
        {
            var probe = lines[i];
            var id = IndexOfColumn(probe, "Id");
            if (id < 0) continue;
            var version = IndexOfColumn(probe, "Version");
            var available = IndexOfColumn(probe, "Available");
            if (version < 0 || available < 0) continue;
            headerIndex = i;
            idStart = id;
            versionStart = version;
            availableStart = available;
            break;
        }
        if (headerIndex < 0) return results;

        for (var i = headerIndex + 1; i < lines.Length; i++)
        {
            var line = lines[i];
            if (string.IsNullOrWhiteSpace(line)) continue;
            // The dashed rule winget draws under its header.
            if (line.TrimStart().StartsWith('-')) continue;
            // Everything after the "packages have no available upgrade" heading is not an
            // upgrade, whatever it looks like.
            if (line.Contains(UnavailableMarker, StringComparison.OrdinalIgnoreCase)) break;
            // A short line is the trailing "N upgrades available" summary, not a row.
            if (line.Length <= idStart) continue;

            var name = Slice(line, 0, idStart);
            var id = Slice(line, idStart, versionStart);
            var current = Slice(line, versionStart, availableStart);
            var available = Slice(line, availableStart, line.Length);

            if (id.Length == 0 || name.Length == 0) continue;
            // An id with a space in it is a mis-slice, not a package: winget package ids are
            // dotted identifiers. Dropping is right — a half-parsed row is worse than none.
            if (id.Contains(' ')) continue;

            // `available` carries the version being offered and is what makes this row an
            // upgrade at all. Its absence means the columns did not line up.
            if (available.Length == 0) continue;

            results.Add(new AvailableUpdate(
                // Version is deliberately NOT part of the uid. An approval says "Firefox may
                // update"; making it version-specific would silently un-approve the package
                // on the day a new build lands, which reads to an operator as the approval
                // having been forgotten.
                Uid: $"{PatchSources.Winget}:{id.ToLowerInvariant()}",
                NativeId: id,
                Source: PatchSources.Winget,
                Kb: "",
                Title: current.Length > 0 ? $"{name} {current} → {available}"
                                          : $"{name} → {available}",
                // winget has no notion of classification at all. Saying so is better than
                // filing every application update under "other" as though somebody decided.
                Classification: "unknown",
                RebootRequired: false,
                SizeBytes: 0));
        }
        return results;
    }

    /// <summary>Where a header column starts, or -1. Matched as a whole word so `Id` does not
    /// hit inside `Identifier` and `Version` does not hit inside `VersionAvailable`.</summary>
    private static int IndexOfColumn(string line, string header)
    {
        var at = 0;
        while (true)
        {
            var found = line.IndexOf(header, at, StringComparison.Ordinal);
            if (found < 0) return -1;
            var beforeOk = found == 0 || line[found - 1] == ' ';
            var afterAt = found + header.Length;
            var afterOk = afterAt >= line.Length || line[afterAt] == ' ';
            if (beforeOk && afterOk) return found;
            at = found + 1;
        }
    }

    private static string Slice(string line, int start, int end)
    {
        if (start >= line.Length) return "";
        if (end > line.Length) end = line.Length;
        if (end <= start) return "";
        return line[start..end].Trim();
    }
}
