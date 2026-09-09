using System.Text.Json.Nodes;

namespace FleetHubAgent.Fleet.Executors;

/// <summary>One executable command type. The dispatcher routes a claimed command here by
/// Type; authorization already happened at the hub's console session gate.
///
/// **Type must match the hub's COMMAND_TYPE string exactly**, the same contract the Windows
/// and Linux agents' *Executor.cs classes hold -- an agent that spelled a type differently
/// would have its commands routed nowhere and answer "unsupported" to a console that shows
/// the button.
///
/// <paramref name="onOutput"/> receives output lines as they are produced. Nothing consumes
/// it yet: live streaming is gated in the console behind MIN_STREAMING_AGENT (3.1.0) and this
/// agent sits below it on purpose, so the full text in CommandResult is what an operator
/// sees. The parameter is threaded through anyway to keep this interface identical to the
/// other two agents', so an executor can be read across all three without a diff that is
/// really about a signature.</summary>
public interface ICommandExecutor
{
    string Type { get; }
    Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput, CancellationToken ct);
}

/// <summary>Helpers for pulling typed values out of a command's params object.
///
/// **None of these throw on a wrong type.** params is JSON from the hub, and a console that
/// sends a timeout as the string "600" rather than the number 600 should cost that one field
/// its value, not take down the executor and leave the command unanswered.</summary>
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

    public static bool GetBool(this JsonNode? paramsNode, string key, bool fallback = false)
    {
        if (paramsNode is JsonObject obj && obj.TryGetPropertyValue(key, out var v) && v is not null)
        {
            try { return v.GetValue<bool>(); }
            catch
            {
                if (bool.TryParse(v.ToString(), out var parsed)) return parsed;
            }
        }
        return fallback;
    }
}
