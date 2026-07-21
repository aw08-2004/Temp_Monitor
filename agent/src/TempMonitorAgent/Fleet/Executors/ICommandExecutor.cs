using System.Text.Json.Nodes;

namespace TempMonitorAgent.Fleet.Executors;

/// <summary>One executable command type. The dispatcher routes a claimed command here
/// by Type; authorization already happened at the hub's console session gate.
///
/// <paramref name="onOutput"/> receives output lines as they are produced, for the
/// console's live terminal. Executors that finish fast or produce nothing useful mid-run
/// (restart, shutdown, rename) may ignore it; the full text is returned in CommandResult
/// either way, so streaming is an addition to the result, never a replacement.</summary>
public interface ICommandExecutor
{
    string Type { get; }
    Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct);
}

/// <summary>Helpers for pulling typed values out of a command's params object.</summary>
public static class ParamsExtensions
{
    public static string? GetString(this JsonNode? paramsNode, string key)
    {
        if (paramsNode is JsonObject obj && obj.TryGetPropertyValue(key, out var v) && v is not null)
        {
            try { return v.GetValue<string>(); }
            catch { return v.ToString(); }
        }
        return null;
    }

    public static int GetInt(this JsonNode? paramsNode, string key, int fallback)
    {
        if (paramsNode is JsonObject obj && obj.TryGetPropertyValue(key, out var v) && v is not null)
        {
            try { return v.GetValue<int>(); }
            catch
            {
                if (int.TryParse(v.ToString(), out var parsed)) return parsed;
            }
        }
        return fallback;
    }

    /// <summary>A nested object (deploy_package's `source` and `detection`), or null.</summary>
    public static JsonObject? GetObject(this JsonNode? paramsNode, string key)
    {
        if (paramsNode is JsonObject obj && obj.TryGetPropertyValue(key, out var v))
            return v as JsonObject;
        return null;
    }

    /// <summary>An array of ints (deploy_package's `success_exit_codes`).
    ///
    /// Returns an EMPTY set when the key is absent or unusable rather than a default like
    /// {0}. The caller decides what "no set" means -- guessing here would silently turn a
    /// malformed payload into "exit 0 is success", which is the one wrong answer that
    /// looks right.</summary>
    public static HashSet<int> GetIntSet(this JsonNode? paramsNode, string key)
    {
        var set = new HashSet<int>();
        if (paramsNode is JsonObject obj && obj.TryGetPropertyValue(key, out var v)
            && v is JsonArray arr)
        {
            foreach (var item in arr)
            {
                if (item is null) continue;
                try { set.Add(item.GetValue<int>()); }
                catch
                {
                    if (int.TryParse(item.ToString(), out var parsed)) set.Add(parsed);
                }
            }
        }
        return set;
    }
}
