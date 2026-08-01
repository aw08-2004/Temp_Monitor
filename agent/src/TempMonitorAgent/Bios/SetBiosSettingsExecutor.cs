using System.Text.Json.Nodes;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Bios;

/// <summary>
/// Applies a firmware change the console asked for, then proves what actually happened
/// (roadmap #9).
///
/// Four steps, and the third is the feature:
///
///   1. **Fetch the payload.** The command carries only a change id -- the attribute list and
///      the BIOS setup password come from an authenticated hub endpoint. That is not a detour:
///      the hub audits command params verbatim, so a password carried in params would sit in
///      the audit log inside the database that is itself backed up. Same reason the restore
///      plan is fetched rather than dispatched.
///   2. **Write**, through whichever vendor interface this machine has (<see cref="BiosWriter"/>).
///   3. **Re-read, and report what the firmware now says.** A WMI method returning success
///      means the vendor accepted the write, which on firmware is a different claim from "the
///      setting is now that": some attributes apply live, some sit pending until POST, and
///      where that line falls is per vendor AND per setting. So the agent does not decide --
///      it reports the observed value and the hub compares (bios.classify_result). This is
///      also why "you should reboot" is never said here: only the comparison can know.
///   4. **Report**, including the full fresh inventory, so the console's Firmware tab is
///      current the moment the change resolves rather than showing pre-change values until
///      the next six-hourly scan.
///
/// The command's own result string is a human summary for the command log. The hub's record of
/// what happened comes from step 4, not from this string -- an executor that reported only
/// through its exit code is exactly the thing this design refuses.
/// </summary>
public sealed class SetBiosSettingsExecutor(FleetClient fleet) : ICommandExecutor
{
    public string Type => "set_bios_settings";

    public async Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        void Say(string line) => onOutput?.Invoke(line + Environment.NewLine);

        var changeId = cmd.Params.GetString("change_id") ?? "";
        if (changeId.Length == 0)
            return CommandResult.Fail("This command carries no change id.");

        var payload = await fleet.FetchBiosChangeAsync(changeId, ct);
        if (payload is null)
        {
            // The hub refuses a change that is no longer pending, which is what makes a
            // redelivered command safe: the second fetch is turned away rather than replaying
            // writes against firmware.
            return CommandResult.Fail(
                "The hub would not supply this change. It may have been cancelled, or already "
                + "sent to this machine.");
        }

        var requested = new List<(string Name, string Value)>();
        foreach (var node in payload["changes"] as JsonArray ?? [])
        {
            var name = node?["name"]?.GetValue<string>() ?? "";
            var value = node?["value"]?.GetValue<string>() ?? "";
            if (name.Length > 0) requested.Add((name, value));
        }
        if (requested.Count == 0)
            return await ReportAsync(changeId, [], "the change carried no settings", ct);

        var password = payload["password"]?.GetValue<string>();
        var manufacturer = BiosReader.Manufacturer();
        Say($"[bios] Applying {requested.Count} setting(s) via {manufacturer}'s firmware interface.");

        var outcomes = BiosWriter.Write(manufacturer, requested, password, BiosWriter.Invoke);
        foreach (var outcome in outcomes)
        {
            Say(outcome.Ok
                ? $"[bios] {outcome.Name} = {outcome.Value}: accepted"
                : $"[bios] {outcome.Name} = {outcome.Value}: {outcome.Error}");
        }

        // The re-read. Unconditional -- run even when every write failed, because a vendor
        // that refuses a write and applies it anyway is exactly the kind of thing this step
        // exists to catch, and because the hub wants the current inventory either way.
        Say("[bios] Re-reading the firmware to see what actually changed.");
        BiosReport after;
        try
        {
            after = BiosReader.Read(manufacturer, BiosReader.BiosVersion(), BiosReader.Query);
        }
        catch (Exception e)
        {
            // Reported as unknown per attribute rather than as failure: the writes may well
            // have landed, and calling them failed because the VERIFICATION failed would be a
            // second wrong answer on top of a missing one.
            return await ReportAsync(changeId, outcomes.Select(o =>
                (o.Name, o.Value, o.Error, (string?)null)).ToList(),
                $"the settings could not be read back: {BiosReader.Describe(e)}", ct);
        }

        var observedBy = after.Items.ToDictionary(a => a.Name, a => a.Value,
                                                  StringComparer.OrdinalIgnoreCase);
        var items = outcomes.Select(o => (
            o.Name, o.Value, o.Error,
            // Null when the attribute vanished from the inventory between write and re-read.
            // Distinct from empty, which is a firmware that really did report an empty value.
            observedBy.TryGetValue(o.Name, out var seen) ? seen : null)).ToList();

        // The refresher is invalidated so the NEXT heartbeat carries the new inventory even if
        // the report below never lands -- the two paths are independent on purpose.
        BiosInventoryReporter.Invalidate();

        var result = await ReportAsync(changeId, items, "", ct,
                                       BiosInventoryReporter.ToPayload(after));
        return result;
    }

    /// <summary>POST the outcome. The full inventory rides along when we have one, so the
    /// console's tab is current the instant the change resolves.</summary>
    private async Task<CommandResult> ReportAsync(
        string changeId,
        IReadOnlyList<(string Name, string Value, string Error, string? Observed)> items,
        string runError, CancellationToken ct, JsonObject? inventory = null)
    {
        var array = new JsonArray();
        foreach (var (name, _, error, observed) in items)
        {
            array.Add(new JsonObject
            {
                ["name"] = name,
                ["error"] = error,
                ["observed"] = observed is null ? null : JsonValue.Create(observed),
            });
        }
        var body = new JsonObject { ["items"] = array, ["error"] = runError };
        if (inventory is not null) body["bios"] = inventory;

        var delivered = await fleet.ReportBiosChangeAsync(changeId, body, ct);

        var applied = items.Count(i => i.Error.Length == 0);
        var summary = runError.Length > 0
            ? $"{applied} of {items.Count} setting(s) written; {runError}"
            : $"{applied} of {items.Count} setting(s) written. The hub verifies each against a "
              + "re-read before reporting it as applied.";
        if (!delivered)
        {
            // Said plainly rather than swallowed: the machine may well have changed, and an
            // operator looking at a change stuck on "in flight" needs to know the write
            // happened and only the REPORT was lost. The hub's stale-change sweep closes the
            // row an hour later as partial, which is the honest state.
            summary += " The result could not be reported to the hub.";
        }
        // Failure only when nothing was written at all. A partial is a success at the command
        // level and a `partial` at the change level -- the change row is where that nuance
        // belongs, and duplicating it as a red command row would double-count one event.
        return applied == 0 && items.Count > 0
            ? CommandResult.Fail(summary)
            : CommandResult.Ok(summary);
    }
}
