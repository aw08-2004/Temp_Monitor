using System.Security.Cryptography;
using Microsoft.Extensions.Logging;
using TempMonitorAgent.Fleet;
using TempMonitorAgent.State;

namespace TempMonitorAgent.Update;

/// <summary>
/// Signed, service-safe self-update. Fetches a small Ed25519-signed manifest
/// (version + sha256 + url), verifies it fail-closed, downloads the newer binary,
/// verifies its hash against the SIGNED value, then swaps it in by renaming the
/// running exe aside (allowed on Windows) and requesting an SCM restart via a
/// distinct exit code. A restart-count guard stops a bad update from looping.
///
/// **That guard stops the same target being downloaded again; it is not a rollback.**
/// Nothing here re-launches the previous binary, because a build too broken to start is
/// also too broken to decide anything -- that decision belongs to whatever is still
/// running at that point, the SCM or the installer. What this class does promise is that
/// the previous binary is still sitting beside the new one, as `.old`, until the new one
/// has reached the hub at least once (ReconcileAfterBoot / ConfirmRunningBuild).
/// </summary>
public sealed class SelfUpdater
{
    private readonly ILogger<SelfUpdater> _log;
    private readonly AgentState _state;
    private readonly HttpClient _http;

    /// <summary>1 while an applied update is still waiting to be confirmed by this build
    /// doing real work. Set on the boot thread by <see cref="ReconcileAfterBoot"/> and
    /// CLAIMED, not merely read, by <see cref="ConfirmRunningBuild"/>: two independent
    /// loops call that method now, on their own thread-pool threads, so the interlocked
    /// exchange is what makes the retirement happen exactly once rather than twice.</summary>
    private int _awaitingConfirmation;

    public SelfUpdater(ILogger<SelfUpdater> log, AgentState state)
    {
        _log = log;
        _state = state;
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };
    }

    /// <summary>On boot, notice that we came back on the version an update was aiming for --
    /// and then **do nothing about it until the build has actually worked**.
    ///
    /// This used to clear the restart guard and delete the .old binary right here, which
    /// treated "the process started" as "the update succeeded". Those are not the same
    /// claim, and the gap between them is where the fleet's worst outcome lives: a build
    /// that starts, deletes its own predecessor, and only then turns out to be unable to
    /// reach the hub is a machine that cannot be reached, fixed, or told anything -- on
    /// every machine at once, because they all take the same manifest. Deleting the one
    /// artifact that could undo it was the last thing to do at the first moment it was
    /// safe to do it.
    ///
    /// So .old now survives boot and is cleared by ConfirmRunningBuild instead. Note what
    /// this does NOT add: nothing here starts the old binary again. Rolling back needs
    /// something that is still running when this process cannot start, which means the SCM
    /// or the installer, not the agent -- see the class summary. What this buys is that the
    /// artifact is still on disk when a human goes looking for it, for the whole window in
    /// which it matters.</summary>
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
        // Older than the target means the swap did not take and the SCM started the old
        // binary. Leave the state alone: its Count is what stops CheckAndApplyAsync
        // retrying the same broken target forever.
    }

    /// <summary>Called once the updated build has completed a round trip the hub accepted,
    /// which is the first evidence it can do its job rather than merely start. Clears the
    /// restart guard and drops the previous binary.
    ///
    /// **Two loops call this, and that is deliberate.** Confirmation must not be coupled to
    /// any one subsystem working: gating it on the telemetry report alone tied "the update
    /// worked" to "the CPU sensor also works", so a machine with no PawnIO driver -- a VM, a
    /// hardened build -- never reached the call and kept its previous binary forever while
    /// being perfectly healthy. The heartbeat has the opposite blind spot, since it needs
    /// enrollment and /api/report does not. Either one landing is proof, so both call it and
    /// whichever arrives first wins; the interlocked claim above is what makes the second
    /// call free.
    ///
    /// Cheap enough for both cadences: one interlocked read per five-to-ten seconds, and
    /// nothing at all after the first success.</summary>
    public void ConfirmRunningBuild()
    {
        if (Interlocked.Exchange(ref _awaitingConfirmation, 0) == 0) return;
        _state.ClearRestartState();
        TryDeleteOldBinary();
        _log.LogInformation("Update to {Version} confirmed -- the hub accepted this build", AgentConfig.Version);
    }

    /// <summary>Check for and apply an update. Returns true if an update was applied
    /// (the process is exiting for restart); false otherwise.</summary>
    public async Task<bool> CheckAndApplyAsync(CancellationToken ct)
    {
        if (AgentConfig.UpdatesDisabled)
            return false;

        try
        {
            // 1. Manifest + detached signature.
            var manifestBytes = await _http.GetByteArrayAsync(AgentConfig.UpdateManifestUrl, ct);
            var sigHex = (await _http.GetStringAsync(AgentConfig.UpdateManifestSigUrl, ct)).Trim();

            // 2. Verify signature over the EXACT manifest bytes (fail-closed).
            if (!SignatureVerifier.VerifyRaw(AgentConfig.UpdatePublicKeyHex, manifestBytes, sigHex))
            {
                _log.LogWarning("[update] manifest signature invalid — refusing");
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

            // 4. Newer?
            if (VersionUtil.Compare(manifest.Version, AgentConfig.Version) <= 0)
                return false;

            // 5. Restart-loop guard.
            var rs = _state.LoadRestartState();
            if (rs is not null && rs.Target == manifest.Version && rs.Count >= AgentConfig.MaxChainRestarts)
            {
                _log.LogWarning("[update] giving up on {Target} after {Count} restarts", rs.Target, rs.Count);
                return false;
            }

            _log.LogInformation("[update] {Cur} -> {New}", AgentConfig.Version, manifest.Version);

            // 6. Download to staging + verify hash against the SIGNED sha256.
            Directory.CreateDirectory(AgentConfig.UpdateStagingDir);
            var stagedPath = Path.Combine(AgentConfig.UpdateStagingDir,
                $"TempMonitorAgent-{manifest.Version}.exe");
            var exeBytes = await _http.GetByteArrayAsync(manifest.Url, ct);
            var actualSha = Convert.ToHexString(SHA256.HashData(exeBytes)).ToLowerInvariant();
            if (!string.Equals(actualSha, manifest.Sha256.Trim().ToLowerInvariant(), StringComparison.Ordinal))
            {
                _log.LogWarning("[update] sha256 mismatch (got {Got}, want {Want}) — aborting", actualSha, manifest.Sha256);
                return false;
            }
            await File.WriteAllBytesAsync(stagedPath, exeBytes, ct);

            // 6b. Re-verify FROM DISK, because what step 7 moves into place is the file, not
            // the byte array checked above. Staging lives under %ProgramData%, whose ACL is
            // now locked to SYSTEM/Administrators (see StateDirectory) -- but this path ends
            // at "the SCM runs these bytes as SYSTEM", so it does not rest on the ACL alone.
            // A pre-created, attacker-owned file at stagedPath survives our write with its
            // owner's full control intact; reading back what we are about to move is what
            // makes that not matter.
            var stagedSha = Convert.ToHexString(
                SHA256.HashData(await File.ReadAllBytesAsync(stagedPath, ct))).ToLowerInvariant();
            if (!string.Equals(stagedSha, actualSha, StringComparison.Ordinal))
            {
                _log.LogWarning("[update] staged file changed after verification — aborting");
                try { File.Delete(stagedPath); } catch { /* ignore */ }
                return false;
            }

            // 7. Swap: rename running exe aside, move new one into place.
            var currentPath = Environment.ProcessPath;
            if (string.IsNullOrEmpty(currentPath))
            {
                _log.LogWarning("[update] cannot resolve current exe path");
                return false;
            }
            var oldPath = currentPath + ".old";
            // A second update landing before the first was confirmed overwrites the rollback
            // target with the unconfirmed build. Accepted rather than guarded: refusing to
            // update while awaiting confirmation would strand exactly the machine that most
            // needs the next release -- one that boots but cannot reach the hub, and so can
            // never confirm anything.
            try { if (File.Exists(oldPath)) File.Delete(oldPath); } catch { /* ignore */ }
            File.Move(currentPath, oldPath);            // rename running exe (allowed on Windows)
            try
            {
                File.Move(stagedPath, currentPath);     // put the new binary in place
            }
            catch (Exception move)
            {
                // **The gap between those two moves is the only place this method can leave a
                // machine with no agent binary at all**, and it used to be unguarded: the
                // second move throws (a scanner holding the staged file, a full disk, an
                // interrupted cross-volume copy), the outer catch logs it, and the exe is
                // simply gone. The running process survives -- it is already in memory -- so
                // nothing looks wrong until the next SCM start, which is a reboot or the next
                // update, and by then the agent that would have carried a fix is the thing
                // that is missing. Recovery is a desk visit, per machine.
                //
                // So put the original back and fail the update. A machine that did not update
                // is still reachable and will try again; a machine with no exe is not.
                _log.LogWarning(move, "[update] could not move the new binary into place -- restoring the previous one");
                try
                {
                    // **overwrite, because the failure this restores from can leave a PARTIAL
                    // file behind.** Within one volume File.Move is a rename and fails having
                    // created nothing, but across volumes it degrades to copy-then-delete, so
                    // a full disk or an interrupted copy -- two of the three cases named above
                    // -- can leave a half-written exe at currentPath. The two-argument overload
                    // refuses an existing destination, so the restore would then throw for a
                    // reason that has nothing to do with the original failure, and the operator
                    // following the message below by hand would hit the same refusal.
                    File.Move(oldPath, currentPath, overwrite: true);
                }
                catch (Exception restore)
                {
                    // Both moves failing is the one outcome nothing here can repair, so say
                    // exactly what a human has to do rather than logging a stack trace.
                    _log.LogError(restore, "[update] RESTORE FAILED -- rename {Old} over {Cur} by hand (replacing it if it exists); this service cannot start until you do", oldPath, currentPath);
                }
                try { File.Delete(stagedPath); } catch { /* ignore */ }
                return false;
            }

            // 8. Record the attempt and request an SCM-driven restart.
            _state.SaveRestartState(new RestartState
            {
                Target = manifest.Version,
                Count = (rs?.Target == manifest.Version ? rs!.Count : 0) + 1,
            });
            _log.LogInformation("[update] staged {New}; exiting {Code} for restart", manifest.Version, AgentConfig.RestartExitCode);
            return true;
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException)
        {
            _log.LogDebug("[update] check failed (offline?): {Msg}", e.Message);
            return false;
        }
        catch (Exception e)
        {
            _log.LogWarning(e, "[update] unexpected error");
            return false;
        }
    }

    private static void TryDeleteOldBinary()
    {
        try
        {
            var old = Environment.ProcessPath + ".old";
            if (File.Exists(old)) File.Delete(old);
        }
        catch { /* the running binary may still be locked; next boot retries */ }
    }
}
