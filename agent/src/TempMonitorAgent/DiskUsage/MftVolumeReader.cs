using System.Buffers.Binary;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using Microsoft.Win32.SafeHandles;

namespace TempMonitorAgent.DiskUsage;

/// <summary>
/// Reads one NTFS volume's Master File Table straight off the disk and builds its folder
/// tree (roadmap #27).
///
/// **Why the MFT and not a directory walk.** A recursive walk of C: opens and enumerates
/// every folder on the machine: minutes of random I/O, thousands of access-denied
/// exceptions, and a scan that changes the last-access times it walks past. The MFT is one
/// file with a 1 KB record per file, laid out mostly contiguously, so reading it is a
/// sequential pass of a few hundred megabytes that takes seconds and touches nothing. It is
/// what WizTree and Everything do, and it needs exactly what this service already has:
/// LocalSystem, which may open <c>\\.\C:</c> for reading.
///
/// **Rejected: a Rust library.** The `ntfs` and `mft` crates are mature, but the agent ships
/// as one self-contained signed .NET executable. A native DLL embedded in it would need cargo
/// and an MSVC target on the release machine, and cargo-xwin in every Linux session that has
/// to compile the agent. The part of NTFS a size scan needs is small enough to own: record
/// fixups, two attribute types and the runlist encoding (see ROADMAP #27).
///
/// **Low I/O priority.** The volume handle is marked with the "low" I/O priority hint, and
/// the reporter runs the scan on a below-normal thread. On a busy PC the person at the
/// keyboard wins, and the scan takes longer.
///
/// **Read-only, end to end.** The handle is opened GENERIC_READ with full sharing. Nothing
/// here can write to the volume, and nothing locks it.
/// </summary>
[SupportedOSPlatform("windows")]
internal static class MftVolumeReader
{
    /// <summary>How much of the MFT one ReadFile call asks for.</summary>
    private const int ChunkBytes = 4 * 1024 * 1024;

    /// <summary>Scan <paramref name="volume"/> (e.g. "C:") and return its folder tree.</summary>
    public static FolderTree Scan(string volume, long totalBytes, long freeBytes, CancellationToken ct)
    {
        using var handle = CreateFileW(@"\\.\" + volume, GenericRead,
            FileShareRead | FileShareWrite | FileShareDelete, IntPtr.Zero, OpenExisting, 0, IntPtr.Zero);
        if (handle.IsInvalid) throw new Win32Exception(Marshal.GetLastWin32Error(), $"open {volume}");

        // Best effort: an older or unusual volume driver may refuse the hint, and the scan is
        // still correct without it.
        var hint = IoPriorityHintLow;
        SetFileInformationByHandle(handle, FileIoPriorityHintInfo, ref hint, sizeof(int));

        var info = new byte[96];
        if (!DeviceIoControl(handle, FsctlGetNtfsVolumeData, IntPtr.Zero, 0, info, info.Length,
                             out _, IntPtr.Zero))
            throw new Win32Exception(Marshal.GetLastWin32Error(), $"FSCTL_GET_NTFS_VOLUME_DATA {volume}");

        var bytesPerSector = (int)BinaryPrimitives.ReadUInt32LittleEndian(info.AsSpan(40));
        var bytesPerCluster = (int)BinaryPrimitives.ReadUInt32LittleEndian(info.AsSpan(44));
        var recordSize = (int)BinaryPrimitives.ReadUInt32LittleEndian(info.AsSpan(48));
        var mftValidBytes = BinaryPrimitives.ReadInt64LittleEndian(info.AsSpan(56));
        var mftStartLcn = BinaryPrimitives.ReadInt64LittleEndian(info.AsSpan(64));
        if (recordSize < 256 || bytesPerCluster < 512 || bytesPerSector < 512)
            throw new InvalidDataException($"implausible NTFS geometry on {volume}");

        var runs = MftRuns(handle, mftStartLcn, bytesPerCluster, bytesPerSector, recordSize,
                           mftValidBytes);
        var recordCount = (int)Math.Min(mftValidBytes / recordSize, int.MaxValue);
        var builder = new FolderTreeBuilder(recordCount);

        var buffer = new byte[ChunkBytes];
        long recordNumber = 0;
        foreach (var (lcn, clusters) in runs)
        {
            var runBytes = clusters * bytesPerCluster;
            if (lcn < 0)
            {
                // A sparse run inside the MFT should not exist; skip its records rather than
                // invent them.
                recordNumber += runBytes / recordSize;
                continue;
            }
            for (long done = 0; done < runBytes && recordNumber < recordCount; )
            {
                ct.ThrowIfCancellationRequested();
                var want = (int)Math.Min(ChunkBytes, runBytes - done);
                var got = ReadAt(handle, lcn * bytesPerCluster + done, buffer, want);
                if (got <= 0) break;
                for (var off = 0; off + recordSize <= got && recordNumber < recordCount; off += recordSize)
                {
                    var record = buffer.AsSpan(off, recordSize);
                    if (MftRecordParser.ApplyFixups(record))
                    {
                        var parsed = MftRecordParser.Parse(record, (uint)recordNumber);
                        if (parsed is not null) builder.Add(parsed.Value);
                    }
                    recordNumber++;
                }
                done += got;
            }
        }

        return builder.Build(volume.ToUpperInvariant(), DateTimeOffset.UtcNow, totalBytes, freeBytes);
    }

    /// <summary>
    /// The runs of the $MFT's own unnamed $DATA, in VCN order.
    ///
    /// Record 0 normally holds them all. On a heavily fragmented volume it holds the first
    /// part and an $ATTRIBUTE_LIST naming the extension records that hold the rest. Those
    /// extension records sit inside the part already mapped, so they can be read with what
    /// record 0 gave us. Without this a fragmented MFT would be read only up to its first
    /// gap, and every folder past that point would be missing.
    /// </summary>
    private static List<(long Lcn, long Clusters)> MftRuns(
        SafeFileHandle handle, long mftStartLcn, int bytesPerCluster, int bytesPerSector,
        int recordSize, long mftValidBytes)
    {
        var record0 = ReadRecord(handle, mftStartLcn * bytesPerCluster, recordSize, bytesPerSector);
        var (data, listValue, listRuns) = MftRecordParser.DataRunsOf(record0);
        var pieces = new List<(long StartVcn, List<(long Lcn, long Clusters)> Runs)>(data);

        long Mapped() => pieces.Sum(p => p.Runs.Sum(r => r.Clusters));
        var needed = (mftValidBytes + bytesPerCluster - 1) / bytesPerCluster;

        if (Mapped() < needed && (listValue is not null || listRuns is not null))
        {
            var list = listValue ?? ReadRuns(handle, listRuns!, bytesPerCluster);
            var known = Flatten(pieces);
            foreach (var rec in MftRecordParser.DataRecordsInList(list, 0))
            {
                var offset = OffsetOfRecord(known, rec, recordSize, bytesPerCluster);
                if (offset < 0) continue;
                var ext = ReadRecord(handle, offset, recordSize, bytesPerSector);
                pieces.AddRange(MftRecordParser.DataRunsOf(ext).Data);
            }
        }

        if (pieces.Count == 0) throw new InvalidDataException("the $MFT record has no data runs");
        return Flatten(pieces);
    }

    private static List<(long Lcn, long Clusters)> Flatten(
        List<(long StartVcn, List<(long Lcn, long Clusters)> Runs)> pieces) =>
        pieces.OrderBy(p => p.StartVcn).SelectMany(p => p.Runs).ToList();

    /// <summary>The byte offset on the volume of MFT record <paramref name="record"/>, or -1
    /// when the runs known so far do not reach it.</summary>
    private static long OffsetOfRecord(List<(long Lcn, long Clusters)> runs, uint record,
                                       int recordSize, int bytesPerCluster)
    {
        var byteInMft = (long)record * recordSize;
        foreach (var (lcn, clusters) in runs)
        {
            var runBytes = clusters * bytesPerCluster;
            if (byteInMft < runBytes) return lcn < 0 ? -1 : lcn * bytesPerCluster + byteInMft;
            byteInMft -= runBytes;
        }
        return -1;
    }

    private static byte[] ReadRecord(SafeFileHandle handle, long offset, int recordSize, int bytesPerSector)
    {
        // A volume handle reads whole sectors at sector-aligned offsets. Records are aligned,
        // but a 1 KB record on a 4Kn disk is smaller than a sector, so round the read up.
        var length = Math.Max(recordSize, bytesPerSector);
        var buffer = new byte[length];
        if (ReadAt(handle, offset, buffer, length) < recordSize)
            throw new InvalidDataException("short read of an MFT record");
        var record = buffer.AsSpan(0, recordSize).ToArray();
        if (!MftRecordParser.ApplyFixups(record))
            throw new InvalidDataException("torn MFT record");
        return record;
    }

    private static byte[] ReadRuns(SafeFileHandle handle, List<(long Lcn, long Clusters)> runs,
                                   int bytesPerCluster)
    {
        using var ms = new MemoryStream();
        foreach (var (lcn, clusters) in runs)
        {
            var bytes = (int)Math.Min(clusters * bytesPerCluster, 16 * 1024 * 1024);
            if (lcn < 0) { ms.Write(new byte[bytes]); continue; }
            var buffer = new byte[bytes];
            var got = ReadAt(handle, lcn * bytesPerCluster, buffer, bytes);
            ms.Write(buffer, 0, Math.Max(0, got));
        }
        return ms.ToArray();
    }

    private static int ReadAt(SafeFileHandle handle, long offset, byte[] buffer, int length)
    {
        if (!SetFilePointerEx(handle, offset, out _, 0))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "seek");
        if (!ReadFile(handle, buffer, length, out var read, IntPtr.Zero))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "read");
        return read;
    }

    // ------------------------------------------------------------------ interop

    private const uint GenericRead = 0x80000000;
    private const uint FileShareRead = 0x1, FileShareWrite = 0x2, FileShareDelete = 0x4;
    private const uint OpenExisting = 3;
    private const uint FsctlGetNtfsVolumeData = 0x00090064;
    private const int FileIoPriorityHintInfo = 12;
    private const int IoPriorityHintLow = 1;

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFileW(
        string name, uint access, uint share, IntPtr security, uint disposition, uint flags,
        IntPtr template);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool DeviceIoControl(
        SafeFileHandle device, uint code, IntPtr inBuffer, int inSize, byte[] outBuffer,
        int outSize, out int returned, IntPtr overlapped);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetFilePointerEx(
        SafeFileHandle file, long distance, out long newPosition, uint moveMethod);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool ReadFile(
        SafeFileHandle file, byte[] buffer, int toRead, out int read, IntPtr overlapped);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetFileInformationByHandle(
        SafeFileHandle file, int infoClass, ref int info, int size);
}
