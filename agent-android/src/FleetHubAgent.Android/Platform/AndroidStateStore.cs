using Android.Content;
using FleetHubAgent.State;

namespace FleetHubAgent.Android.Platform;

/// <summary>
/// The <see cref="IStateStore"/> backed by the app's private SharedPreferences.
///
/// **Commit(), never Apply(), and that is the entire reason this class has a comment.**
/// Apply() writes to memory and schedules the disk write on a background thread; it is what
/// almost every Android example uses, because for a UI setting the difference never shows.
/// The enrollment token is not a UI setting. Android kills an app's process without warning
/// and without running anything on the way out, so a token that was still queued when that
/// happened is simply gone -- and the agent re-enrolls, and the device appears in the console
/// as a SECOND machine with the same name, days later, with nothing to trace it to.
///
/// Commit() blocks until the write is on disk and returns whether it worked, which is what
/// lets AgentState.SaveIdentity tell FleetClient to discard an identity it could not keep.
/// The cost is a synchronous disk write on the enrolling thread, a handful of times in a
/// device's life.
///
/// **No EncryptedSharedPreferences.** It would need AndroidX Security, whose Jetpack
/// implementation is deprecated, and it would protect against an attacker who has already got
/// root or physical access with the device unlocked -- at which point the fleet's shared
/// enrollment secret is the smaller of the problems. What actually protects this file is the
/// kernel's per-UID isolation plus the manifest's allowBackup="false", which is what stops the
/// identity being restored onto a second device. That is the threat that has a realistic path.
/// </summary>
internal sealed class AndroidStateStore : IStateStore
{
    /// <summary>Deliberately not the default preferences file. A named file is what the
    /// device-owner backup exclusions and any future migration can address by name.</summary>
    private const string PreferencesName = "fleethub.agent.state";

    private readonly ISharedPreferences _prefs;

    public AndroidStateStore(Context context)
    {
        _prefs = context.ApplicationContext!.GetSharedPreferences(PreferencesName, FileCreationMode.Private)
                 ?? throw new InvalidOperationException("SharedPreferences unavailable");
    }

    public string? Get(string key)
    {
        try { return _prefs.GetString(key, null); }
        catch { return null; }
    }

    public bool Set(string key, string? value)
    {
        try
        {
            var editor = _prefs.Edit();
            if (editor is null) return false;
            if (value is null) editor.Remove(key);
            else editor.PutString(key, value);
            return editor.Commit();
        }
        catch { return false; }
    }
}
