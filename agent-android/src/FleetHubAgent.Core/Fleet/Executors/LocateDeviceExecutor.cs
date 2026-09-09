using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;

namespace FleetHubAgent.Fleet.Executors;

/// <summary>
/// One position, on demand -- roadmap #23 phase B.
///
/// **"I could not tell you where I am" is a SUCCESS, not a failure**, and that is the whole
/// shape of this executor. A device indoors, with location switched off, or with the permission
/// never granted has answered the question truthfully; a `Fail` would be indistinguishable in
/// the console from a network problem or a crashed agent, and would send an operator looking at
/// the wrong thing while a phone sits in a drawer. The same line ShowMessageExecutor draws for
/// its `no_session` case: the command worked, the answer is "no".
///
/// A `Fail` here therefore means one thing only: the executor itself could not run. The hub
/// files that as `no_answer` and says so differently -- see hub/location.py's three statuses.
///
/// **The result is JSON in the command's OUTPUT**, not a new wire field. The result envelope
/// stays `{success, output}` exactly as every other command's does, so a hub too old to know
/// about locating simply stores a string it does not read, and nothing about the protocol has
/// to change for one feature.
///
/// **The reading itself is platform code behind <see cref="ILocationSource"/>.** Everything
/// about *policy* -- the time budget, what counts as a fix, what to say when there is not one,
/// the shape that goes on the wire -- lives here in Core where it is testable on a workstation.
/// The Android half only has to answer "here is a position, or here is why not".
/// </summary>
public sealed class LocateDeviceExecutor(
    ILogger<LocateDeviceExecutor> log,
    ILocationSource source,
    ILocationDisclosure disclosure) : ICommandExecutor
{
    /// <summary>Matches hub/location.py's COMMAND_TYPE.</summary>
    public string Type => "locate_device";

    /// <summary>Used when the hub sends no budget at all -- an older console, or a
    /// hand-rolled command. Matches location.default_timeout_seconds' default, so a device
    /// behaves the same whether or not the hub told it what to do.</summary>
    internal const int DefaultTimeoutSeconds = 45;

    /// <summary>Bounds whatever the hub asked for. The floor exists because a satellite fix
    /// from cold cannot happen in under a few seconds and a one-second budget would guarantee
    /// a stale answer; the ceiling because this holds a wake lock on a battery-powered device
    /// somebody is carrying, and no console setting should be able to make that ten minutes.
    /// </summary>
    internal const int MinTimeoutSeconds = 5;
    internal const int MaxTimeoutSeconds = 300;

    internal static int ResolveTimeout(JsonNode? parameters)
    {
        var asked = parameters.GetInt("timeout_seconds", DefaultTimeoutSeconds);
        return Math.Clamp(asked, MinTimeoutSeconds, MaxTimeoutSeconds);
    }

    public async Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var timeout = ResolveTimeout(cmd.Params);

        // **Before the fix is taken, not after, and not conditionally.** The person holding
        // the device is told they were located whether or not the attempt succeeds -- a
        // notification that only appeared on success would make a failed locate the quiet way
        // to check whether somebody's phone is switched on. It is also deliberately not
        // awaited on the success path below: posting it is the disclosure, and a device that
        // could not raise a notification (permission refused on Android 13+) must still
        // answer, or refusing the notification would become a way to refuse being found.
        disclosure.NotifyLocated(cmd.IssuedBy);

        LocationFix? fix;
        try
        {
            fix = await source.GetCurrentAsync(TimeSpan.FromSeconds(timeout), ct);
        }
        catch (System.OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // The agent is shutting down, not a location failure. Reported as a fail so the
            // hub files it as "no answer" -- which is true: nobody took a fix.
            throw;
        }
        catch (Exception e)
        {
            log.LogWarning(e, "Location provider threw");
            return CommandResult.Fail($"the device could not read its location: {e.Message}");
        }

        if (fix is null)
        {
            // No coordinates. Success, with the reason -- see the class docstring.
            log.LogInformation("Locate: no position available");
            return CommandResult.Ok(new JsonObject
            {
                ["error"] = source.UnavailableReason
                            ?? "no position was available within the time allowed",
            }.ToJsonString());
        }

        log.LogInformation("Locate: {Provider} fix, {Accuracy}m, stale={Stale}",
            fix.Provider, fix.AccuracyMetres, fix.Stale);
        return CommandResult.Ok(Describe(fix).ToJsonString());
    }

    /// <summary>The wire shape hub/location.py's clean_fix reads. Internal so a test can hold
    /// the two ends of that contract together -- a renamed key here is not a crash, it is a
    /// console that silently shows a device as never located.</summary>
    internal static JsonObject Describe(LocationFix fix) => new()
    {
        ["lat"] = fix.Latitude,
        ["lon"] = fix.Longitude,
        // Null rather than 0 when the platform did not say. Zero metres is a claim of perfect
        // precision, and the console draws an accuracy circle from this number.
        ["accuracy_m"] = fix.AccuracyMetres,
        ["provider"] = fix.Provider,
        ["fixed_at"] = fix.FixedAtUnix,
        // **The most important field in this object.** A last-known position two hours old
        // rendered as a current one is the single most misleading thing this feature could do:
        // somebody would drive to where a phone used to be. The reader sets it; the hub carries
        // it through; the console has to say so.
        ["stale"] = fix.Stale,
    };
}

/// <summary>One position, however the platform can get one.
///
/// Deliberately tiny, and deliberately in Core: everything about what a fix MEANS is the
/// executor's business, so the platform half cannot accidentally decide policy. Returning null
/// is the ordinary answer for a device that cannot see the sky.</summary>
public interface ILocationSource
{
    /// <summary>A position, or null if none was available within <paramref name="budget"/>.
    /// Must not throw for the ordinary "no fix" case.</summary>
    Task<LocationFix?> GetCurrentAsync(TimeSpan budget, CancellationToken ct);

    /// <summary>Why the last call returned null, phrased for an operator reading a command
    /// result. "Location is switched off on this device" and "no fix within 45 seconds" lead to
    /// completely different next steps, and a single "unavailable" would hide which one it
    /// was.</summary>
    string? UnavailableReason { get; }
}

/// <summary>Telling the person holding the device that they were located.
///
/// **An interface rather than a call into the notification manager, because this is a promise
/// rather than a detail.** The device shows who asked, every time, and putting that behind a
/// contract in Core is what makes it testable and what stops a later refactor quietly dropping
/// it as "just a notification".</summary>
public interface ILocationDisclosure
{
    /// <summary>Post a notification naming the operator who asked. Must not throw.</summary>
    void NotifyLocated(string? requestedBy);
}

/// <summary>What a platform location reader produces.</summary>
/// <param name="Latitude">Degrees north.</param>
/// <param name="Longitude">Degrees east.</param>
/// <param name="AccuracyMetres">Radius of confidence, or null if the platform did not say.</param>
/// <param name="Provider">Which provider produced it ("gps", "network", "fused"), so the
/// console never implies more precision than there was.</param>
/// <param name="FixedAtUnix">When the fix was taken, in epoch seconds -- NOT when it was
/// reported. The two differ by hours for a stale one, which is the whole point of carrying it.</param>
/// <param name="Stale">True when this is a last-known position rather than one just taken.</param>
public sealed record LocationFix(
    double Latitude,
    double Longitude,
    double? AccuracyMetres,
    string Provider,
    long FixedAtUnix,
    bool Stale);
