using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace TempMonitorAgent.Backup;

/// <summary>
/// The FHBK1 backup envelope: gzip, then AES-256-GCM in chunks. Roadmap #1b.
///
/// THIS IS THE SECOND IMPLEMENTATION OF THIS FORMAT. The first is backups.py on the hub,
/// which reads what this writes (and writes what restore_backup.py reads). Two crypto
/// implementations of one format drift apart silently, and the way you discover it is a
/// backup that will not restore — so `tests/fixtures/` holds a real artifact sealed by the
/// Python side that the agent tests decrypt, and the agent tests seal one that the Python
/// suite decrypts. Neither side can pass by being consistently wrong on its own.
///
/// If you change anything here, change backups.py in the same commit and regenerate the
/// fixture with tests/make_envelope_fixture.py. Do not "fix" one side alone.
///
/// Layout (all integers big-endian):
///
///   MAGIC                     6 bytes, "FHBK1\n"
///   header length             uint32
///   header                    UTF-8 JSON
///   repeated, until final:
///       ciphertext length     uint32
///       final flag            uint8, 1 on the last chunk only
///       ciphertext            AES-256-GCM output (plaintext + 16-byte tag)
///
/// Nonce is a 4-byte per-artifact random prefix ‖ 8-byte counter, so no nonce repeats
/// under one data key. The AAD binds sha256(header) ‖ counter ‖ final-flag, which is what
/// makes a tampered header, a reordered chunk and — the one that actually happens — a
/// TRUNCATED upload all fail closed rather than decrypting to plausible garbage.
/// </summary>
public static class BackupEnvelope
{
    private static readonly byte[] Magic = Encoding.ASCII.GetBytes("FHBK1\n");

    public const int Version = 1;
    public const int DefaultChunkBytes = 4 * 1024 * 1024;
    private const int TagBytes = 16;
    private const int NonceBytes = 12;

    /// <summary>Per-artifact data-key wrap. Must match backups.write_envelope byte for byte.</summary>
    private static readonly byte[] WrapAad = Encoding.ASCII.GetBytes("fleethub-backup-wrap");

    // HKDF-SHA256 with a fixed salt, one Expand block — 32 bytes needs exactly one.
    // Mirrors backups.derive_machine_key; the salt and info strings are part of the wire
    // contract, not an implementation detail.
    private static readonly byte[] HkdfSalt = Encoding.ASCII.GetBytes("fleethub-backup-hkdf-salt");
    private const string MachineInfoPrefix = "fleethub-backup-machine:";

    /// <summary>
    /// The key ONE machine's backups are sealed with, derived from the hub master key.
    /// The agent is normally HANDED this by the hub and never sees the master; this exists
    /// so the derivation can be tested against the Python side's fixture.
    /// </summary>
    public static byte[] DeriveMachineKey(byte[] masterKey, string machine)
    {
        var info = Encoding.UTF8.GetBytes(MachineInfoPrefix + (machine ?? "").Trim().ToLowerInvariant());
        using var extract = new HMACSHA256(HkdfSalt);
        var prk = extract.ComputeHash(masterKey);
        using var expand = new HMACSHA256(prk);
        var block = new byte[info.Length + 1];
        Buffer.BlockCopy(info, 0, block, 0, info.Length);
        block[^1] = 0x01;
        return expand.ComputeHash(block);
    }

    /// <summary>A short, non-reversible label for a key. Mirrors backups.key_id.</summary>
    public static string KeyId(byte[] key)
    {
        using var mac = new HMACSHA256(key);
        var digest = mac.ComputeHash(Encoding.ASCII.GetBytes("fleethub-backup-key-id"));
        return Convert.ToHexString(digest).ToLowerInvariant()[..16];
    }

    /// <summary>
    /// Seal <paramref name="plaintext"/> into <paramref name="destination"/>.
    ///
    /// The caller supplies ALREADY-COMPRESSED bytes (see BackupArchive, which pipes tar
    /// through GZipStream), matching the Python side where write_envelope takes an
    /// iterator of gzip output. Returns the total bytes written and their sha256 — the
    /// digest of the CIPHERTEXT, which is what the hub records and what an S3 PUT signs.
    /// </summary>
    public static (long Written, string Sha256) Write(
        Stream plaintext, Stream destination, byte[] key, JsonObject headerExtra,
        int chunkBytes = DefaultChunkBytes)
    {
        var dataKey = RandomNumberGenerator.GetBytes(32);
        var wrapNonce = RandomNumberGenerator.GetBytes(NonceBytes);
        var wrapped = new byte[dataKey.Length + TagBytes];
        using (var wrapAes = new AesGcm(key, TagBytes))
        {
            wrapAes.Encrypt(wrapNonce, dataKey, wrapped.AsSpan(0, dataKey.Length),
                            wrapped.AsSpan(dataKey.Length), WrapAad);
        }
        var noncePrefix = RandomNumberGenerator.GetBytes(4);

        var header = headerExtra is null ? new JsonObject() : (JsonObject)headerExtra.DeepClone();
        header["v"] = Version;
        header["cipher"] = "AES-256-GCM";
        header["compression"] = "gzip";
        header["chunk_bytes"] = chunkBytes;
        header["key_id"] = KeyId(key);
        header["wrap_nonce"] = Convert.ToBase64String(wrapNonce);
        header["wrapped_key"] = Convert.ToBase64String(wrapped);
        header["nonce_prefix"] = Convert.ToBase64String(noncePrefix);
        header["created_at"] = DateTimeOffset.UtcNow.ToUnixTimeSeconds();

        // The Python side writes json.dumps(header, sort_keys=True), and the AAD binds
        // sha256 of those exact bytes. The serialisation only has to be self-consistent —
        // the reader hashes the bytes it read, not a re-serialisation — but sorting keeps
        // the two sides producing comparable output when debugging.
        var headerBytes = SerializeSorted(header);
        var headerDigest = SHA256.HashData(headerBytes);

        using var digest = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        long written = 0;

        void Emit(ReadOnlySpan<byte> data)
        {
            destination.Write(data);
            digest.AppendData(data);
            written += data.Length;
        }

        Emit(Magic);
        Span<byte> lengthBuffer = stackalloc byte[4];
        BinaryPrimitives.WriteUInt32BigEndian(lengthBuffer, (uint)headerBytes.Length);
        Emit(lengthBuffer);
        Emit(headerBytes);

        using var aes = new AesGcm(dataKey, TagBytes);
        var buffer = new byte[chunkBytes];
        var pending = new byte[chunkBytes];
        int pendingLength = -1;
        ulong counter = 0;

        // One block of lookahead: the final flag is authenticated INSIDE the last chunk,
        // so a block can only be sealed once we know whether another follows.
        while (true)
        {
            int read = ReadFully(plaintext, buffer);
            if (read > 0)
            {
                if (pendingLength >= 0)
                {
                    Emit(Seal(aes, noncePrefix, counter, pending.AsSpan(0, pendingLength),
                              false, headerDigest));
                    counter++;
                }
                Buffer.BlockCopy(buffer, 0, pending, 0, read);
                pendingLength = read;
                continue;
            }
            Emit(Seal(aes, noncePrefix, counter,
                      pendingLength > 0 ? pending.AsSpan(0, pendingLength) : ReadOnlySpan<byte>.Empty,
                      true, headerDigest));
            break;
        }

        return (written, Convert.ToHexString(digest.GetHashAndReset()).ToLowerInvariant());
    }

    /// <summary>
    /// Open an artifact and write its decompressed contents to <paramref name="destination"/>.
    ///
    /// <paramref name="key"/> may be the master key or an already-derived machine key: if
    /// the header names a machine and the master does not match, the machine key is
    /// re-derived — the same one-argument contract restore_backup.py has.
    ///
    /// Throws InvalidDataException with an operator-readable message on anything that is
    /// not a decryptable FHBK1 file. Never returns partial output as success.
    /// </summary>
    public static JsonObject Read(Stream source, byte[] key, Stream destination)
    {
        var (header, chunks) = Open(source, key);
        using var gzip = new System.IO.Compression.GZipStream(chunks, System.IO.Compression.CompressionMode.Decompress);
        gzip.CopyTo(destination);
        return header;
    }

    /// <summary>Header plus a stream of the DECRYPTED (still gzip-framed) bytes.</summary>
    public static (JsonObject Header, Stream Plaintext) Open(Stream source, byte[] key)
    {
        var magic = new byte[Magic.Length];
        if (ReadFully(source, magic) != Magic.Length || !magic.AsSpan().SequenceEqual(Magic))
            throw new InvalidDataException("Not a FleetHub backup file (bad magic).");

        var lengthBuffer = new byte[4];
        if (ReadFully(source, lengthBuffer) != 4)
            throw new InvalidDataException("Truncated backup file (no header).");
        var headerLength = (int)BinaryPrimitives.ReadUInt32BigEndian(lengthBuffer);
        var headerBytes = new byte[headerLength];
        if (ReadFully(source, headerBytes) != headerLength)
            throw new InvalidDataException("Truncated backup file (short header).");

        JsonObject header;
        try
        {
            header = JsonNode.Parse(headerBytes)?.AsObject()
                     ?? throw new InvalidDataException("Corrupt backup header.");
        }
        catch (JsonException)
        {
            throw new InvalidDataException("Corrupt backup header.");
        }

        if (header["v"]?.GetValue<int>() != Version)
            throw new InvalidDataException($"Unsupported backup format version {header["v"]}.");

        var unwrapKey = key;
        var machine = header["machine"]?.GetValue<string>();
        var headerKeyId = header["key_id"]?.GetValue<string>();
        if (!string.IsNullOrEmpty(machine) && headerKeyId != KeyId(key))
            unwrapKey = DeriveMachineKey(key, machine);
        if (!string.IsNullOrEmpty(headerKeyId) && headerKeyId != KeyId(unwrapKey))
            throw new InvalidDataException("This backup was encrypted with a different master key.");

        byte[] dataKey;
        try
        {
            var wrapped = Convert.FromBase64String(header["wrapped_key"]!.GetValue<string>());
            var wrapNonce = Convert.FromBase64String(header["wrap_nonce"]!.GetValue<string>());
            dataKey = new byte[wrapped.Length - TagBytes];
            using var wrapAes = new AesGcm(unwrapKey, TagBytes);
            wrapAes.Decrypt(wrapNonce, wrapped.AsSpan(0, dataKey.Length),
                            wrapped.AsSpan(dataKey.Length), dataKey, WrapAad);
        }
        catch (Exception e) when (e is CryptographicException or FormatException or NullReferenceException)
        {
            throw new InvalidDataException("The master key does not decrypt this backup.");
        }

        var noncePrefix = Convert.FromBase64String(header["nonce_prefix"]!.GetValue<string>());
        return (header, new ChunkStream(source, dataKey, noncePrefix, SHA256.HashData(headerBytes)));
    }

    private static byte[] Seal(AesGcm aes, byte[] noncePrefix, ulong counter,
                               ReadOnlySpan<byte> plaintext, bool final, byte[] headerDigest)
    {
        Span<byte> nonce = stackalloc byte[NonceBytes];
        noncePrefix.CopyTo(nonce);
        BinaryPrimitives.WriteUInt64BigEndian(nonce[4..], counter);

        var aad = BuildAad(headerDigest, counter, final);
        var output = new byte[5 + plaintext.Length + TagBytes];
        BinaryPrimitives.WriteUInt32BigEndian(output.AsSpan(0, 4), (uint)(plaintext.Length + TagBytes));
        output[4] = (byte)(final ? 1 : 0);
        aes.Encrypt(nonce, plaintext, output.AsSpan(5, plaintext.Length),
                    output.AsSpan(5 + plaintext.Length), aad);
        return output;
    }

    /// <summary>
    /// sha256(header) ‖ counter ‖ final-flag. Python builds this with
    /// struct.pack("&gt;Q?", counter, final) — 8 big-endian bytes then one 0/1 byte, with
    /// NO padding, which is what `?` gives inside a `&gt;`-prefixed format.
    /// </summary>
    private static byte[] BuildAad(byte[] headerDigest, ulong counter, bool final)
    {
        var aad = new byte[headerDigest.Length + 9];
        Buffer.BlockCopy(headerDigest, 0, aad, 0, headerDigest.Length);
        BinaryPrimitives.WriteUInt64BigEndian(aad.AsSpan(headerDigest.Length, 8), counter);
        aad[^1] = (byte)(final ? 1 : 0);
        return aad;
    }

    /// <summary>Serialise with sorted keys, matching json.dumps(..., sort_keys=True)'s ordering.</summary>
    private static byte[] SerializeSorted(JsonObject header)
    {
        var sorted = new SortedDictionary<string, JsonNode?>(StringComparer.Ordinal);
        foreach (var kv in header) sorted[kv.Key] = kv.Value?.DeepClone();
        var rebuilt = new JsonObject();
        foreach (var kv in sorted) rebuilt[kv.Key] = kv.Value;
        return JsonSerializer.SerializeToUtf8Bytes(rebuilt);
    }

    private static int ReadFully(Stream stream, Span<byte> buffer)
    {
        int total = 0;
        while (total < buffer.Length)
        {
            int read = stream.Read(buffer[total..]);
            if (read == 0) break;
            total += read;
        }
        return total;
    }

    private static int ReadFully(Stream stream, byte[] buffer) => ReadFully(stream, buffer.AsSpan());

    /// <summary>
    /// Decrypts chunks on demand. A Stream rather than a byte[] because a machine backup
    /// is allowed to be bigger than RAM — the whole reason the format is chunked.
    ///
    /// Reaching the end of the source without ever seeing a chunk flagged `final` throws:
    /// that is a truncated upload, and it is the corruption most likely to actually occur.
    /// </summary>
    private sealed class ChunkStream : Stream
    {
        private readonly Stream _source;
        private readonly AesGcm _aes;
        private readonly byte[] _noncePrefix;
        private readonly byte[] _headerDigest;
        private byte[] _buffer = [];
        private int _offset;
        private ulong _counter;
        private bool _done;

        public ChunkStream(Stream source, byte[] dataKey, byte[] noncePrefix, byte[] headerDigest)
        {
            _source = source;
            _aes = new AesGcm(dataKey, TagBytes);
            _noncePrefix = noncePrefix;
            _headerDigest = headerDigest;
        }

        public override int Read(byte[] buffer, int offset, int count)
            => Read(buffer.AsSpan(offset, count));

        public override int Read(Span<byte> buffer)
        {
            while (_offset >= _buffer.Length)
            {
                if (_done) return 0;
                if (!NextChunk()) return 0;
            }
            int take = Math.Min(buffer.Length, _buffer.Length - _offset);
            _buffer.AsSpan(_offset, take).CopyTo(buffer);
            _offset += take;
            return take;
        }

        private bool NextChunk()
        {
            var framing = new byte[5];
            if (ReadFully(_source, framing) != 5)
                throw new InvalidDataException(
                    "Truncated backup file -- it has no final chunk, so the upload did not complete.");

            int length = (int)BinaryPrimitives.ReadUInt32BigEndian(framing.AsSpan(0, 4));
            bool final = framing[4] != 0;
            var ciphertext = new byte[length];
            if (ReadFully(_source, ciphertext) != length)
                throw new InvalidDataException("Truncated backup file (short chunk).");

            Span<byte> nonce = stackalloc byte[NonceBytes];
            _noncePrefix.CopyTo(nonce);
            BinaryPrimitives.WriteUInt64BigEndian(nonce[4..], _counter);

            var plaintext = new byte[length - TagBytes];
            try
            {
                _aes.Decrypt(nonce, ciphertext.AsSpan(0, plaintext.Length),
                             ciphertext.AsSpan(plaintext.Length), plaintext,
                             BuildAad(_headerDigest, _counter, final));
            }
            catch (CryptographicException)
            {
                throw new InvalidDataException(
                    $"Backup chunk {_counter} failed authentication -- the file is corrupt or was tampered with.");
            }

            _buffer = plaintext;
            _offset = 0;
            _counter++;
            _done = final;
            return plaintext.Length > 0 || !final;
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing) _aes.Dispose();
            base.Dispose(disposing);
        }

        public override bool CanRead => true;
        public override bool CanSeek => false;
        public override bool CanWrite => false;
        public override long Length => throw new NotSupportedException();
        public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
        public override void Flush() { }
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
    }
}
