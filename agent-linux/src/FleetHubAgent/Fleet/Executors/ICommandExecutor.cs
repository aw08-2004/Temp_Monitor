using System.Text.Json.Nodes;

namespace FleetHubAgent.Fleet.Executors;

/// <summary>One executable command type. The dispatcher routes a claimed command here by
/// Type; authorization already happened at the hub's console session gate.
///
/// **Type must match the hub's COMMAND_TYPE string exactly**, the same contract the Windows
/// agent's *Executor.cs classes hold -- a Linux agent that spelled a type differently would
/// have its commands routed nowhere and answer "unsupported" to a console that shows the
/// button.
///
/// <paramref name="onOutput"/> receives output lines as they are produced, and the Worker
/// now hands it an <see cref="OutputStreamer"/> that posts them to the hub while the command
/// is still running (roadmap #22) -- so a ten-minute script shows its progress in the console
/// rather than arriving all at once at the end. It stays NULLABLE because a caller that does
/// not care (a test, a future internal dispatch) must not have to invent a sink, and because
/// an executor that never produces intermediate output is a perfectly good executor.
///
/// **Whatever it is handed must never be blocked on or trusted to return quickly.** The
/// callback runs on the thread draining the child process's stdout pipe; a slow one there
/// stalls the child against a full pipe, which shows up as a command that timed out rather
/// than as anything to do with the hub.</summary>
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
