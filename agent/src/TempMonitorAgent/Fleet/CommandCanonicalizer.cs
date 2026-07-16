using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace TempMonitorAgent.Fleet;

/// <summary>
/// Produces the exact bytes an offline signer signed, so the agent can re-verify a
/// command's Ed25519 signature. MUST reproduce Python's
///   json.dumps({"type":..,"machine":..,"params":..},
///              sort_keys=True, separators=(",",":"), ensure_ascii=True).encode("utf-8")
/// byte-for-byte (see fleet.canonical_command_bytes). The single load-bearing interop
/// risk in the whole agent — covered by golden-vector tests against the Python hub.
/// </summary>
public static class CommandCanonicalizer
{
    /// <summary>Canonical bytes over the three security-relevant fields.</summary>
    public static byte[] CanonicalBytes(string commandType, string machine, JsonNode? paramsNode)
    {
        var sb = new StringBuilder();
        // Keys sorted by Unicode code point: machine < params < type.
        sb.Append('{');
        AppendKey(sb, "machine");
        AppendString(sb, machine ?? "");   // str()-ified on the Python side
        sb.Append(',');
        AppendKey(sb, "params");
        // params defaults to {} when absent (mirrors `params if params is not None else {}`)
        WriteNode(sb, paramsNode);
        sb.Append(',');
        AppendKey(sb, "type");
        AppendString(sb, commandType ?? "");
        sb.Append('}');
        return Encoding.UTF8.GetBytes(sb.ToString());
    }

    private static void AppendKey(StringBuilder sb, string key)
    {
        AppendString(sb, key);
        sb.Append(':');
    }

    private static void WriteNode(StringBuilder sb, JsonNode? node)
    {
        switch (node)
        {
            case null:
                sb.Append("{}"); // top-level params default; nested nulls handled below
                return;
            case JsonObject obj:
                WriteObject(sb, obj);
                return;
            case JsonArray arr:
                WriteArray(sb, arr);
                return;
            case JsonValue val:
                WriteValue(sb, val);
                return;
            default:
                sb.Append("null");
                return;
        }
    }

    // Distinct from WriteNode: a genuine JSON null (object member / array element)
    // must emit "null", whereas an absent top-level params emits "{}".
    private static void WriteNodeOrNull(StringBuilder sb, JsonNode? node)
    {
        if (node is null) { sb.Append("null"); return; }
        WriteNode(sb, node);
    }

    private static void WriteObject(StringBuilder sb, JsonObject obj)
    {
        // Recursively sort keys by ordinal (UTF-16 code unit == code point for BMP).
        var keys = new List<string>(obj.Count);
        foreach (var kvp in obj) keys.Add(kvp.Key);
        keys.Sort(StringComparer.Ordinal);

        sb.Append('{');
        for (int i = 0; i < keys.Count; i++)
        {
            if (i > 0) sb.Append(',');
            AppendString(sb, keys[i]);
            sb.Append(':');
            WriteNodeOrNull(sb, obj[keys[i]]);
        }
        sb.Append('}');
    }

    private static void WriteArray(StringBuilder sb, JsonArray arr)
    {
        sb.Append('[');
        for (int i = 0; i < arr.Count; i++)
        {
            if (i > 0) sb.Append(',');
            WriteNodeOrNull(sb, arr[i]);
        }
        sb.Append(']');
    }

    private static void WriteValue(StringBuilder sb, JsonValue val)
    {
        // JsonValue from parsing is JsonElement-backed; use the element's kind.
        if (val.TryGetValue<JsonElement>(out var el))
        {
            switch (el.ValueKind)
            {
                case JsonValueKind.String:
                    AppendString(sb, el.GetString() ?? "");
                    return;
                case JsonValueKind.True:
                    sb.Append("true");
                    return;
                case JsonValueKind.False:
                    sb.Append("false");
                    return;
                case JsonValueKind.Null:
                    sb.Append("null");
                    return;
                case JsonValueKind.Number:
                    // The number text reaching us was produced by Python (hub/Flask),
                    // and the offline signer also uses Python json — so the raw token
                    // matches. Keep params string-valued to avoid float ambiguity.
                    sb.Append(el.GetRawText());
                    return;
            }
        }

        // Fallbacks for non-element-backed values.
        if (val.TryGetValue<string>(out var s)) { AppendString(sb, s); return; }
        if (val.TryGetValue<bool>(out var b)) { sb.Append(b ? "true" : "false"); return; }
        if (val.TryGetValue<long>(out var l)) { sb.Append(l.ToString(System.Globalization.CultureInfo.InvariantCulture)); return; }
        sb.Append(val.ToJsonString());
    }

    /// <summary>Append a JSON string escaped exactly as Python json with ensure_ascii=True:
    /// escape " and \, use short escapes for \b\t\n\f\r, and \uXXXX (lowercase) for any
    /// character below 0x20 or above 0x7e.</summary>
    private static void AppendString(StringBuilder sb, string s)
    {
        sb.Append('"');
        foreach (char c in s)
        {
            switch (c)
            {
                case '"': sb.Append("\\\""); break;
                case '\\': sb.Append("\\\\"); break;
                case '\b': sb.Append("\\b"); break;
                case '\t': sb.Append("\\t"); break;
                case '\n': sb.Append("\\n"); break;
                case '\f': sb.Append("\\f"); break;
                case '\r': sb.Append("\\r"); break;
                default:
                    if (c < 0x20 || c > 0x7e)
                        sb.Append("\\u").Append(((int)c).ToString("x4", System.Globalization.CultureInfo.InvariantCulture));
                    else
                        sb.Append(c);
                    break;
            }
        }
        sb.Append('"');
    }
}
