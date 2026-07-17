using System.Text.Json.Nodes;

namespace TempMonitorAgent.Fleet.Executors;

/// <summary>One executable command type. The dispatcher routes a claimed command here
/// by Type; authorization already happened at the hub's console session gate.</summary>
public interface ICommandExecutor
{
    string Type { get; }
    Task<CommandResult> ExecuteAsync(FleetCommand cmd, CancellationToken ct);
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
}
