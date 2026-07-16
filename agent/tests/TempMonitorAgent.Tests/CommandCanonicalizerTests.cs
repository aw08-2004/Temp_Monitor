using System.Text;
using System.Text.Json.Nodes;
using TempMonitorAgent.Fleet;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Byte-exact parity with fleet.canonical_command_bytes. The expected HEX values were
/// produced by running the hub's own Python (see agent build notes) so these vectors
/// lock the single load-bearing interop surface: if the C# canonicalizer ever drifts
/// from Python's json.dumps(sort_keys, compact, ensure_ascii), signatures diverge and
/// these tests fail.
/// </summary>
public class CommandCanonicalizerTests
{
    private static string Hex(string type, string machine, string paramsJson)
    {
        var node = JsonNode.Parse(paramsJson);
        var bytes = CommandCanonicalizer.CanonicalBytes(type, machine, node);
        return Convert.ToHexString(bytes).ToLowerInvariant();
    }

    [Fact]
    public void SimpleScript_MatchesPython()
    {
        Assert.Equal(
            "7b226d616368696e65223a2250432d3031222c22706172616d73223a7b22736372697074223a226563686f206869227d2c2274797065223a2272756e5f736372697074227d",
            Hex("run_script", "PC-01", "{\"script\": \"echo hi\"}"));
    }

    [Fact]
    public void TwoStringParams_MatchesPython()
    {
        Assert.Equal(
            "7b226d616368696e65223a2250432d3031222c22706172616d73223a7b22736372697074223a2257726974652d486f737420277827222c227368656c6c223a22706f7765727368656c6c227d2c2274797065223a2272756e5f736372697074227d",
            Hex("run_script", "PC-01", "{\"script\": \"Write-Host 'x'\", \"shell\": \"powershell\"}"));
    }

    [Fact]
    public void MixedTypesWithNullBoolInt_MatchesPython()
    {
        // params: {version:"A12", count:3, flag:true, note:null} — keys must sort,
        // and int/bool/null must render as 3/true/null (no quotes).
        Assert.Equal(
            "7b226d616368696e65223a224445534b2d37222c22706172616d73223a7b22636f756e74223a332c22666c6167223a747275652c226e6f7465223a6e756c6c2c2276657273696f6e223a22413132227d2c2274797065223a227570646174655f62696f73227d",
            Hex("update_bios", "DESK-7", "{\"version\": \"A12\", \"count\": 3, \"flag\": true, \"note\": null}"));
    }

    [Fact]
    public void NonAscii_EscapedAsLowercaseUnicode()
    {
        // machine "lab-é中" and param "gpu-é" must become é / 中 (ensure_ascii).
        Assert.Equal(
            "7b226d616368696e65223a226c61622d5c75303065395c7534653264222c22706172616d73223a7b22706b67223a226770752d5c7530306539227d2c2274797065223a22696e7374616c6c5f647269766572227d",
            Hex("install_driver", "lab-é中", "{\"pkg\": \"gpu-é\"}"));
    }

    [Fact]
    public void NestedObjectAndArray_KeysSortedArrayPreserved()
    {
        // Nested object keys sort (a before b), array order preserved.
        Assert.Equal(
            "7b226d616368696e65223a2250432d3031222c22706172616d73223a7b226e6573746564223a7b2261223a5b332c312c325d2c2262223a327d2c227a223a22656e64227d2c2274797065223a2272756e5f736372697074227d",
            Hex("run_script", "PC-01", "{\"nested\": {\"b\": 2, \"a\": [3, 1, 2]}, \"z\": \"end\"}"));
    }

    [Fact]
    public void NullParams_DefaultsToEmptyObject()
    {
        var bytes = CommandCanonicalizer.CanonicalBytes("restart", "PC-01", null);
        Assert.Equal("{\"machine\":\"PC-01\",\"params\":{},\"type\":\"restart\"}",
            Encoding.UTF8.GetString(bytes));
    }
}
