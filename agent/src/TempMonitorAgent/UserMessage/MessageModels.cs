using System.Text.Json.Serialization;

namespace TempMonitorAgent.UserMessage;

/// <summary>
/// The wire shape between the hub, the service, and the session-injected helper that actually
/// shows the dialog.
///
/// One shape all the way through on purpose: the hub's command params deserialize straight
/// into <see cref="MessageRequest"/>, the service writes that same object to a file, and the
/// helper reads it back. A translation step in the middle would be a second place for the
/// button list to drift out of agreement with what the hub validated.
/// </summary>
public sealed class MessageRequest
{
    [JsonPropertyName("title")] public string Title { get; set; } = "";
    [JsonPropertyName("body")] public string Body { get; set; } = "";
    [JsonPropertyName("style")] public string Style { get; set; } = "dialog";
    [JsonPropertyName("buttons")] public List<MessageButton> Buttons { get; set; } = new();
    [JsonPropertyName("default_button")] public string? DefaultButton { get; set; }

    /// <summary>Seconds before the dialog closes itself, reporting <c>timeout</c>. Zero means
    /// it waits indefinitely -- which the hub bounds separately by the command's TTL, so an
    /// un-answered dialog cannot pin a session forever.</summary>
    [JsonPropertyName("timeout_seconds")] public int TimeoutSeconds { get; set; }

    /// <summary>Which Windows session to show it in. Null means "whichever session has a
    /// signed-in user", resolved at send time.</summary>
    [JsonPropertyName("target_session")] public uint? TargetSession { get; set; }
}

public sealed class MessageButton
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("label")] public string? Label { get; set; }
    [JsonPropertyName("style")] public string? Style { get; set; }
}

/// <summary>What the helper writes back, and what the service reports to the hub as the
/// command's output. The hub maps <see cref="Outcome"/> onto follow-up actions.</summary>
public sealed class MessageAnswer
{
    [JsonPropertyName("outcome")] public string Outcome { get; set; } = MessageOutcomes.Failed;
    [JsonPropertyName("shown_at")] public long ShownAt { get; set; }
    [JsonPropertyName("responded_at")] public long RespondedAt { get; set; }
    [JsonPropertyName("error")] public string? Error { get; set; }
}

/// <summary>The outcomes that are not button presses.
///
/// These are values, not an enum, because they cross a process boundary and a JSON file, and
/// because the hub's routing table is keyed on exactly these strings (rules.py's
/// NON_BUTTON_OUTCOMES). Keep the two lists in step.</summary>
public static class MessageOutcomes
{
    public const string Ok = "ok";
    /// <summary>The countdown ran out with nobody answering.</summary>
    public const string Timeout = "timeout";
    /// <summary>Esc, Alt+F4, or the close box -- the user actively declined to engage.</summary>
    public const string Dismissed = "dismissed";
    /// <summary>Nobody was signed in, so there was no desktop to show it on. Reported as a
    /// SUCCESSFUL command: a message to a logged-out PC is not an error, and calling it one
    /// would make every rule's history look broken and strand this outcome's route.</summary>
    public const string NoSession = "no_session";
    /// <summary>The dialog could not be shown at all.</summary>
    public const string Failed = "failed";
}
