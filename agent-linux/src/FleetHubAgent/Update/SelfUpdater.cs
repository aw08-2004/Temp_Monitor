using System.Security.Cryptography;
using Microsoft.Extensions.Logging;
using FleetHubAgent.Fleet;
using FleetHubAgent.State;

namespace FleetHubAgent.Update;

/// <summary>
/// Signed, service-safe self-update: fetch a small Ed25519-signed manifest (version + sha256 +
/// url), verify it fail-closed, download the newer binary, verify its hash against the SIGNED
/// value, swap it in by renaming the running binary aside, and exit so systemd restarts onto
/// it. A restart-count guard stops a bad update looping.
///
/// **The guard stops the same target being downloaded again; it is not a rollback.** Nothing
/// here re-launches the previous binary, because a build too broken to start is also too broken
/// to decide anything. What this class does promise is that the previous binary is still beside
/// the new one, as `.old`, until the new one has reached the hub at least once
/// (ReconcileAfterBoot / ConfirmRunningBuild).
///
/// The shape is the Windows agent's SelfUpdater, and the parts that are the same are the same
/// on purpose -- both fleets take manifests signed by one offline key, so the verification
/// order, the hash-after-write re-read, and the restore-on-failed-swap all have to behave
/// identically. **Four things are genuinely different on Linux, and each is a way to brick a
/// machine if ported blindly:**
///
///   1. STAGING MUST SHARE A FILESYSTEM WITH THE BINARY. File.Move is rename(2) within one
///      filesystem and atomic; across filesystems .NET degrades it to copy-then-delete, which
///      is not. /var/lib and /opt are separate mounts on plenty of real installs -- including
///      the kind of NAS this agent is aimed at -- so staging next to the state directory (where
///      the Windows agent stages, under %ProgramData%) would silently turn the atomic swap into
///      a copy that can be interrupted half-written. Staging therefore lives beside the
///      executable. See StagingDir.
///   2. THE EXECUTE BIT HAS TO BE SET. A downloaded file is 0644. Windows has no such concept
///      and the Windows updater has no equivalent line; skip it here and systemd answers
///      "Permission denied" on a binary that is present, correct and verified.
///   3. A RUNNING BINARY CANNOT BE WRITTEN, BUT CAN BE RENAMED. Writing over it gives ETXTBSY;
///      rename(2) is fine, because the running process holds the inode rather than the name.
///      That is what makes the same rename-aside dance work here at all.
///   4. RESTART IS systemd's, NOT AN SCM'S. The unit is Restart=always with
///      StartLimitIntervalSec=0, so simply exiting is enough and no failure action needs
///      configuring. The distinct exit code is kept for the journal's sake, not because
///      anything keys on it.
/// </summary>
public sealed class SelfUpdater
{
    private readonly ILogger<SelfUpdater> _log;
    private readonly AgentState _state;
    private readonly HttpClient _http;

    /// <summary>1 while an applied update is still waiting to be confirmed by this build doing
    /// real work. Set on the boot thread by <see cref="ReconcileAfterBoot"/> and CLAIMED, not
    /// merely read, by <see cref="ConfirmRunningBuild"/>: two independent loops call that
    /// method on their own thread-pool threads, so the interlocked exchange is what makes the
    /// retirement happen exactly once rather than twice.</summary>
    private int _awaitingConfirmation;

    public SelfUpdater(ILogger<SelfUpdater> log, AgentState state)
    {
        _log = log;
        _state = state;
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };
    }

    /// <summary>Where a downloaded binary is staged: a hidden directory BESIDE the running
    /// executable, not under the state root.
    ///
    /// This is difference (1) in the class note, and it is the one most likely to be "tidied"
    /// into /var/lib by someone matching the Windows layout. /opt and /var are routinely
    /// separate mounts; a cross-filesystem File.Move is a copy, and a copy interrupted by a
    /// full disk leaves a partial binary where the agent used to be.
    ///
    /// Returns null when the executable path cannot be resolved, which is the caller's cue to
    /// do nothing at all.</summary>
    internal static string? StagingDir()
    {
        var exe = Environment.ProcessPath;
        if (string.IsNullOrEmpty(exe)) return null;
        var dir = Path.GetDirectoryName(exe);
        return string.IsNullOrEmpty(dir) ? null : Path.Combine(dir, ".update");
    }

    /// <summary>On boot, notice that we came back on the version an update was aiming for --
    /// and then **do nothing about it until the build has actually worked**.
    ///
    /// Clearing the guard and deleting the `.old` binary here would treat "the process started"
    /// as "the update succeeded". Those are not the same claim, and the gap between them is
    /// where the worst outcome lives: a build that starts, deletes its own predecessor, and
    /// only then turns out to be unable to reach the hub is a machine that cannot be reached,
    /// fixed, or told anything -- on every machine at once, because they all take the same
    /// manifest.
    ///
    /// So `.old` survives boot and is cleared by ConfirmRunningBuild instead. Nothing here
    /// starts the old binary again: rolling back needs something still running when this
    /// process cannot start, which is systemd or a person, not the agent.</summary>
    public void ReconcileAfterBoot()
    {
        var rs = _state.LoadRestartState();
        if (rs is null) return;
        if (VersionUtil.Compare(AgentConfig.Version, rs.Target) >= 0)
        {
            Volatile.Write(ref _awaitingConfirmation, 1);
            _log.LogInformation(
                "Update to {Target} booted (now {Version}); holding the previous binary until this build reaches the hub",
                rs.Target, AgentConfig.Version);
        }
        // Older than the target means the swap did not take and systemd started the old binary.
        // Leave the state alone: its Count is what stops CheckAndApplyAsync retrying the same
        // broken target forever.
    }

    /// <summary>Called once the updated build has completed a round trip the hub accepted --
    /// the first evidence it can do its job rather than merely start. Clears the restart guard
    /// and drops the previous binary.
    ///
    /// **Two loops call this, and that is deliberate.** Confirmation must not be coupled to any
    /// one subsystem working. Gating it on the telemetry report alone would tie "the update
    /// worked" to "this machine has a readable thermal sensor" -- and a VM with no sensor never
    /// reports at all (see Worker), so it would keep its previous binary forever while being
    /// perfectly healthy. The heartbeat has the opposite blind spot: it needs enrollment, and
    /// /api/report does not. Either landing is proof, so both call it and whichever arrives
    /// first wins; the interlocked claim above makes the second call free.</summary>
    public void ConfirmRunningBuild()
    {
        if (Interlocked.Exchange(ref _awaitingConfirmation, 0) == 0) return;
        _state.ClearRestartState();
        TryDeleteOldBinary();
        _log.LogInformation("Update to {Version} confirmed -- the hub accepted this build", AgentConfig.Version);
    }

    /// <summary>Check for and apply an update. Returns true if one was applied, meaning the
    /// caller should exit for restart.</summary>
    public async Task<bool> CheckAndApplyAsync(CancellationToken ct)
    {
        if (AgentConfig.UpdatesDisabled) return false;

        try
        {
            // 1. Manifest + detached signature.
            var manifestBytes = await _http.GetByteArrayAsync(AgentConfig.UpdateManifestUrl, ct);
            var sigHex = (await _http.GetStringAsync(AgentConfig.UpdateManifestSigUrl, ct)).Trim();

            // 2. Verify the signature over the EXACT manifest bytes, before parsing them.
            // Parsing first would mean deserializing attacker-controlled JSON to decide whether
            // to trust it, which is the wrong order however safe the parser is.
            if (!SignatureVerifier.VerifyRaw(AgentConfig.UpdatePublicKeyHex, manifestBytes, sigHex))
            {
                _log.LogWarning("[update] manifest signature invalid -- refusing");
                return false;
            }

            // 3. Parse.
            var manifest = System.Text.Json.JsonSerializer.Deserialize<UpdateManifest>(manifestBytes);
            if (manifest is null || string.IsNullOrEmpty(manifest.Version) ||
                string.IsNullOrEmpty(manifest.Sha256) || string.IsNullOrEmpty(manifest.Url))
            {
                _log.LogWarning("[update] manifest incomplete");
                return false;
            }

            // 4. Newer? Strictly greater, never equal -- see CLAUDE.md on why a reused version
            // number means nothing happens anywhere while the release looks shipped.
            if (VersionUtil.Compare(manifest.Version, AgentConfig.Version) <= 0) return false;

            // 5. Restart-loop guard.
            var rs = _state.LoadRestartState();
            if (rs is not null && rs.Target == manifest.Version && rs.Count >= AgentConfig.MaxChainRestarts)
            {
                _log.LogWarning("[update] giving up on {Target} after {Count} restarts", rs.Target, rs.Count);
                return false;
            }

            var stagingDir = StagingDir();
            if (stagingDir is null)
            {
                _log.LogWarning("[update] cannot resolve the running binary's path");
                return false;
            }
            var currentPath = Environment.ProcessPath!;

            _log.LogInformation("[update] {Cur} -> {New}", AgentConfig.Version, manifest.Version);

            // 6. Download and verify the hash against the SIGNED sha256.
            //
            // 0700 on the staging directory: everything below this line ends at "systemd runs
            // these bytes as root", so a directory any local user could drop a file into would
            // be a privilege-escalation primitive rather than a cache.
            StateDirectory.EnsurePrivateDir(stagingDir);
            var stagedPath = Path.Combine(stagingDir, $"fleethub-agent-{manifest.Version}");

            var bytes = await _http.GetByteArrayAsync(manifest.Url, ct);
            var actualSha = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
            if (!string.Equals(actualSha, manifest.Sha256.Trim().ToLowerInvariant(), StringComparison.Ordinal))
            {
                _log.LogWarning("[update] sha256 mismatch (got {Got}, want {Want}) -- aborting",
                    actualSha, manifest.Sha256);
                return false;
            }
            await File.WriteAllBytesAsync(stagedPath, bytes, ct);

            // 6b. Re-verify FROM DISK, because what step 7 renames into place is the file, not
            // the byte array checked above. A pre-created, attacker-owned file at stagedPath
            // would survive our write with its owner still able to rewrite it; reading back
            // what we are about to install is what makes that not matter.
            var stagedSha = Convert.ToHexString(
                SHA256.HashData(await File.ReadAllBytesAsync(stagedPath, ct))).ToLowerInvariant();
            if (!string.Equals(stagedSha, actualSha, StringComparison.Ordinal))
            {
                _log.LogWarning("[update] staged file changed after verification -- aborting");
                TryDelete(stagedPath);
                return false;
            }

            // 6c. Make it executable. Difference (2) in the class note: the Windows updater has
            // no equivalent line because Windows has no equivalent concept, and without this
            // systemd reports "Permission denied" for a binary that is present and verified --
            // a failure that looks like a packaging bug and is a one-line omission.
            // 0700, not 0755: nothing but root should be able to run the fleet's agent binary.
            File.SetUnixFileMode(stagedPath,
                UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);

            // 7. Swap: rename the running binary aside, rename the new one into place. Both are
            // renames within one directory, so both are atomic -- see difference (1).
            var oldPath = currentPath + ".old";
            // A second update landing before the first was confirmed overwrites the rollback
            // target with the unconfirmed build. Accepted rather than guarded: refusing to
            // update while awaiting confirmation would strand exactly the machine that most
            // needs the next release -- one that boots but cannot reach the hub, and so can
            // never confirm anything.
            TryDelete(oldPath);
            File.Move(currentPath, oldPath);
            try
            {
                File.Move(stagedPath, currentPath);
            }
            catch (Exception move)
            {
                // **The gap between those two renames is the only place this method can leave a
                // machine with no agent binary at all.** The running process survives -- it is
                // already in memory, and on Linux it holds the inode regardless of the name --
                // so nothing looks wrong until the next restart, which is a reboot or the next
                // update. By then the agent that would have carried a fix is the thing that is
                // missing, and recovery is a visit to the machine.
                //
                // So put the original back and fail the update. A machine that did not update
                // is still reachable; a machine with no binary is not.
                _log.LogWarning(move, "[update] could not move the new binary into place -- restoring the previous one");
                try
                {
                    File.Move(oldPath, currentPath, overwrite: true);
                }
                catch (Exception restore)
                {
                    // Both renames failing is the one outcome nothing here can repair, so say
                    // exactly what a human has to do rather than logging a stack trace at them.
                    _log.LogError(restore,
                        "[update] RESTORE FAILED -- run: mv -f {Old} {Cur} && systemctl restart fleethub-agent. " +
                        "This service cannot start until you do", oldPath, currentPath);
                }
                TryDelete(stagedPath);
                return false;
            }

            // 8. Record the attempt. Written BEFORE the caller exits, because the guard's whole
            // job is to survive a build that never gets far enough to write anything.
            _state.SaveRestartState(new RestartState
            {
                Target = manifest.Version,
                Count = (rs?.Target == manifest.Version ? rs!.Count : 0) + 1,
            });
            _log.LogInformation("[update] staged {New}; exiting {Code} for systemd to restart",
                manifest.Version, AgentConfig.RestartExitCode);
            return true;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            // Offline, or GitHub is having a day. Not an error: the check runs weekly and a
            // failed one is invisible by design -- see the note on UpdateIntervalSeconds.
            _log.LogDebug("[update] check failed (offline?): {Msg}", e.Message);
            return false;
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "[update] unexpected error");
            return false;
        }
    }

    private void TryDeleteOldBinary()
    {
        var exe = Environment.ProcessPath;
        if (!string.IsNullOrEmpty(exe)) TryDelete(exe + ".old");
    }

    private static void TryDelete(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); } catch { /* next pass retries */ }
    }
}
