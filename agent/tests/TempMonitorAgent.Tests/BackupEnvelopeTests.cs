using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using TempMonitorAgent.Backup;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Cross-implementation tests for the FHBK1 envelope.
///
/// The point of this file is NOT that the C# envelope round-trips its own output — that
/// would pass just as happily if both halves were wrong in the same way. It is that this
/// implementation agrees with backups.py, which is the one that has to restore these
/// files when a machine is gone. So:
///
///   * it decrypts artifacts sealed by PYTHON (tests/fixtures/*.fhb), and
///   * it writes one to tests/fixtures/from-agent.fhb that tests/test_backups.py decrypts.
///
/// Both directions, because a format has a reader and a writer and either can drift.
///
/// Regenerate the Python-sealed fixtures with `python tests/make_envelope_fixture.py`,
/// and only when the format changes deliberately.
/// </summary>
public class BackupEnvelopeTests
{
    private sealed record Fixture(
        byte[] MasterKey, byte[] MachineKey, string Machine, byte[] Plaintext, string Dir);

    /// <summary>Walk up from the test binary to the repo root, then into tests/fixtures.</summary>
    private static Fixture LoadFixture()
    {
        var dir = AppContext.BaseDirectory;
        string? fixtures = null;
        for (var probe = new DirectoryInfo(dir); probe is not null; probe = probe.Parent)
        {
            var candidate = Path.Combine(probe.FullName, "tests", "fixtures", "envelope.json");
            if (File.Exists(candidate)) { fixtures = Path.GetDirectoryName(candidate); break; }
        }
        Assert.True(fixtures is not null,
            "tests/fixtures/envelope.json not found — run: python tests/make_envelope_fixture.py");

        var meta = JsonNode.Parse(File.ReadAllText(Path.Combine(fixtures!, "envelope.json")))!.AsObject();
        return new Fixture(
            Convert.FromBase64String(meta["master_key_b64"]!.GetValue<string>()),
            Convert.FromBase64String(meta["machine_key_b64"]!.GetValue<string>()),
            meta["machine"]!.GetValue<string>(),
            Convert.FromBase64String(meta["plaintext_b64"]!.GetValue<string>()),
            fixtures!);
    }

    [Fact]
    public void DerivesTheSameMachineKeyAsTheHub()
    {
        // If this drifts, an agent seals archives the hub cannot open — and nothing else
        // in the system notices until a restore.
        var fixture = LoadFixture();
        var derived = BackupEnvelope.DeriveMachineKey(fixture.MasterKey, fixture.Machine);
        Assert.Equal(fixture.MachineKey, derived);
    }

    [Fact]
    public void MachineKeyDerivationIsCaseInsensitiveAndPerMachine()
    {
        var fixture = LoadFixture();
        Assert.Equal(
            BackupEnvelope.DeriveMachineKey(fixture.MasterKey, "PC-1"),
            BackupEnvelope.DeriveMachineKey(fixture.MasterKey, "pc-1"));
        Assert.NotEqual(
            BackupEnvelope.DeriveMachineKey(fixture.MasterKey, "PC-1"),
            BackupEnvelope.DeriveMachineKey(fixture.MasterKey, "PC-2"));
        Assert.NotEqual(fixture.MasterKey,
            BackupEnvelope.DeriveMachineKey(fixture.MasterKey, "PC-1"));
    }

    [Fact]
    public void ReadsAHubArtifactSealedByPython()
    {
        var fixture = LoadFixture();
        using var source = File.OpenRead(Path.Combine(fixture.Dir, "envelope-hub.fhb"));
        using var output = new MemoryStream();
        var header = BackupEnvelope.Read(source, fixture.MasterKey, output);

        Assert.Equal(fixture.Plaintext, output.ToArray());
        Assert.Equal("hub_db", header["kind"]!.GetValue<string>());
        Assert.Equal("AES-256-GCM", header["cipher"]!.GetValue<string>());
    }

    [Fact]
    public void ReadsAMachineArtifactByDerivingFromTheMasterKey()
    {
        // The restore contract: one key opens everything, because the header says which
        // machine it was sealed for.
        var fixture = LoadFixture();
        using var source = File.OpenRead(Path.Combine(fixture.Dir, "envelope-machine.fhb"));
        using var output = new MemoryStream();
        var header = BackupEnvelope.Read(source, fixture.MasterKey, output);

        Assert.Equal(fixture.Plaintext, output.ToArray());
        Assert.Equal(fixture.Machine, header["machine"]!.GetValue<string>());
    }

    [Fact]
    public void ReadsAMachineArtifactWithTheDerivedKeyDirectly()
    {
        // How the agent itself opens one during a restore: it holds only its own key.
        var fixture = LoadFixture();
        using var source = File.OpenRead(Path.Combine(fixture.Dir, "envelope-machine.fhb"));
        using var output = new MemoryStream();
        BackupEnvelope.Read(source, fixture.MachineKey, output);
        Assert.Equal(fixture.Plaintext, output.ToArray());
    }

    [Fact]
    public void RefusesAnotherMachinesKey()
    {
        var fixture = LoadFixture();
        var other = BackupEnvelope.DeriveMachineKey(fixture.MasterKey, "SOMEONE-ELSE");
        using var source = File.OpenRead(Path.Combine(fixture.Dir, "envelope-machine.fhb"));
        using var output = new MemoryStream();
        var e = Assert.Throws<InvalidDataException>(
            () => BackupEnvelope.Read(source, other, output));
        Assert.Contains("different master key", e.Message);
    }

    [Fact]
    public void WritesAnArtifactPythonCanRead()
    {
        // The other direction. tests/test_backups.py picks this file up and decrypts it,
        // which is what proves the WRITER agrees, not just the reader.
        var fixture = LoadFixture();
        var payload = Encoding.UTF8.GetBytes(
            "sealed by the agent\n" + new string('x', 50_000) + "\ntail");

        using var compressed = new MemoryStream();
        using (var gzip = new GZipStream(compressed, CompressionLevel.Optimal, leaveOpen: true))
            gzip.Write(payload);
        compressed.Position = 0;

        var header = new JsonObject
        {
            ["kind"] = "machine_files",
            ["machine"] = fixture.Machine,
            ["written_by"] = "agent-tests",
        };
        var path = Path.Combine(fixture.Dir, "from-agent.fhb");
        using (var destination = File.Create(path))
        {
            var (written, sha) = BackupEnvelope.Write(
                compressed, destination, fixture.MachineKey, header, chunkBytes: 4096);
            Assert.True(written > 0);
            Assert.Equal(64, sha.Length);
        }

        // Round-trips here too, so a failure in the Python suite localises to the format
        // rather than to this file being unreadable by anyone.
        using var back = File.OpenRead(path);
        using var output = new MemoryStream();
        BackupEnvelope.Read(back, fixture.MasterKey, output);
        Assert.Equal(payload, output.ToArray());
    }

    [Fact]
    public void DetectsATruncatedUpload()
    {
        // The corruption that actually happens. A truncated artifact must fail loudly,
        // not decrypt to a short-but-plausible file.
        var fixture = LoadFixture();
        var full = File.ReadAllBytes(Path.Combine(fixture.Dir, "envelope-machine.fhb"));
        using var source = new MemoryStream(full[..(full.Length - 40)]);
        using var output = new MemoryStream();
        Assert.Throws<InvalidDataException>(
            () => BackupEnvelope.Read(source, fixture.MasterKey, output));
    }

    [Fact]
    public void DetectsAFlippedBit()
    {
        var fixture = LoadFixture();
        var bytes = File.ReadAllBytes(Path.Combine(fixture.Dir, "envelope-machine.fhb"));
        bytes[^20] ^= 0x01;
        using var source = new MemoryStream(bytes);
        using var output = new MemoryStream();
        Assert.Throws<InvalidDataException>(
            () => BackupEnvelope.Read(source, fixture.MasterKey, output));
    }

    [Fact]
    public void RejectsAFileThatIsNotAnEnvelope()
    {
        var fixture = LoadFixture();
        using var source = new MemoryStream(Encoding.ASCII.GetBytes("SQLite format 3\0not ours"));
        using var output = new MemoryStream();
        var e = Assert.Throws<InvalidDataException>(
            () => BackupEnvelope.Read(source, fixture.MasterKey, output));
        Assert.Contains("bad magic", e.Message);
    }

    [Fact]
    public void RoundTripsAnEmptyPayload()
    {
        var fixture = LoadFixture();
        using var empty = new MemoryStream();
        using (var gzip = new GZipStream(empty, CompressionLevel.Optimal, leaveOpen: true)) { }
        empty.Position = 0;

        using var sealedStream = new MemoryStream();
        BackupEnvelope.Write(empty, sealedStream, fixture.MachineKey, new JsonObject(),
                             chunkBytes: 4096);
        sealedStream.Position = 0;

        using var output = new MemoryStream();
        BackupEnvelope.Read(sealedStream, fixture.MachineKey, output);
        Assert.Empty(output.ToArray());
    }

    [Fact]
    public void KeyIdMatchesTheHubsLabel()
    {
        var fixture = LoadFixture();
        using var source = File.OpenRead(Path.Combine(fixture.Dir, "envelope-machine.fhb"));
        using var output = new MemoryStream();
        var header = BackupEnvelope.Read(source, fixture.MasterKey, output);
        Assert.Equal(BackupEnvelope.KeyId(fixture.MachineKey),
                     header["key_id"]!.GetValue<string>());
    }
}
