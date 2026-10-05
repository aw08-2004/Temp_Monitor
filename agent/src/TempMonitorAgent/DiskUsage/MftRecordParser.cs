using System.Buffers.Binary;
using System.Text;

namespace TempMonitorAgent.DiskUsage;

/// <summary>What one FILE record (or one extension record) contributes to the scan.</summary>
/// <param name="RecordNumber">This record's own number in the MFT.</param>
/// <param name="BaseRecord">The record this one belongs to: itself for a base record, the
/// base for an extension record. Every size and name below is credited to THIS number.</param>
/// <param name="IsDirectory">From the record header flags; only meaningful on a base record.</param>
/// <param name="Name">The best long name found in this record, or null.</param>
/// <param name="NameNamespace">The namespace of <paramref name="Name"/> (0 POSIX, 1 Win32,
/// 2 DOS, 3 Win32+DOS), used to prefer a long name over a short one across records.</param>
/// <param name="ParentRecord">The parent directory's record number from the chosen name.</param>
/// <param name="Size">Logical size of the unnamed $DATA stream, when this record holds its
/// first (VCN 0) instance; otherwise -1.</param>
/// <param name="Allocated">Bytes on disk of every $DATA stream whose first (VCN 0) instance is
/// in this record, named streams included. Additive across a file's records.</param>
/// <param name="HasAttributeList">True when the record carries an $ATTRIBUTE_LIST.</param>
internal readonly record struct MftRecord(
    uint RecordNumber, uint BaseRecord, bool IsDirectory, string? Name, int NameNamespace,
    uint ParentRecord, long Size, long Allocated, bool HasAttributeList);

/// <summary>
/// Parses raw NTFS FILE records into the few facts a folder-size scan needs (roadmap #27).
///
/// **Pure on purpose: bytes in, a record out.** Everything that touches a volume lives in
/// <see cref="MftVolumeReader"/>. The parser is where a wrong offset turns into a silently
/// wrong number on every PC in the fleet, so it is the half the tests drive directly with
/// hand-built records. A live volume cannot be put in a fixture.
///
/// **Extension records are credited to their base record, and that is how fragmented files
/// are counted.** A file with too many attributes for one 1 KB record (a heavily fragmented
/// VHDX, a file with hundreds of hard links) keeps an $ATTRIBUTE_LIST in its base record and
/// spills the rest into extension records, each carrying the base's number in its header.
/// Because the scan reads every record in the MFT anyway, attributing each one to
/// <c>BaseRecord</c> reaches every $DATA and $FILE_NAME the list would point at, without
/// following the list. Following it would need random reads into the MFT for no extra
/// information. The one place the list must be followed is the MFT's own $DATA, which is
/// needed BEFORE the sequential read can start. <see cref="MftVolumeReader"/> handles that.
///
/// **Only the VCN-0 instance of a non-resident $DATA carries sizes.** A data run split across
/// records repeats the attribute with a later starting VCN, and those copies hold zeros (or
/// stale values) in the size fields. Adding them would double-count. Reading only VCN 0 is
/// what the NTFS documentation specifies.
///
/// **Short (DOS 8.3) names lose to long ones.** A file usually has two $FILE_NAME attributes,
/// "PROGRA~1" and "Program Files". The folder tree must say the second, or the console shows
/// paths nobody recognises. A hard link has two Win32 names under two parents; the first one
/// read wins and the file is counted once, which is how WizTree and WinDirStat behave too.
/// </summary>
internal static class MftRecordParser
{
    internal const uint AttrStandardInformation = 0x10;
    internal const uint AttrAttributeList = 0x20;
    internal const uint AttrFileName = 0x30;
    internal const uint AttrData = 0x80;
    internal const uint AttrEnd = 0xFFFFFFFF;

    internal const ushort FlagInUse = 0x0001;
    internal const ushort FlagDirectory = 0x0002;

    private const ushort AttrFlagCompressedMask = 0x00FF;
    private const ushort AttrFlagSparse = 0x8000;

    /// <summary>NTFS protects every 512 bytes of a multi-sector structure with the update
    /// sequence, whatever the physical sector size. This is not BytesPerSector, and using
    /// that instead breaks 4Kn disks.</summary>
    internal const int FixupStride = 512;

    /// <summary>Record numbers are the low 48 bits of a file reference. The high 16 bits are
    /// the sequence number, which this scan does not need.</summary>
    internal static uint RecordOf(ulong fileReference) => (uint)(fileReference & 0x0000FFFFFFFFFFFFUL);

    /// <summary>
    /// Undo the update-sequence substitution in place. Returns false when the record is torn
    /// (a sector's tail does not match the sequence number), and a torn record must be skipped,
    /// not half-read.
    ///
    /// Before NTFS writes a record it saves the last two bytes of each 512-byte stride into the
    /// update sequence array and stamps the sequence number there instead. Reading a record
    /// without restoring them corrupts any attribute that crosses a stride boundary. That
    /// usually means a long file name, so the damage looks random.
    /// </summary>
    internal static bool ApplyFixups(Span<byte> record)
    {
        if (record.Length < 48) return false;
        int usaOffset = BinaryPrimitives.ReadUInt16LittleEndian(record[4..]);
        int usaCount = BinaryPrimitives.ReadUInt16LittleEndian(record[6..]);
        if (usaCount == 0) return true;
        if (usaOffset + usaCount * 2 > record.Length) return false;
        if ((usaCount - 1) * FixupStride > record.Length) return false;

        var usn = BinaryPrimitives.ReadUInt16LittleEndian(record[usaOffset..]);
        for (var i = 1; i < usaCount; i++)
        {
            var tail = i * FixupStride - 2;
            if (BinaryPrimitives.ReadUInt16LittleEndian(record[tail..]) != usn) return false;
            record.Slice(usaOffset + i * 2, 2).CopyTo(record.Slice(tail, 2));
        }
        return true;
    }

    /// <summary>
    /// Parse one record whose fixups are already applied. Returns null for a record that is
    /// not in use, has no FILE signature, or is malformed. A malformed record costs one entry,
    /// never the scan.
    /// </summary>
    internal static MftRecord? Parse(ReadOnlySpan<byte> record, uint recordNumber)
    {
        if (record.Length < 48) return null;
        if (BinaryPrimitives.ReadUInt32LittleEndian(record) != 0x454C4946) return null; // "FILE"
        var flags = BinaryPrimitives.ReadUInt16LittleEndian(record[22..]);
        if ((flags & FlagInUse) == 0) return null;

        var baseRef = BinaryPrimitives.ReadUInt64LittleEndian(record[32..]);
        var baseRecord = baseRef == 0 ? recordNumber : RecordOf(baseRef);
        int bytesInUse = (int)Math.Min(BinaryPrimitives.ReadUInt32LittleEndian(record[24..]),
                                       (uint)record.Length);
        int offset = BinaryPrimitives.ReadUInt16LittleEndian(record[20..]);

        string? name = null;
        var nameNs = -1;
        uint parent = 0;
        long size = -1, allocated = 0;
        var hasList = false;

        // Each attribute header is at least 16 bytes. The loop guard stops a corrupt length
        // field (zero, or one running past the record) from spinning or reading past the end.
        while (offset + 16 <= bytesInUse)
        {
            var attr = record[offset..bytesInUse];
            var type = BinaryPrimitives.ReadUInt32LittleEndian(attr);
            if (type == AttrEnd) break;
            var length = (int)BinaryPrimitives.ReadUInt32LittleEndian(attr[4..]);
            if (length < 16 || length > attr.Length) break;
            attr = attr[..length];

            var nonResident = attr[8] != 0;
            var nameLength = attr[9];

            switch (type)
            {
                case AttrAttributeList:
                    hasList = true;
                    break;

                case AttrFileName when !nonResident:
                {
                    var value = ResidentValue(attr);
                    if (value.Length < 66) break;
                    var charCount = value[64];
                    var ns = value[65];
                    if (66 + charCount * 2 > value.Length) break;
                    if (Better(ns, nameNs))
                    {
                        nameNs = ns;
                        parent = RecordOf(BinaryPrimitives.ReadUInt64LittleEndian(value));
                        name = Encoding.Unicode.GetString(value.Slice(66, charCount * 2));
                    }
                    break;
                }

                // SIZE is the unnamed stream only: that is Explorer's "Size", and counting a
                // Zone.Identifier would make every download a few bytes bigger than it says.
                //
                // ON DISK is every stream, named ones included. This was unnamed-only at first,
                // and the first live run on a real PC accounted for 83.9 of 99.9 GB used: CompactOS
                // (WOF) compression keeps a compressed file's bytes in a named stream,
                // WofCompressedData, and leaves the unnamed one sparse with almost nothing
                // allocated. C:\Windows read 21 GB of data and 9 GB on disk. The on-disk figure is
                // what fills a volume, so it must count where the bytes actually are.
                case AttrData:
                    if (!nonResident)
                    {
                        // Resident data lives inside the MFT record: it has a length but takes
                        // no clusters of its own.
                        if (nameLength == 0 && attr.Length >= 24)
                            size = BinaryPrimitives.ReadUInt32LittleEndian(attr[16..]);
                    }
                    else if (attr.Length >= 64
                             && BinaryPrimitives.ReadUInt64LittleEndian(attr[16..]) == 0) // VCN 0
                    {
                        var attrFlags = BinaryPrimitives.ReadUInt16LittleEndian(attr[12..]);
                        if (nameLength == 0)
                            size = (long)BinaryPrimitives.ReadUInt64LittleEndian(attr[48..]);
                        var onDisk = (long)BinaryPrimitives.ReadUInt64LittleEndian(attr[40..]);
                        // A compressed or sparse stream reserves its full allocation in the
                        // header, but only the compressed size is actually in use on disk.
                        if ((attrFlags & (AttrFlagCompressedMask | AttrFlagSparse)) != 0
                            && attr.Length >= 72)
                            onDisk = (long)BinaryPrimitives.ReadUInt64LittleEndian(attr[64..]);
                        allocated += Math.Max(0, onDisk);
                    }
                    break;
            }

            offset += length;
        }

        return new MftRecord(recordNumber, baseRecord, (flags & FlagDirectory) != 0, name, nameNs,
                             parent, size, allocated, hasList);
    }

    /// <summary>Is namespace <paramref name="candidate"/> a better display name than
    /// <paramref name="current"/>? Anything beats nothing; a DOS-only (2) name beats nothing
    /// else. Among the rest the first one seen is kept, which is the hard-link rule.</summary>
    internal static bool Better(int candidate, int current)
    {
        if (current < 0) return true;
        if (current == 2 && candidate != 2) return true;
        return false;
    }

    /// <summary>The value of a resident attribute, or empty when its header is inconsistent.</summary>
    internal static ReadOnlySpan<byte> ResidentValue(ReadOnlySpan<byte> attr)
    {
        if (attr.Length < 24) return ReadOnlySpan<byte>.Empty;
        var valueLength = (int)BinaryPrimitives.ReadUInt32LittleEndian(attr[16..]);
        int valueOffset = BinaryPrimitives.ReadUInt16LittleEndian(attr[20..]);
        if (valueLength < 0 || valueOffset + valueLength > attr.Length) return ReadOnlySpan<byte>.Empty;
        return attr.Slice(valueOffset, valueLength);
    }

    /// <summary>
    /// The unnamed $DATA instances of one record as (starting VCN, decoded runs), plus its
    /// $ATTRIBUTE_LIST. That is either the resident value or the runs of a non-resident one.
    ///
    /// Only <see cref="MftVolumeReader"/> needs this, for the $MFT's own record. Its data
    /// runs are the map of where every other record lives, so they must be known before the
    /// sequential pass can start, and on a badly fragmented volume part of that map sits in
    /// an extension record the list points at.
    /// </summary>
    internal static (List<(long StartVcn, List<(long Lcn, long Clusters)> Runs)> Data,
                     byte[]? ListValue, List<(long Lcn, long Clusters)>? ListRuns)
        DataRunsOf(ReadOnlySpan<byte> record)
    {
        var data = new List<(long, List<(long, long)>)>();
        byte[]? listValue = null;
        List<(long, long)>? listRuns = null;
        if (record.Length < 48) return (data, null, null);

        int bytesInUse = (int)Math.Min(BinaryPrimitives.ReadUInt32LittleEndian(record[24..]),
                                       (uint)record.Length);
        int offset = BinaryPrimitives.ReadUInt16LittleEndian(record[20..]);
        while (offset + 16 <= bytesInUse)
        {
            var attr = record[offset..bytesInUse];
            var type = BinaryPrimitives.ReadUInt32LittleEndian(attr);
            if (type == AttrEnd) break;
            var length = (int)BinaryPrimitives.ReadUInt32LittleEndian(attr[4..]);
            if (length < 16 || length > attr.Length) break;
            attr = attr[..length];
            var nonResident = attr[8] != 0;

            if (type == AttrData && attr[9] == 0 && nonResident && attr.Length >= 64)
            {
                var startVcn = (long)BinaryPrimitives.ReadUInt64LittleEndian(attr[16..]);
                int runsOffset = BinaryPrimitives.ReadUInt16LittleEndian(attr[32..]);
                if (runsOffset < attr.Length)
                    data.Add((startVcn, DecodeRunList(attr[runsOffset..])));
            }
            else if (type == AttrAttributeList)
            {
                if (!nonResident)
                {
                    listValue = ResidentValue(attr).ToArray();
                }
                else if (attr.Length >= 64)
                {
                    int runsOffset = BinaryPrimitives.ReadUInt16LittleEndian(attr[32..]);
                    if (runsOffset < attr.Length) listRuns = DecodeRunList(attr[runsOffset..]);
                }
            }
            offset += length;
        }
        return (data, listValue, listRuns);
    }

    /// <summary>The record numbers an $ATTRIBUTE_LIST names as holding part of the unnamed
    /// $DATA, other than <paramref name="self"/>. The entry layout is type (4), length (2),
    /// name length (1), name offset (1), starting VCN (8), file reference (8), id (2).</summary>
    internal static List<uint> DataRecordsInList(ReadOnlySpan<byte> list, uint self)
    {
        var records = new List<uint>();
        var i = 0;
        while (i + 26 <= list.Length)
        {
            var type = BinaryPrimitives.ReadUInt32LittleEndian(list[i..]);
            int length = BinaryPrimitives.ReadUInt16LittleEndian(list[(i + 4)..]);
            if (length < 26 || type == AttrEnd) break;
            if (type == AttrData && list[i + 6] == 0)
            {
                var rec = RecordOf(BinaryPrimitives.ReadUInt64LittleEndian(list[(i + 16)..]));
                if (rec != self && !records.Contains(rec)) records.Add(rec);
            }
            i += length;
        }
        return records;
    }

    /// <summary>
    /// Decode a non-resident attribute's mapping pairs into (LCN, cluster count) runs. A sparse
    /// run has LCN -1. Returns what decoded before the first malformed byte, so a partially
    /// readable runlist still yields its readable prefix.
    ///
    /// Each pair is a header byte (low nibble: bytes of length, high nibble: bytes of offset),
    /// an unsigned length, then a SIGNED offset relative to the previous run's LCN. The sign is
    /// the classic bug: an MFT that grew into a lower region of the disk has a negative offset,
    /// and reading it unsigned sends the scan to a cluster past the end of the volume.
    /// </summary>
    internal static List<(long Lcn, long Clusters)> DecodeRunList(ReadOnlySpan<byte> runs)
    {
        var result = new List<(long, long)>();
        long lcn = 0;
        var i = 0;
        while (i < runs.Length)
        {
            var header = runs[i];
            if (header == 0) break;
            int lengthBytes = header & 0x0F, offsetBytes = header >> 4;
            if (lengthBytes == 0 || lengthBytes > 8 || offsetBytes > 8) break;
            if (i + 1 + lengthBytes + offsetBytes > runs.Length) break;

            long clusters = 0;
            for (var b = 0; b < lengthBytes; b++)
                clusters |= (long)runs[i + 1 + b] << (8 * b);

            if (offsetBytes == 0)
            {
                result.Add((-1, clusters));
            }
            else
            {
                long delta = 0;
                for (var b = 0; b < offsetBytes; b++)
                    delta |= (long)runs[i + 1 + lengthBytes + b] << (8 * b);
                // Sign-extend from the top byte actually present.
                var shift = 64 - 8 * offsetBytes;
                delta = (delta << shift) >> shift;
                lcn += delta;
                result.Add((lcn, clusters));
            }
            i += 1 + lengthBytes + offsetBytes;
        }
        return result;
    }
}
