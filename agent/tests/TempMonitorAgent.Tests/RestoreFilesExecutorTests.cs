using System.Formats.Tar;
using System.IO.Compression;
using System.Text;
using System.Text.Json.Nodes;
using TempMonitorAgent.Backup;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Where a restore puts each file, and whether it can be talked into putting it somewhere
/// else. Roadmap #1b.
///
/// A restore archive is a file that came back over the network from a machine that was, by
/// assumption, worth backing up — but is not necessarily trustworthy NOW. It is unpacked by
/// a service running as SYSTEM. So the emphasis here is the pair of properties that make
/// that safe: a member can never escape the folder it is being restored into, and a
/// restore to "the original location" only ever writes to an absolute local path the hub's
/// own manifest recorded.
///
/// The round-trip test builds a real archive (tar → gzip → FHBK1 envelope) and unpacks it,
/// because the failure this feature actually has is subtler than a crash: the member names
/// not lining up, so the restore downloads gigabytes, matches nothing, and reports every
/// file as missing.
/// </summary>
public class RestoreFilesExecutorTests
{
    [Theory]
    // Into a folder: the tree is rebuilt from the MEMBER name, so two files called
    // report.docx from different folders cannot collide.
    [InlineData("C/Users/bob/a.txt", "C:\\Restored\\C\\Users\\bob\\a.txt")]
    [InlineData("D/Finance/q1.xlsx", "C:\\Restored\\D\\Finance\\q1.xlsx")]
    public void RestoringIntoAFolderRebuildsTheTreeUnderIt(string member, string expected)
        => Assert.Equal(expected,
                        RestoreFilesExecutor.ResolveTarget(member, "C:\\ignored", "C:\\Restored"));

    [Theory]
    // The classic tar traversal, in the shapes it actually arrives in.
    [InlineData("../../Windows/System32/evil.dll")]
    [InlineData("C/../../../Windows/evil.dll")]
    [InlineData("C:/Windows/System32/evil.dll")]
    [InlineData("")]
    public void AMemberCannotEscapeTheRestoreFolder(string member)
        => Assert.Null(RestoreFilesExecutor.ResolveTarget(member, "C:\\ignored", "C:\\Restored"));

    [Fact]
    public void RestoringToTheOriginalLocationUsesTheManifestPath()
        => Assert.Equal("C:\\Users\\bob\\a.txt",
                        RestoreFilesExecutor.ResolveTarget("C/Users/bob/a.txt",
                                                           "C:\\Users\\bob\\a.txt", ""));

    [Fact]
    public void ADotDotInAFILENAMEIsNotATraversal()
    {
        // `report..final.txt` is an ordinary filename. Refusing it on a substring match
        // would silently drop real files from a restore for no security gain — the check
        // has to be on path SEGMENTS.
        const string path = "C:\\Users\\bob\\report..final.txt";
        Assert.Equal(path, RestoreFilesExecutor.ResolveTarget("C/Users/bob/report..final.txt",
                                                              path, ""));
    }

    [Theory]
    // With no target folder there is nothing to anchor a relative path against — and the
    // working directory of a SYSTEM service is C:\Windows\System32, which is precisely
    // where a file must not land by accident.
    [InlineData("relative\\path.txt")]
    [InlineData("\\\\server\\share\\f.txt")]
    [InlineData("C:\\Users\\..\\Windows\\evil.dll")]
    public void RestoringToTheOriginalLocationRefusesAnythingButALocalAbsolutePath(string original)
        => Assert.Null(RestoreFilesExecutor.ResolveTarget("x/y.txt", original, ""));

    [Fact]
    public void UnpacksOnlyTheMembersTheRestoreAskedFor()
    {
        var temp = Path.Combine(Path.GetTempPath(), "fh-restore-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(temp);
        try
        {
            var key = new byte[32];
            Random.Shared.NextBytes(key);
            var archive = Path.Combine(temp, "archive.fhb");
            WriteArchive(archive, key, new Dictionary<string, string>
            {
                ["C/Users/bob/wanted.txt"] = "keep me",
                ["C/Users/bob/other.txt"] = "not asked for",
            });

            var wantedPath = Path.Combine(temp, "out", "wanted.txt");
            var failures = new List<string>();
            var (files, bytes) = RestoreFilesExecutor.Unpack(
                archive, key,
                new Dictionary<string, string> { ["C/Users/bob/wanted.txt"] = wantedPath },
                failures);

            Assert.Equal(1, files);
            Assert.Equal("keep me", File.ReadAllText(wantedPath));
            Assert.Equal(new FileInfo(wantedPath).Length, bytes);
            // A member present in the archive but not asked for must not be written: a
            // restore of one file has to be a restore of one file.
            Assert.False(File.Exists(Path.Combine(temp, "out", "other.txt")));
            Assert.Empty(failures);
        }
        finally { Directory.Delete(temp, recursive: true); }
    }

    [Fact]
    public void AMemberThePlanPromisedButTheArchiveLacksIsNamed()
    {
        // The hub's manifest and the archive disagreeing is exactly the case a silent
        // short count would hide — and the hub turns any short count into a failed restore,
        // so the reason has to travel with it.
        var temp = Path.Combine(Path.GetTempPath(), "fh-restore-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(temp);
        try
        {
            var key = new byte[32];
            Random.Shared.NextBytes(key);
            var archive = Path.Combine(temp, "archive.fhb");
            WriteArchive(archive, key,
                         new Dictionary<string, string> { ["C/Users/bob/there.txt"] = "hi" });

            var failures = new List<string>();
            var (files, _) = RestoreFilesExecutor.Unpack(
                archive, key,
                new Dictionary<string, string>
                {
                    ["C/Users/bob/missing.txt"] = Path.Combine(temp, "out", "missing.txt"),
                },
                failures);

            Assert.Equal(0, files);
            Assert.Contains(failures, f => f.Contains("missing.txt")
                                           && f.Contains("not present"));
        }
        finally { Directory.Delete(temp, recursive: true); }
    }

    [Fact]
    public void AnArchiveSealedWithAnotherMachinesKeyIsReportedNotThrown()
    {
        // A restore run against the wrong key is an operator-visible problem, not a crash
        // in a background service: the run has to close with a reason.
        var temp = Path.Combine(Path.GetTempPath(), "fh-restore-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(temp);
        try
        {
            var key = new byte[32];
            var wrongKey = new byte[32];
            Random.Shared.NextBytes(key);
            Random.Shared.NextBytes(wrongKey);
            var archive = Path.Combine(temp, "archive.fhb");
            WriteArchive(archive, key,
                         new Dictionary<string, string> { ["C/a.txt"] = "hi" });

            var failures = new List<string>();
            var (files, _) = RestoreFilesExecutor.Unpack(
                archive, wrongKey,
                new Dictionary<string, string> { ["C/a.txt"] = Path.Combine(temp, "a.txt") },
                failures);

            Assert.Equal(0, files);
            Assert.Contains(failures, f => f.Contains("could not be decrypted"));
        }
        finally { Directory.Delete(temp, recursive: true); }
    }

    /// <summary>Build a real tar → gzip → FHBK1 archive, the way BackupFilesExecutor does.</summary>
    private static void WriteArchive(string path, byte[] key, Dictionary<string, string> members)
    {
        using var plain = new MemoryStream();
        using (var gzip = new GZipStream(plain, CompressionLevel.Fastest, leaveOpen: true))
        using (var tar = new TarWriter(gzip, TarEntryFormat.Pax, leaveOpen: true))
        {
            foreach (var (name, content) in members)
            {
                using var body = new MemoryStream(Encoding.UTF8.GetBytes(content));
                tar.WriteEntry(new PaxTarEntry(TarEntryType.RegularFile, name)
                {
                    DataStream = body,
                });
            }
        }
        plain.Position = 0;
        using var destination = new FileStream(path, FileMode.Create, FileAccess.Write);
        BackupEnvelope.Write(plain, destination, key, new JsonObject
        {
            ["kind"] = "machine_files",
            ["machine"] = "TEST-PC",
        });
    }
}
