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
/// </summary>
public sealed class SelfUpdater
{
    private readonly ILogger<SelfUpdater> _log;
    private readonly AgentState _state;
    private readonly HttpClient _http;

    public SelfUpdater(ILogger<SelfUpdater> log, AgentState state)
    {
        _log = log;
        _state = state;
        _http = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };
    }

    /// <summary>On boot, if we successfully reached a version >= the pending target,
    /// clear the restart guard and delete the old binary.</summary>
    public void ReconcileAfterBoot()
    {
        var rs = _state.LoadRestartState();
        if (rs is null) return;
        if (VersionUtil.Compare(AgentConfig.Version, rs.Target) >= 0)
        {
            _state.ClearRestartState();
            TryDeleteOldBinary();
            _log.LogInformation("Update to {Target} confirmed (now {Version})", rs.Target, AgentConfig.Version);
        }
    }

    /// <summary>Check for and apply an update. Returns true if an update was applied
    /// (the process is exiting for restart); false otherwise.</summary>
    public async Task<bool> CheckAndApplyAsync(CancellationToken ct)
    {
        if (Environment.GetEnvironmentVariable("TEMP_MONITOR_NO_UPDATE") == "1")
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

            // 7. Swap: rename running exe aside, move new one into place.
            var currentPath = Environment.ProcessPath;
            if (string.IsNullOrEmpty(currentPath))
            {
                _log.LogWarning("[update] cannot resolve current exe path");
                return false;
            }
            var oldPath = currentPath + ".old";
            try { if (File.Exists(oldPath)) File.Delete(oldPath); } catch { /* ignore */ }
            File.Move(currentPath, oldPath);            // rename running exe (allowed on Windows)
            File.Move(stagedPath, currentPath);         // put the new binary in place

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
