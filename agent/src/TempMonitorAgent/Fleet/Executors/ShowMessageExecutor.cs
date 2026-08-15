using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Remote;
using TempMonitorAgent.UserMessage;

namespace TempMonitorAgent.Fleet.Executors;

/// <summary>
/// show_message: put a dialog on the signed-in user's desktop and report which button they
/// pressed.
///
/// The only command in the catalog whose subject is a PERSON rather than the machine, which
/// is what makes its result shape different from every other executor here: the output is a
/// JSON object the hub parses (<c>{"outcome": "...", "shown_at": ..., "responded_at": ...}</c>)
/// rather than prose for an operator to read. The hub maps that outcome onto follow-up
/// actions -- see rules.py's on_response routing.
///
/// Three deliberate choices worth keeping:
///
///  * <b>Nobody signed in succeeds.</b> It reports <c>no_session</c> with Success=true, not a
///    failure. A message sent to a logged-out PC is not an error, and reporting it as one
///    would both make every rule's history look broken and strand the hub's <c>no_session</c>
///    route, which is how an operator says "then ask again in half an hour".
///  * <b>The agent never interprets a button.</b> It ships ids and labels down and reports an
///    id back. What "Later" MEANS lives in the hub, so changing it does not require building,
///    signing and rolling out a new agent.
///  * <b>The wait is bounded by the dialog's own timeout plus slack</b>, never by the
///    cancellation token alone. A dialog with no timeout could otherwise hold this executor
///    open for the life of the service.
/// </summary>
public sealed class ShowMessageExecutor : ICommandExecutor
{
    private readonly ILogger<ShowMessageExecutor> _log;

    public ShowMessageExecutor(ILogger<ShowMessageExecutor> log) => _log = log;

    public string Type => "show_message";

    /// <summary>How long to wait past the dialog's own timeout before giving up on the helper.
    /// Covers process start, WinForms init and the answer file being written.</summary>
    private const int HelperSlackSeconds = 60;

    /// <summary>Ceiling on the wait when the message has no timeout of its own. The hub bounds
    /// this too, via the command TTL it stretches to cover the dialog -- this is the agent-side
    /// half of the same promise, so a hub that forgot cannot pin a helper here forever.</summary>
    private const int MaxWaitSeconds = 12 * 3600;

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                                  CancellationToken ct)
    {
        MessageRequest request;
        try
        {
            request = ParseRequest(cmd.Params);
        }
        catch (Exception ex)
        {
            return CommandResult.Fail($"bad show_message parameters: {ex.Message}");
        }

        // Resolve the session first so "nobody is here" is answered without paying for a
        // process launch, and so the answer is the same one SessionInjector would reach.
        uint session = request.TargetSession ?? SessionInjector.AutoSelectSession();
        if (session == SessionInjector.NoActiveSession || !HasSignedInUser(session))
        {
            _log.LogInformation("show_message: no signed-in session, reporting no_session");
            return Answer(new MessageAnswer
            {
                Outcome = MessageOutcomes.NoSession,
                ShownAt = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
                RespondedAt = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
            });
        }

        Directory.CreateDirectory(AgentConfig.MessageStateDir);
        string requestPath = Path.Combine(AgentConfig.MessageStateDir,
                                          $"{Guid.NewGuid():N}.json");
        string answerPath = MessageHelper.AnswerPathFor(requestPath);

        try
        {
            await File.WriteAllTextAsync(requestPath, JsonSerializer.Serialize(request), ct);

            string exe = Environment.ProcessPath
                         ?? Process.GetCurrentProcess().MainModule?.FileName
                         ?? throw new InvalidOperationException("cannot locate the agent binary");

            var injection = SessionInjector.Launch(
                exe, $"{AgentConfig.ShowMessageArg} \"{requestPath}\"", session);
            if (!injection.Ok)
            {
                // Could not get into the session at all. Reported as a SUCCESSFUL command
                // carrying outcome=failed, so the hub's `failed` route can act on it; a
                // command-level failure would be indistinguishable from a network problem.
                _log.LogWarning("show_message: injection failed: {Error}", injection.Error);
                return Answer(new MessageAnswer
                {
                    Outcome = MessageOutcomes.Failed,
                    Error = injection.Error,
                    ShownAt = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
                    RespondedAt = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
                });
            }

            var answer = await WaitForAnswerAsync(answerPath, request.TimeoutSeconds, ct);
            _log.LogInformation("show_message: outcome {Outcome} in session {Session}",
                                answer.Outcome, session);
            return Answer(answer);
        }
        finally
        {
            TryDelete(requestPath);
            TryDelete(answerPath);
        }
    }

    /// <summary>Poll for the helper's answer file.
    ///
    /// Polling rather than waiting on the process handle: SessionInjector deliberately does
    /// not hand back a waitable handle (it closes both, because the remote helper long
    /// outlives the command that started it), and the answer file is the real contract
    /// anyway -- a helper that crashed after writing it still answered.</summary>
    private static async Task<MessageAnswer> WaitForAnswerAsync(string answerPath,
                                                                int timeoutSeconds,
                                                                CancellationToken ct)
    {
        int budget = timeoutSeconds > 0
            ? timeoutSeconds + HelperSlackSeconds
            : MaxWaitSeconds;
        var deadline = DateTime.UtcNow.AddSeconds(budget);

        while (DateTime.UtcNow < deadline)
        {
            if (ct.IsCancellationRequested)
                return new MessageAnswer { Outcome = MessageOutcomes.Failed,
                                           Error = "the agent was stopping" };
            if (File.Exists(answerPath))
            {
                try
                {
                    var text = await File.ReadAllTextAsync(answerPath, ct);
                    var answer = JsonSerializer.Deserialize<MessageAnswer>(text);
                    if (answer is not null) return answer;
                }
                catch (IOException)
                {
                    // Caught mid-write; come back round rather than calling it a failure.
                }
            }
            await Task.Delay(500, ct);
        }
        // The helper never answered -- it was killed, or the session ended under it. That is
        // a timeout from the hub's point of view: nobody responded.
        return new MessageAnswer
        {
            Outcome = MessageOutcomes.Timeout,
            Error = "the message helper did not report back",
            RespondedAt = DateTimeOffset.UtcNow.ToUnixTimeSeconds(),
        };
    }

    /// <summary>Is there an actual person signed in to this session?
    ///
    /// The logon screen is a session with a window station and no human, so a dialog shown
    /// there waits for somebody who has not arrived yet -- and would greet them with a stale
    /// prompt about a condition from hours ago. SessionProbe already reports this, and the
    /// hub already knows it too (session.count), but the agent re-checks because the two can
    /// be seconds apart and this one is authoritative.</summary>
    private static bool HasSignedInUser(uint session)
    {
        foreach (var s in SessionProbe.Enumerate())
        {
            if (s.SessionId != session) continue;
            return !s.IsLogonScreen && !string.IsNullOrWhiteSpace(s.User);
        }
        return false;
    }

    private static CommandResult Answer(MessageAnswer answer) =>
        CommandResult.Ok(JsonSerializer.Serialize(answer));

    private static MessageRequest ParseRequest(JsonNode? node)
    {
        var request = node is null
            ? new MessageRequest()
            : JsonSerializer.Deserialize<MessageRequest>(node.ToJsonString()) ?? new MessageRequest();
        if (string.IsNullOrWhiteSpace(request.Title)) request.Title = "Message";
        if (string.IsNullOrWhiteSpace(request.Body))
            throw new InvalidOperationException("a message needs a body");
        if (request.Buttons.Count == 0)
            request.Buttons.Add(new MessageButton { Id = MessageOutcomes.Ok, Label = "OK" });
        foreach (var button in request.Buttons)
        {
            if (string.IsNullOrWhiteSpace(button.Id))
                throw new InvalidOperationException("every button needs an id");
            if (string.IsNullOrWhiteSpace(button.Label))
                button.Label = Capitalise(button.Id);
        }
        return request;
    }

    /// <summary>A readable label for a button the hub sent without one. The hub normally
    /// supplies localised labels; this is the floor, not the intent.</summary>
    private static string Capitalise(string id) =>
        id.Length == 0 ? id : char.ToUpperInvariant(id[0]) + id[1..];

    private static void TryDelete(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); } catch { }
    }
}
