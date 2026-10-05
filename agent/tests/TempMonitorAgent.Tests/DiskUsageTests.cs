using System.Buffers.Binary;
using System.Security.Principal;
using System.Text;
using System.Text.Json.Nodes;
using TempMonitorAgent.DiskUsage;
using Xunit;
using Xunit.Abstractions;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The disk-usage scan (roadmap #27): MFT record parsing, the folder rollup, the change
/// summary and the wire payload.
///
/// **The silent failure this file exists for is a wrong number, not a crash.** An off-by-one
/// in an attribute offset, a missed fixup or an unsigned runlist delta does not throw; it
/// produces folder sizes that look plausible and are wrong on every PC in the fleet. So the
/// records here are built byte by byte from the on-disk layout, including the update
/// sequence protection, rather than from the parser's own idea of it.
///
/// **What this cannot cover** is a live volume: real fragmentation, a 4Kn disk, an MFT with
/// an attribute list, and how long a quarter of a million folders take. The one live test
/// below runs only in an elevated session, and is skipped (it passes vacuously) everywhere
/// else, including the Linux build.
/// </summary>
public class DiskUsageTests(ITestOutputHelper output)
{
    // ------------------------------------------------------------------ record builder

    private const int RecordSize = 1024;

    private sealed class RecordBuilder
    {
        private readonly List<byte[]> _attrs = [];
        public uint Number { get; init; }
        public ulong BaseRef { get; init; }
        public bool Directory { get; init; }
        public bool InUse { get; init; } = true;

        public RecordBuilder FileName(uint parent, string name, byte ns)
        {
            var value = new byte[66 + name.Length * 2];
            BinaryPrimitives.WriteUInt64LittleEndian(value, parent | (1UL << 48));
            value[64] = (byte)name.Length;
            value[65] = ns;
            Encoding.Unicode.GetBytes(name).CopyTo(value, 66);
            _attrs.Add(Resident(MftRecordParser.AttrFileName, value));
            return this;
        }

        public RecordBuilder ResidentData(int length, string name = "")
        {
            _attrs.Add(Resident(MftRecordParser.AttrData, new byte[length], name));
            return this;
        }

        public RecordBuilder NonResidentData(long size, long allocated, long startVcn = 0,
                                             long? compressed = null, byte[]? runs = null,
                                             string name = "", bool sparse = false)
        {
            var headerLength = compressed is null && !sparse ? 64 : 72;
            runs ??= [0x11, 0x01, 0x10, 0x00];
            var nameBytes = Encoding.Unicode.GetBytes(name);
            var runsOffset = Align8(headerLength + nameBytes.Length);
            var length = Align8(runsOffset + runs.Length);
            var a = new byte[length];
            BinaryPrimitives.WriteUInt32LittleEndian(a, MftRecordParser.AttrData);
            BinaryPrimitives.WriteUInt32LittleEndian(a.AsSpan(4), (uint)length);
            a[8] = 1;
            a[9] = (byte)name.Length;
            BinaryPrimitives.WriteUInt16LittleEndian(a.AsSpan(10), (ushort)headerLength);
            nameBytes.CopyTo(a, headerLength);
            if (sparse) BinaryPrimitives.WriteUInt16LittleEndian(a.AsSpan(12), 0x8000);
            if (compressed is not null) BinaryPrimitives.WriteUInt16LittleEndian(a.AsSpan(12), 0x0001);
            BinaryPrimitives.WriteInt64LittleEndian(a.AsSpan(16), startVcn);
            BinaryPrimitives.WriteUInt16LittleEndian(a.AsSpan(32), (ushort)runsOffset);
            BinaryPrimitives.WriteInt64LittleEndian(a.AsSpan(40), allocated);
            BinaryPrimitives.WriteInt64LittleEndian(a.AsSpan(48), size);
            BinaryPrimitives.WriteInt64LittleEndian(a.AsSpan(56), size);
            if (compressed is not null || sparse)
                BinaryPrimitives.WriteInt64LittleEndian(a.AsSpan(64), compressed ?? 0);
            runs.CopyTo(a, runsOffset);
            _attrs.Add(a);
            return this;
        }

        public RecordBuilder AttributeList()
        {
            _attrs.Add(Resident(MftRecordParser.AttrAttributeList, new byte[32]));
            return this;
        }

        /// <summary>The record as it sits on disk: with the update sequence applied, so the
        /// parser has to undo it.</summary>
        public byte[] Build(ushort usn = 0x0042)
        {
            var r = new byte[RecordSize];
            BinaryPrimitives.WriteUInt32LittleEndian(r, 0x454C4946);
            BinaryPrimitives.WriteUInt16LittleEndian(r.AsSpan(4), 48);         // USA offset
            BinaryPrimitives.WriteUInt16LittleEndian(r.AsSpan(6), 3);          // 1 + 2 strides
            BinaryPrimitives.WriteUInt16LittleEndian(r.AsSpan(20), 56);        // first attribute
            ushort flags = 0;
            if (InUse) flags |= MftRecordParser.FlagInUse;
            if (Directory) flags |= MftRecordParser.FlagDirectory;
            BinaryPrimitives.WriteUInt16LittleEndian(r.AsSpan(22), flags);
            BinaryPrimitives.WriteUInt32LittleEndian(r.AsSpan(28), RecordSize);
            BinaryPrimitives.WriteUInt64LittleEndian(r.AsSpan(32), BaseRef);
            BinaryPrimitives.WriteUInt32LittleEndian(r.AsSpan(44), Number);
            var at = 56;
            foreach (var a in _attrs) { a.CopyTo(r, at); at += a.Length; }
            BinaryPrimitives.WriteUInt32LittleEndian(r.AsSpan(at), MftRecordParser.AttrEnd);
            at += 8;
            BinaryPrimitives.WriteUInt32LittleEndian(r.AsSpan(24), (uint)at);  // bytes in use

            // Update sequence protection: stash each stride's last two bytes, stamp the USN.
            BinaryPrimitives.WriteUInt16LittleEndian(r.AsSpan(48), usn);
            for (var i = 1; i <= 2; i++)
            {
                var tail = i * 512 - 2;
                r[48 + i * 2] = r[tail];
                r[48 + i * 2 + 1] = r[tail + 1];
                BinaryPrimitives.WriteUInt16LittleEndian(r.AsSpan(tail), usn);
            }
            return r;
        }

        private static byte[] Resident(uint type, byte[] value, string name = "")
        {
            var nameBytes = Encoding.Unicode.GetBytes(name);
            var valueOffset = Align8(24 + nameBytes.Length);
            var length = Align8(valueOffset + value.Length);
            var a = new byte[length];
            BinaryPrimitives.WriteUInt32LittleEndian(a, type);
            BinaryPrimitives.WriteUInt32LittleEndian(a.AsSpan(4), (uint)length);
            a[9] = (byte)name.Length;
            BinaryPrimitives.WriteUInt16LittleEndian(a.AsSpan(10), 24);
            nameBytes.CopyTo(a, 24);
            BinaryPrimitives.WriteUInt32LittleEndian(a.AsSpan(16), (uint)value.Length);
            BinaryPrimitives.WriteUInt16LittleEndian(a.AsSpan(20), (ushort)valueOffset);
            value.CopyTo(a, valueOffset);
            return a;
        }

        private static int Align8(int n) => (n + 7) & ~7;
    }

    private static MftRecord ParseOnDisk(byte[] record, uint number)
    {
        Assert.True(MftRecordParser.ApplyFixups(record));
        var parsed = MftRecordParser.Parse(record, number);
        Assert.NotNull(parsed);
        return parsed!.Value;
    }

    // ------------------------------------------------------------------ parser

    /// <summary>The fixup is what makes a name that crosses byte 510 readable. Without it the
    /// two bytes there read as the sequence number, and the name is garbled.</summary>
    [Fact]
    public void FixupsRestoreTheBytesUnderTheSequenceNumber()
    {
        // A long name pushes the $FILE_NAME value across the first 512-byte stride.
        var longName = new string('x', 220);
        var raw = new RecordBuilder { Number = 40 }.FileName(5, longName, 1).Build();
        var parsed = ParseOnDisk(raw, 40);
        Assert.Equal(longName, parsed.Name);
    }

    [Fact]
    public void ATornRecordIsRefused()
    {
        var raw = new RecordBuilder { Number = 40 }.FileName(5, "a.txt", 1).Build();
        raw[1022] ^= 0xFF; // the second stride's tail no longer matches the sequence number
        Assert.False(MftRecordParser.ApplyFixups(raw));
    }

    [Fact]
    public void TheLongNameWinsOverTheShortOne()
    {
        var raw = new RecordBuilder { Number = 64, Directory = true }
            .FileName(5, "PROGRA~1", 2)
            .FileName(5, "Program Files", 1)
            .Build();
        var parsed = ParseOnDisk(raw, 64);
        Assert.Equal("Program Files", parsed.Name);
        Assert.Equal(5u, parsed.ParentRecord);
        Assert.True(parsed.IsDirectory);
    }

    [Fact]
    public void ResidentDataHasASizeAndTakesNoClusters()
    {
        var parsed = ParseOnDisk(new RecordBuilder { Number = 70 }
            .FileName(5, "tiny.txt", 3).ResidentData(300).Build(), 70);
        Assert.Equal(300, parsed.Size);
        Assert.Equal(0, parsed.Allocated);
    }

    /// <summary>An alternate data stream is not the file. Counting Zone.Identifier would
    /// make every downloaded file a few hundred bytes bigger than Explorer says.</summary>
    [Fact]
    public void ANamedStreamIsNotTheFileSize()
    {
        var parsed = ParseOnDisk(new RecordBuilder { Number = 71 }
            .FileName(5, "setup.exe", 1).ResidentData(26, "Zone.Identifier").Build(), 71);
        Assert.Equal(-1, parsed.Size);
    }

    [Fact]
    public void ACompressedStreamCountsItsCompressedSizeOnDisk()
    {
        var parsed = ParseOnDisk(new RecordBuilder { Number = 72 }
            .FileName(5, "log.txt", 1)
            .NonResidentData(size: 10_000_000, allocated: 10_027_008, compressed: 2_000_000)
            .Build(), 72);
        Assert.Equal(10_000_000, parsed.Size);
        Assert.Equal(2_000_000, parsed.Allocated);
    }

    /// <summary>
    /// A CompactOS (WOF) compressed file, as Windows writes C:\Windows and C:\Program Files: the
    /// unnamed stream is sparse with nothing allocated, and the bytes live in the named stream
    /// WofCompressedData. The first live run counted only the unnamed stream and missed 16 GB of
    /// a 100 GB drive. Size stays the unnamed stream's; on disk is where the bytes are.
    /// </summary>
    [Fact]
    public void ACompactOsFileCountsItsWofStreamOnDisk()
    {
        var parsed = ParseOnDisk(new RecordBuilder { Number = 74 }
            .FileName(5, "ntoskrnl.exe", 1)
            .NonResidentData(size: 12_000_000, allocated: 12_001_280, sparse: true)
            .NonResidentData(size: 5_000_000, allocated: 5_001_216, name: "WofCompressedData")
            .Build(), 74);
        Assert.Equal(12_000_000, parsed.Size);
        Assert.Equal(5_001_216, parsed.Allocated);
    }

    /// <summary>Only the VCN-0 copy carries sizes. Reading a later copy would double-count
    /// every fragmented file.</summary>
    [Fact]
    public void ALaterRunOfTheSameStreamCarriesNoSize()
    {
        var parsed = ParseOnDisk(new RecordBuilder { Number = 900, BaseRef = 72 | (1UL << 48) }
            .NonResidentData(size: 0, allocated: 0, startVcn: 4096).Build(), 900);
        Assert.Equal(-1, parsed.Size);
        Assert.Equal(72u, parsed.BaseRecord);
    }

    [Fact]
    public void AnExtensionRecordIsCreditedToItsBase()
    {
        var parsed = ParseOnDisk(new RecordBuilder { Number = 901, BaseRef = 73 | (2UL << 48) }
            .NonResidentData(size: 5_000_000_000, allocated: 5_000_003_584).Build(), 901);
        Assert.Equal(73u, parsed.BaseRecord);
        Assert.Equal(5_000_000_000, parsed.Size);
    }

    [Fact]
    public void ARecordNotInUseIsSkipped()
    {
        var raw = new RecordBuilder { Number = 80, InUse = false }.FileName(5, "gone", 1).Build();
        Assert.True(MftRecordParser.ApplyFixups(raw));
        Assert.Null(MftRecordParser.Parse(raw, 80));
    }

    /// <summary>The sign bug: an MFT extent at a LOWER cluster than the previous one is
    /// encoded with a negative delta.</summary>
    [Fact]
    public void RunListOffsetsAreSigned()
    {
        // run 1: 0x20 clusters at LCN 0x1000; run 2: 0x10 clusters at -0x800 (LCN 0x800);
        // run 3: sparse, 4 clusters.
        byte[] runs = [0x21, 0x20, 0x00, 0x10, 0x21, 0x10, 0x00, 0xF8, 0x01, 0x04, 0x00];
        var decoded = MftRecordParser.DecodeRunList(runs);
        Assert.Equal([(0x1000L, 0x20L), (0x800L, 0x10L), (-1L, 4L)], decoded);
    }

    [Fact]
    public void TheMftsOwnRunsAndListAreFound()
    {
        var raw = new RecordBuilder { Number = 0 }
            .FileName(5, "$MFT", 3)
            .AttributeList()
            .NonResidentData(size: 1 << 20, allocated: 1 << 20, runs: [0x21, 0x40, 0x00, 0x04, 0x00])
            .Build();
        Assert.True(MftRecordParser.ApplyFixups(raw));
        var (data, listValue, listRuns) = MftRecordParser.DataRunsOf(raw);
        Assert.Single(data);
        Assert.Equal(0, data[0].StartVcn);
        Assert.Equal([(0x400L, 0x40L)], data[0].Runs);
        Assert.NotNull(listValue);
        Assert.Null(listRuns);
    }

    // ------------------------------------------------------------------ rollup

    private static FolderTree BuildTree(Action<FolderTreeBuilder> add, string volume = "C:",
                                        DateTimeOffset? at = null)
    {
        var b = new FolderTreeBuilder(4096);
        b.Add(new MftRecord(5, 5, true, ".", 3, 5, -1, -1, false));
        add(b);
        return b.Build(volume, at ?? DateTimeOffset.FromUnixTimeSeconds(1_700_000_000),
                       1_000_000_000_000, 400_000_000_000);
    }

    private static MftRecord Dir(uint n, uint parent, string name) =>
        new(n, n, true, name, 1, parent, -1, -1, false);

    private static MftRecord File(uint n, uint parent, string name, long size, long alloc = -1) =>
        new(n, n, false, name, 1, parent, size, alloc < 0 ? size : alloc, false);

    [Fact]
    public void SizesRollUpToEveryAncestor()
    {
        var tree = BuildTree(b =>
        {
            b.Add(Dir(100, 5, "Users"));
            b.Add(Dir(101, 100, "Ann"));
            b.Add(Dir(102, 101, "Documents"));
            b.Add(File(200, 102, "a.docx", 1000, 4096));
            b.Add(File(201, 101, "b.txt", 10, 0));
            b.Add(File(202, 5, "pagefile.sys", 5000, 8192));
        });

        var ann = tree.Find(@"C:\Users\Ann");
        Assert.Equal(1010, tree.Size[ann]);
        Assert.Equal(4096, tree.Allocated[ann]);
        Assert.Equal(2, tree.Files[ann]);
        Assert.Equal(1, tree.SubDirs[ann]);
        Assert.Equal(6010, tree.Size[0]);
        Assert.Equal(3, tree.Files[0]);
        Assert.Equal(3, tree.SubDirs[0]);
        Assert.Equal(@"C:\Users\Ann\Documents", tree.PathOf(tree.Find(@"c:\users\ann\DOCUMENTS")));
    }

    /// <summary>An extension record read BEFORE its base must still land on the right file.</summary>
    [Fact]
    public void AnExtensionRecordBeforeItsBaseStillCounts()
    {
        var tree = BuildTree(b =>
        {
            b.Add(Dir(100, 5, "VMs"));
            b.Add(new MftRecord(50, 300, false, null, -1, 0, 7_000_000_000, 7_000_000_000, false));
            b.Add(new MftRecord(300, 300, false, "disk.vhdx", 1, 100, -1, -1, true));
        });
        var vms = tree.Find(@"C:\VMs");
        Assert.Equal(7_000_000_000, tree.Size[vms]);
        Assert.Contains(tree.FileName, n => n == "disk.vhdx");
    }

    [Fact]
    public void OrphansAndCyclesAreLeftOutOfTheTree()
    {
        var tree = BuildTree(b =>
        {
            b.Add(Dir(100, 5, "Real"));
            b.Add(Dir(110, 999, "Orphan"));        // parent record is not a directory
            b.Add(Dir(120, 121, "LoopA"));
            b.Add(Dir(121, 120, "LoopB"));
            b.Add(File(200, 110, "lost.bin", 1_000_000));
        });
        Assert.Equal(2, tree.Count); // root + Real
        Assert.Equal(-1, tree.Find(@"C:\Orphan"));
        Assert.Equal(0, tree.Size[0]);
    }

    [Fact]
    public void AHardLinkIsCountedOnce()
    {
        var tree = BuildTree(b =>
        {
            b.Add(Dir(100, 5, "A"));
            b.Add(Dir(101, 5, "B"));
            // One record, two long names. The first one read keeps the file.
            b.Add(new MftRecord(200, 200, false, "x.dll", 1, 100, 1000, 4096, false));
            // The second name, as an extension record carrying only a $FILE_NAME would add it.
            b.Add(new MftRecord(200, 200, false, "x.dll", 1, 101, -1, 0, false));
        });
        Assert.Equal(1000, tree.Size[0]);
        Assert.Equal(1000, tree.Size[tree.Find(@"C:\A")]);
        Assert.Equal(0, tree.Size[tree.Find(@"C:\B")]);
    }

    [Fact]
    public void ATreeSurvivesSaveAndLoad()
    {
        var tree = BuildTree(b =>
        {
            b.Add(Dir(100, 5, "Users"));
            b.Add(Dir(101, 100, "Ånn")); // non-ASCII on purpose
            b.Add(File(200, 101, "big.iso", 200_000_000));
        });
        var path = Path.Combine(Path.GetTempPath(), "fhdu-" + Guid.NewGuid().ToString("N"));
        try
        {
            tree.Save(path);
            var back = FolderTree.Load(path);
            Assert.NotNull(back);
            Assert.Equal(tree.Count, back!.Count);
            Assert.Equal(tree.ScannedAt, back.ScannedAt);
            Assert.Equal(200_000_000, back.Size[back.Find(@"C:\Users\Ånn")]);
            var f = Enumerable.Range(0, back.FileCount).Single(i => back.FileName[i] == "big.iso");
            Assert.Equal(@"C:\Users\Ånn\big.iso", back.FilePathOf(f));

            // The listing's load stops after the folders and still has every total.
            var folders = FolderTree.Load(path, includeFiles: false);
            Assert.NotNull(folders);
            Assert.False(folders!.HasFiles);
            Assert.Equal(0, folders.FileCount);
            Assert.Equal(200_000_000, folders.Size[0]);
        }
        finally { System.IO.File.Delete(path); }
    }

    /// <summary>Every file is in the tree, small ones too. The hub keeps history down to single
    /// files, and a tree that only named the big ones would leave it nothing to diff.</summary>
    [Fact]
    public void EveryFileIsNamedNotOnlyTheLargeOnes()
    {
        var tree = BuildTree(b =>
        {
            b.Add(Dir(100, 5, "Logs"));
            b.Add(File(200, 100, "tiny.log", 10));
            b.Add(File(201, 100, "zero.txt", 0));
        });
        Assert.Equal(2, tree.FileCount);
        Assert.Contains(@"C:\Logs\tiny.log", Enumerable.Range(0, tree.FileCount).Select(tree.FilePathOf));
    }

    [Fact]
    public void AGarbageFileLoadsAsNoTree()
    {
        var path = Path.Combine(Path.GetTempPath(), "fhdu-" + Guid.NewGuid().ToString("N"));
        try
        {
            System.IO.File.WriteAllBytes(path, [1, 2, 3, 4, 5]);
            Assert.Null(FolderTree.Load(path));
        }
        finally { System.IO.File.Delete(path); }
    }

    // ------------------------------------------------------------------ diff

    private const long Mb = 1024 * 1024;

    private static FolderTree Snapshot(long cacheMb, long downloadsMb, long oldMb, bool withNew)
    {
        return BuildTree(b =>
        {
            b.Add(Dir(100, 5, "Users"));
            b.Add(Dir(101, 100, "Ann"));
            b.Add(Dir(102, 101, "AppData"));
            b.Add(Dir(103, 102, "Cache"));
            b.Add(File(200, 103, "cache.db", cacheMb * Mb));
            b.Add(Dir(104, 101, "Downloads"));
            for (uint i = 0; i < 4; i++) b.Add(File(210 + i, 104, $"f{i}.zip", downloadsMb / 4 * Mb));
            b.Add(Dir(105, 5, "Old"));
            if (oldMb > 0) b.Add(File(220, 105, "old.bak", oldMb * Mb));
            if (withNew)
            {
                b.Add(Dir(106, 5, "Games"));
                b.Add(File(230, 106, "game.pak", 3000 * Mb));
            }
        });
    }

    /// <summary>The point of the diff: name the folder that actually grew, not its five
    /// ancestors that grew by the same amount.</summary>
    [Fact]
    public void TheDeepestExplainingFolderIsReportedNotItsAncestors()
    {
        var before = Snapshot(cacheMb: 1000, downloadsMb: 400, oldMb: 0, withNew: false);
        var after = Snapshot(cacheMb: 6000, downloadsMb: 400, oldMb: 0, withNew: false);
        var changes = FolderTreeDiff.Compare(before, after);
        var change = Assert.Single(changes);
        Assert.Equal(@"C:\Users\Ann\AppData\Cache", change.Path);
        Assert.Equal(5000 * Mb, change.Delta);
    }

    /// <summary>Growth spread over many files is reported at the folder holding them.</summary>
    [Fact]
    public void SpreadGrowthIsReportedAtTheFolder()
    {
        var before = Snapshot(1000, downloadsMb: 400, oldMb: 0, withNew: false);
        var after = Snapshot(1000, downloadsMb: 4000, oldMb: 0, withNew: false);
        var change = Assert.Single(FolderTreeDiff.Compare(before, after));
        Assert.Equal(@"C:\Users\Ann\Downloads", change.Path);
    }

    /// <summary>A gain in one place and a loss elsewhere net to nothing at the root, and
    /// both are still reported.</summary>
    [Fact]
    public void OffsettingChangesAreBothReported()
    {
        var before = Snapshot(1000, 400, oldMb: 3000, withNew: false);
        var after = Snapshot(1000, 400, oldMb: 0, withNew: true);
        var changes = FolderTreeDiff.Compare(before, after);
        Assert.Contains(changes, c => c.Path == @"C:\Games" && c.Delta == 3000 * Mb);
        Assert.Contains(changes, c => c.Path == @"C:\Old" && c.Delta == -3000 * Mb);
    }

    [Fact]
    public void NoPreviousScanMeansNoChanges()
    {
        Assert.Empty(FolderTreeDiff.Compare(null, Snapshot(1, 1, 0, false)));
        Assert.Empty(FolderTreeDiff.NewLargeFiles(null, Snapshot(1, 1, 0, false)));
    }

    [Fact]
    public void ANewLargeFileIsNamed()
    {
        var before = Snapshot(1000, 400, 0, withNew: false);
        var after = Snapshot(1000, 400, 0, withNew: true);
        var file = Assert.Single(FolderTreeDiff.NewLargeFiles(before, after));
        Assert.Equal(@"C:\Games\game.pak", file.Path);
    }

    // ------------------------------------------------------------------ payload

    /// <summary>The wire contract with hub/disk_usage.py. tests/test_disk_usage.py posts
    /// the same field names from the other side.</summary>
    [Fact]
    public void ThePayloadCarriesEveryFieldTheHubReads()
    {
        var before = Snapshot(1000, 400, 0, false);
        var after = Snapshot(6000, 400, 0, true);
        var payload = DiskUsageReporter.VolumePayload(before, after, 1234);

        foreach (var key in new[] { "volume", "fs", "scanned_at", "previous_scanned_at",
                                    "duration_ms", "total_bytes", "free_bytes", "used_bytes",
                                    "folders", "files", "changes", "new_large_files" })
            Assert.True(payload.ContainsKey(key), key);
        Assert.Equal("C:", payload["volume"]!.GetValue<string>());
        Assert.Equal(600_000_000_000, payload["used_bytes"]!.GetValue<long>());

        var change = payload["changes"]!.AsArray()[0]!.AsObject();
        Assert.True(change.ContainsKey("path") && change.ContainsKey("before") && change.ContainsKey("after"));
        // Full-depth history goes by its own upload; the summary no longer carries folders.
        Assert.False(payload.ContainsKey("tracked"));
    }

    // ------------------------------------------------------------------ history delta

    private static List<JsonObject> DeltaLines(FolderTree? before, FolderTree after)
    {
        var w = new StringWriter();
        TreeDelta.Write(before, after, w);
        return w.ToString().Split('\n', StringSplitOptions.RemoveEmptyEntries)
                .Select(l => JsonNode.Parse(l)!.AsObject()).ToList();
    }

    /// <summary>A full tree is a delta against nothing: every folder and file, parents first.</summary>
    [Fact]
    public void AFullTreeListsEveryEntryParentsFirst()
    {
        var tree = BuildTree(b =>
        {
            b.Add(Dir(100, 5, "Users"));
            b.Add(Dir(101, 100, "Ann"));
            b.Add(File(200, 101, "a.txt", 10));
            b.Add(File(201, 5, "root.sys", 5));
        });
        var lines = DeltaLines(null, tree);
        var paths = lines.Select(l => l["p"]!.GetValue<string>()).ToList();
        Assert.Equal(5, lines.Count);
        Assert.Equal(@"C:\", paths[0]);
        Assert.True(paths.IndexOf(@"C:\Users") < paths.IndexOf(@"C:\Users\Ann"));
        Assert.True(paths.IndexOf(@"C:\Users\Ann") < paths.IndexOf(@"C:\Users\Ann\a.txt"));
        var ann = lines.Single(l => l["p"]!.GetValue<string>() == @"C:\Users\Ann");
        Assert.Equal(1, ann["d"]!.GetValue<int>());
        Assert.Equal(1, ann["f"]!.GetValue<long>());
        var file = lines.Single(l => l["p"]!.GetValue<string>() == @"C:\Users\Ann\a.txt");
        Assert.Equal(0, file["d"]!.GetValue<int>());
        Assert.False(file.ContainsKey("f"));
    }

    [Fact]
    public void AnUnchangedTreeProducesNoLines()
    {
        var a = Snapshot(1000, 400, 0, false);
        var b = Snapshot(1000, 400, 0, false);
        Assert.Empty(DeltaLines(a, b));
    }

    /// <summary>A grown file is listed with every ancestor whose total moved, and nothing else.</summary>
    [Fact]
    public void AGrownFileListsItselfAndItsAncestors()
    {
        var before = Snapshot(1000, 400, 0, false);
        var after = Snapshot(6000, 400, 0, false);
        var paths = DeltaLines(before, after).Select(l => l["p"]!.GetValue<string>()).ToList();
        Assert.Equal(new[]
        {
            @"C:\", @"C:\Users", @"C:\Users\Ann", @"C:\Users\Ann\AppData",
            @"C:\Users\Ann\AppData\Cache", @"C:\Users\Ann\AppData\Cache\cache.db",
        }, paths);
    }

    /// <summary>A deleted folder lists itself and everything under it as gone, so the hub never
    /// has to infer a child's deletion from its parent's.</summary>
    [Fact]
    public void ADeletedFolderListsEverythingUnderItAsGone()
    {
        var before = Snapshot(1000, 400, 0, withNew: true);
        var after = Snapshot(1000, 400, 0, withNew: false);
        var gone = DeltaLines(before, after).Where(l => l.ContainsKey("x"))
                                            .Select(l => l["p"]!.GetValue<string>()).ToList();
        Assert.Equal(new[] { @"C:\Games", @"C:\Games\game.pak" }, gone);
    }

    [Fact]
    public void ARenamedFileIsOneGoneAndOneNew()
    {
        var before = BuildTree(b => { b.Add(Dir(100, 5, "D")); b.Add(File(200, 100, "old.txt", 10)); });
        var after = BuildTree(b => { b.Add(Dir(100, 5, "D")); b.Add(File(200, 100, "new.txt", 10)); });
        var lines = DeltaLines(before, after);
        Assert.Contains(lines, l => l["p"]!.GetValue<string>() == @"C:\D\old.txt" && l.ContainsKey("x"));
        Assert.Contains(lines, l => l["p"]!.GetValue<string>() == @"C:\D\new.txt" && !l.ContainsKey("x"));
        // Same size and count, so the folder itself did not change.
        Assert.DoesNotContain(lines, l => l["p"]!.GetValue<string>() == @"C:\D");
    }

    [Fact]
    public void ADeltaWrittenToDiskIsGzip()
    {
        var path = Path.Combine(Path.GetTempPath(), "fhdu-delta-" + Guid.NewGuid().ToString("N"));
        try
        {
            var count = TreeDelta.Write(null, Snapshot(1, 1, 0, false), path);
            using var gz = new System.IO.Compression.GZipStream(System.IO.File.OpenRead(path),
                System.IO.Compression.CompressionMode.Decompress);
            var text = new StreamReader(gz).ReadToEnd();
            Assert.Equal(count, text.Split('\n', StringSplitOptions.RemoveEmptyEntries).Length);
        }
        finally { System.IO.File.Delete(path); }
    }

    // ------------------------------------------------------------------ listing

    [Fact]
    public void AListingGetsTreeSizesForFoldersButNeverASize()
    {
        var tree = BuildTree(b =>
        {
            b.Add(Dir(100, 5, "Users"));
            b.Add(Dir(101, 100, "Ann"));
            b.Add(File(200, 101, "a.bin", 5000));
        }, volume: "Q:");
        var dir = Path.Combine(Path.GetTempPath(), "fhdu-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(dir);
        var original = FolderTreeCache.TreePath;
        try
        {
            tree.Save(Path.Combine(dir, "Q.cur"));
            FolderTreeCache.TreePath = letter => Path.Combine(dir, letter.TrimEnd(':') + ".cur");
            FolderTreeCache.Forget("Q:");

            var entries = new JsonArray
            {
                new JsonObject { ["name"] = "ann", ["directory"] = true, ["link"] = false },
                new JsonObject { ["name"] = "Ann-link", ["directory"] = true, ["link"] = true },
                new JsonObject { ["name"] = "notes.txt", ["directory"] = false, ["size"] = 12 },
            };
            var scannedAt = FolderTreeCache.Annotate(@"Q:\Users", entries);

            Assert.Equal(1_700_000_000, scannedAt);
            var ann = entries[0]!.AsObject();
            Assert.Equal(5000, ann["tree_size"]!.GetValue<long>());
            Assert.False(ann.ContainsKey("size"));
            Assert.False(entries[1]!.AsObject().ContainsKey("tree_size"));
            Assert.False(entries[2]!.AsObject().ContainsKey("tree_size"));
        }
        finally
        {
            FolderTreeCache.TreePath = original;
            FolderTreeCache.Forget("Q:");
            Directory.Delete(dir, recursive: true);
        }
    }

    [Fact]
    public void AnUnscannedVolumeAnnotatesNothing()
    {
        var original = FolderTreeCache.TreePath;
        try
        {
            FolderTreeCache.TreePath = _ => Path.Combine(Path.GetTempPath(), "missing-" + Guid.NewGuid());
            FolderTreeCache.Forget("R:");
            var entries = new JsonArray { new JsonObject { ["name"] = "x", ["directory"] = true } };
            Assert.Null(FolderTreeCache.Annotate(@"R:\", entries));
            Assert.False(entries[0]!.AsObject().ContainsKey("tree_size"));
        }
        finally
        {
            FolderTreeCache.TreePath = original;
            FolderTreeCache.Forget("R:");
        }
    }

    // ------------------------------------------------------------------ live volume

    /// <summary>
    /// The real thing, against the system drive. Runs only when the test process is elevated,
    /// because opening \\.\C: needs it. It checks the MFT reading against what Windows itself
    /// says: the folders it must find exist, and C:\Windows is gigabytes, not zero.
    /// </summary>
    [Fact]
    public void ReadsTheSystemDriveWhenElevated()
    {
        using var identity = WindowsIdentity.GetCurrent();
        if (!new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator))
        {
            output.WriteLine("SKIPPED: not elevated, the live MFT read did not run.");
            return;
        }

        var system = Path.GetPathRoot(Environment.SystemDirectory)!.TrimEnd('\\');
        var drive = new DriveInfo(system);
        var watch = System.Diagnostics.Stopwatch.StartNew();
        var tree = MftVolumeReader.Scan(system, drive.TotalSize, drive.TotalFreeSpace, CancellationToken.None);
        watch.Stop();

        // The numbers first contact needs, printed before the asserts so a failure still
        // reports them.
        using var self = System.Diagnostics.Process.GetCurrentProcess();
        var upload = Path.Combine(Path.GetTempPath(), "fhdu-live-" + Guid.NewGuid().ToString("N"));
        var lines = TreeDelta.Write(null, tree, upload);
        output.WriteLine($"ELEVATED live scan of {system}: {watch.ElapsedMilliseconds} ms, "
                         + $"{tree.Count:N0} folders, {tree.FileCount:N0} files, "
                         + $"peak working set {self.PeakWorkingSet64 / (1024 * 1024)} MB");
        output.WriteLine($"Full-tree upload: {lines:N0} lines, "
                         + $"{new FileInfo(upload).Length / 1024 / 1024.0:F1} MB gzip");
        output.WriteLine($"Accounted on disk {tree.Allocated[0] / 1e9:F1} GB of "
                         + $"{(drive.TotalSize - drive.TotalFreeSpace) / 1e9:F1} GB used");
        foreach (var sample in new[] { @"\Windows", @"\Program Files", @"\Users" })
        {
            var at = tree.Find(system + sample);
            if (at > 0)
                output.WriteLine($"  {system}{sample}: {tree.Size[at] / 1e9:F2} GB, "
                                 + $"{tree.Allocated[at] / 1e9:F2} GB on disk, {tree.Files[at]:N0} files");
        }
        System.IO.File.Delete(upload);

        Assert.True(tree.Count > 1000, $"only {tree.Count} folders");
        var windows = tree.Find(system + @"\Windows");
        Assert.True(windows > 0);
        Assert.True(tree.Allocated[windows] > 1L * 1024 * 1024 * 1024);
        Assert.True(tree.Find(system + @"\Windows\System32") > 0);
        // The allocation the tree accounts for cannot exceed what the volume says is used.
        Assert.True(tree.Allocated[0] <= drive.TotalSize - drive.TotalFreeSpace + 64 * Mb);
    }
}
