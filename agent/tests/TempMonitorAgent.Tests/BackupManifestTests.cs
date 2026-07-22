using System.Formats.Tar;
using System.IO.Compression;
using System.Text;
using System.Text.Json.Nodes;
using TempMonitorAgent.Backup;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The incremental decision and the archive format — the two places a per-PC backup can be
/// silently wrong.
///
/// "Silently wrong" is the operative phrase. A backup that fails loudly gets fixed the
/// next morning; one that skips a changed file, or writes a tar a restore cannot read,
/// looks green for months and is discovered at exactly the wrong moment. So the emphasis
/// is on: a file that changed must never be skipped, a deletion must be recorded, and the
/// archive must round-trip through the real stdlib tar reader.
/// </summary>
public class BackupManifestTests
{
    private static ManifestEntry Entry(string path, long size, long mtime, string sha = "aa")
        => new() { Path = path, Size = size, Mtime = mtime, Sha256 = sha };

    /// <summary>The shared fixture's machine key, plus where to drop artifacts the Python
    /// suite reads. Falls back to a throwaway key if the fixture is missing, so this file
    /// still runs standalone.</summary>
    private static (byte[] Key, string Machine, string? Dir) FixtureKey()
    {
        for (var probe = new DirectoryInfo(AppContext.BaseDirectory); probe is not null; probe = probe.Parent)
        {
            var candidate = Path.Combine(probe.FullName, "tests", "fixtures", "envelope.json");
            if (!File.Exists(candidate)) continue;
            var meta = JsonNode.Parse(File.ReadAllText(candidate))!.AsObject();
            return (Convert.FromBase64String(meta["machine_key_b64"]!.GetValue<string>()),
                    meta["machine"]!.GetValue<string>(),
                    Path.GetDirectoryName(candidate));
        }
        var fallback = new byte[32];
        Random.Shared.NextBytes(fallback);
        return (fallback, "FALLBACK-PC", null);
    }

    [Fact]
    public void AnUnknownFileCountsAsChanged()
    {
        var manifest = new BackupManifest();
        Assert.True(manifest.HasChanged(@"C:\new.txt", 10, 100));
    }

    [Fact]
    public void SameSizeAndMtimeIsUnchanged()
    {
        var manifest = new BackupManifest();
        manifest.Files[@"C:\a.txt"] = Entry(@"C:\a.txt", 10, 100);
        Assert.False(manifest.HasChanged(@"C:\a.txt", 10, 100));
    }

    [Theory]
    [InlineData(11, 100)]   // grew
    [InlineData(9, 100)]    // shrank
    [InlineData(10, 101)]   // rewritten in place, same length
    public void ADifferentSizeOrMtimeIsChanged(long size, long mtime)
    {
        var manifest = new BackupManifest();
        manifest.Files[@"C:\a.txt"] = Entry(@"C:\a.txt", 10, 100);
        Assert.True(manifest.HasChanged(@"C:\a.txt", size, mtime));
    }

    [Fact]
    public void PathsAreComparedCaseInsensitively()
    {
        // Windows is case-insensitive; treating C:\A.txt and C:\a.txt as different files
        // would re-upload the whole profile whenever anything renamed its casing.
        var manifest = new BackupManifest();
        manifest.Files[@"C:\Users\Bob\A.txt"] = Entry(@"C:\Users\Bob\A.txt", 10, 100);
        Assert.False(manifest.HasChanged(@"c:\users\bob\a.txt", 10, 100));
    }

    [Fact]
    public void APreviouslyDeletedFileCountsAsChangedWhenItComesBack()
    {
        var manifest = new BackupManifest();
        manifest.Files[@"C:\a.txt"] = new ManifestEntry { Path = @"C:\a.txt", Deleted = true };
        Assert.True(manifest.HasChanged(@"C:\a.txt", 10, 100));
    }

    [Fact]
    public void RoundTripsThroughDisk()
    {
        var chainId = "chain" + Guid.NewGuid().ToString("N")[..8];
        var manifest = new BackupManifest { ChainId = chainId, Sequence = 3 };
        manifest.Files[@"C:\Users\bob\a.txt"] = Entry(@"C:\Users\bob\a.txt", 42, 1700, "beef");
        manifest.Save();
        try
        {
            var loaded = BackupManifest.Load(chainId);
            Assert.NotNull(loaded);
            Assert.Equal(3, loaded!.Sequence);
            Assert.Equal(42, loaded.Files[@"C:\Users\bob\a.txt"].Size);
            // The comparer must survive deserialization, or the first incremental after a
            // restart re-uploads everything.
            Assert.False(loaded.HasChanged(@"c:\users\BOB\a.txt", 42, 1700));
        }
        finally
        {
            BackupManifest.PruneOthers("nothing-matches-this");
        }
    }

    [Fact]
    public void LoadReturnsNullForAnUnknownChain()
        => Assert.Null(BackupManifest.Load("chain-that-was-never-written"));

    [Fact]
    public void LoadReturnsNullForACorruptCache()
    {
        // Corrupt cache == no cache == force a full. Salvaging half a manifest is how an
        // incremental with holes in it gets uploaded and recorded as complete.
        var chainId = "corrupt" + Guid.NewGuid().ToString("N")[..8];
        var manifest = new BackupManifest { ChainId = chainId };
        manifest.Save();
        var dir = Path.Combine(AgentConfig.ProgramDataDir, "backup");
        var file = Directory.GetFiles(dir, "*.json")
                            .First(f => Path.GetFileNameWithoutExtension(f) == chainId);
        File.WriteAllText(file, "{ this is not json");
        try { Assert.Null(BackupManifest.Load(chainId)); }
        finally { File.Delete(file); }
    }

    [Fact]
    public void HashesAFileTheSameWayTheHubExpects()
    {
        var path = Path.Combine(Path.GetTempPath(), Guid.NewGuid().ToString("N") + ".txt");
        File.WriteAllText(path, "hello");
        try
        {
            // sha256("hello"), so a hash mismatch here is this code and not the algorithm.
            Assert.Equal("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
                         BackupManifest.HashFile(path));
        }
        finally { File.Delete(path); }
    }

    /// <summary>
    /// The archive-member naming contract, against the SAME vectors the hub's
    /// backup_paths.archive_member is tested with.
    ///
    /// This is the other half of the shared-fixture discipline PathExpanderTests follows.
    /// The agent writes these names into the tar; the hub names them again when it plans a
    /// restore of an archive it never wrote. Drift is not a crash — it is a restore that
    /// downloads the right archive, matches nothing, and reports every file missing.
    /// </summary>
    [Fact]
    public void ArchiveMemberNamesMatchTheSharedVectors()
    {
        var vectors = LoadVectors();
        foreach (var node in vectors["members"]!.AsArray())
        {
            var entry = node!.AsObject();
            Assert.Equal(entry["member"]!.GetValue<string>(),
                         BackupManifest.ArchiveMember(entry["path"]!.GetValue<string>()));
        }
    }

    [Theory]
    // Where a restore puts a member back. Only the first segment can have been a drive.
    [InlineData("C/Users/bob/a.txt", "C:\\Users\\bob\\a.txt")]
    [InlineData("D/Finance/q1.xlsx", "D:\\Finance\\q1.xlsx")]
    [InlineData("C", "C:\\")]
    // A UNC source is unreconstructable — \\srv\share\f and srv/share/f are one member —
    // so it stays relative rather than being guessed onto some drive.
    [InlineData("srv/share/f.txt", "srv\\share\\f.txt")]
    public void MemberToPathIsTheInverseOfArchiveMember(string member, string expected)
        => Assert.Equal(expected, BackupManifest.MemberToPath(member));

    private static JsonObject LoadVectors()
    {
        for (var probe = new DirectoryInfo(AppContext.BaseDirectory); probe is not null; probe = probe.Parent)
        {
            var candidate = Path.Combine(probe.FullName, "tests", "backup_path_vectors.json");
            if (File.Exists(candidate))
                return JsonNode.Parse(File.ReadAllText(candidate))!.AsObject();
        }
        throw new FileNotFoundException("tests/backup_path_vectors.json not found");
    }

    [Fact]
    public void ArchiveRoundTripsThroughTheRealTarReader()
    {
        // The archive is tar → gzip → envelope specifically so restore_backup.py can
        // unpack it with stdlib tarfile and no hub. If the tar layer is malformed, that
        // property is gone and nobody notices until a restore.
        var files = new List<ManifestEntry>();
        var temp = Path.Combine(Path.GetTempPath(), "fh-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(temp);
        try
        {
            for (int i = 0; i < 3; i++)
            {
                var path = Path.Combine(temp, $"file{i}.txt");
                File.WriteAllText(path, $"contents of file {i}");
                var info = new FileInfo(path);
                files.Add(new ManifestEntry
                {
                    Path = path,
                    Size = info.Length,
                    Mtime = new DateTimeOffset(info.LastWriteTimeUtc).ToUnixTimeSeconds(),
                    Sha256 = BackupManifest.HashFile(path),
                });
            }

            // Sealed with the SHARED fixture key and written to tests/fixtures, so
            // tests/test_backups.py unpacks this exact archive with stdlib tarfile. That
            // is the contract restore_backup.py depends on — a C#-only assertion here
            // would not catch a tar Python cannot read.
            var (key, machine, fixtureDir) = FixtureKey();
            using var sealedStream = new MemoryStream();
            using (var pipe = new TarGzipPipe(files, new VssSnapshot(NullLogger.Instance),
                                              NullLogger.Instance))
            {
                // `machine` in the header is what lets the MASTER key open this — the
                // one-argument restore contract. An archive without it can only be read
                // by whoever already holds the derived key.
                BackupEnvelope.Write(pipe, sealedStream, key, new JsonObject
                {
                    ["kind"] = "machine_files",
                    ["machine"] = machine,
                });
            }
            if (fixtureDir is not null)
                File.WriteAllBytes(Path.Combine(fixtureDir, "from-agent-archive.fhb"),
                                   sealedStream.ToArray());

            sealedStream.Position = 0;
            using var plain = new MemoryStream();
            BackupEnvelope.Read(sealedStream, key, plain);
            plain.Position = 0;

            var names = new List<string>();
            using var tar = new TarReader(plain);
            while (tar.GetNextEntry() is { } entry) names.Add(entry.Name);

            // manifest.json first, so an archive can be listed without unpacking it.
            Assert.Equal("manifest.json", names[0]);
            Assert.Equal(4, names.Count);
            // Drive colons stripped and separators normalised, so the tar stays portable.
            Assert.All(names.Skip(1), n => Assert.DoesNotContain(":", n));
            Assert.All(names.Skip(1), n => Assert.DoesNotContain("\\", n));
        }
        finally
        {
            Directory.Delete(temp, recursive: true);
        }
    }

    [Fact]
    public void ArchiveSurvivesAFileVanishingBetweenSelectionAndPacking()
    {
        // A user deleting something mid-backup must not fail the whole run.
        var temp = Path.Combine(Path.GetTempPath(), "fh-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(temp);
        try
        {
            var real = Path.Combine(temp, "real.txt");
            File.WriteAllText(real, "still here");
            var files = new List<ManifestEntry>
            {
                new() { Path = real, Size = 10, Mtime = 1700, Sha256 = "aa" },
                new() { Path = Path.Combine(temp, "gone.txt"), Size = 5, Mtime = 1700, Sha256 = "bb" },
            };

            var key = new byte[32];
            using var sealedStream = new MemoryStream();
            using (var pipe = new TarGzipPipe(files, new VssSnapshot(NullLogger.Instance),
                                              NullLogger.Instance))
            {
                BackupEnvelope.Write(pipe, sealedStream, key, new JsonObject());
            }

            sealedStream.Position = 0;
            using var plain = new MemoryStream();
            BackupEnvelope.Read(sealedStream, key, plain);
            plain.Position = 0;

            var names = new List<string>();
            using var tar = new TarReader(plain);
            while (tar.GetNextEntry() is { } entry) names.Add(entry.Name);

            Assert.Contains(names, n => n.EndsWith("real.txt"));
            Assert.DoesNotContain(names, n => n.EndsWith("gone.txt"));
        }
        finally
        {
            Directory.Delete(temp, recursive: true);
        }
    }
}

/// <summary>A logger that does nothing, so these tests need no host.</summary>
internal sealed class NullLogger : Microsoft.Extensions.Logging.ILogger
{
    public static readonly NullLogger Instance = new();
    public IDisposable? BeginScope<TState>(TState state) where TState : notnull => null;
    public bool IsEnabled(Microsoft.Extensions.Logging.LogLevel logLevel) => false;
    public void Log<TState>(Microsoft.Extensions.Logging.LogLevel logLevel,
                            Microsoft.Extensions.Logging.EventId eventId, TState state,
                            Exception? exception, Func<TState, Exception?, string> formatter) { }
}
