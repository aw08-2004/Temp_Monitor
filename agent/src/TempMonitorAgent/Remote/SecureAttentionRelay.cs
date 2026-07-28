using System.IO.Pipes;
using System.Security.AccessControl;
using System.Security.Principal;
using Microsoft.Extensions.Logging;

namespace TempMonitorAgent.Remote;

/// <summary>
/// Lets the session-injected helper trigger Ctrl+Alt+Del.
///
/// The secure attention sequence cannot be synthesised with SendInput -- the kernel intercepts
/// it, which is the entire point of a *secure* attention sequence. The supported route is
/// <c>SendSAS</c> from sas.dll, but it has a condition that is easy to miss: with the usual
/// <c>SoftwareSASGeneration</c> policy value it is only honoured for a caller running <b>as a
/// service</b>. The remote helper is SYSTEM, but it is a session-injected process, not a
/// service, so its own SendSAS call is very likely a silent no-op.
///
/// So the helper asks the real service to do it. The service owns the pipe, calls SendSAS from
/// session 0 where it genuinely is a service, and the operator gets a logon screen.
///
/// The pipe is locked to SYSTEM. It is a "press Ctrl+Alt+Del on the console" primitive, which is
/// not catastrophic on its own -- but it should not be reachable by every process on the box, and
/// the only legitimate caller already runs as SYSTEM.
/// </summary>
public static class SecureAttentionRelay
{
    /// <summary>Pipe name, unqualified. Both ends derive the full path from this.</summary>
    public const string PipeName = "FleetHub.RemoteSas";

    /// <summary>Sent by the helper; anything else is ignored so a stray connection cannot do
    /// something unintended.</summary>
    private const string RequestMessage = "sas";

    private const int ConnectTimeoutMs = 2000;

    /// <summary>
    /// Ask the service to press Ctrl+Alt+Del. Returns false if the service is not listening
    /// (an older agent, or the service is down) so the caller can fall back to its own
    /// SendSAS attempt. Never throws -- a failed Ctrl+Alt+Del must not end the session.
    /// </summary>
    public static bool TryRequest()
    {
        try
        {
            using var client = new NamedPipeClientStream(
                ".", PipeName, PipeDirection.Out, PipeOptions.None,
                TokenImpersonationLevel.Identification);
            client.Connect(ConnectTimeoutMs);
            using var writer = new StreamWriter(client) { AutoFlush = true };
            writer.WriteLine(RequestMessage);
            return true;
        }
        catch
        {
            return false;
        }
    }

    /// <summary>
    /// Serve requests until cancelled. Runs in the agent service (session 0). One connection at
    /// a time is plenty: this fires when an operator clicks a button, not in a loop.
    /// </summary>
    public static async Task RunServerAsync(ILogger log, CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            try
            {
                using var server = CreateServer();
                await server.WaitForConnectionAsync(ct);

                using var reader = new StreamReader(server);
                string? line = await reader.ReadLineAsync(ct);
                if (!string.Equals(line?.Trim(), RequestMessage, StringComparison.Ordinal))
                {
                    log.LogWarning("Secure-attention pipe got an unexpected message; ignoring.");
                    continue;
                }

                log.LogInformation("Secure-attention request from the remote helper; sending SAS.");
                InputInjector.SendSecureAttentionFromService();
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch (Exception e)
            {
                log.LogWarning(e, "Secure-attention pipe server error; retrying.");
                // Don't spin on a persistent failure (e.g. the pipe name is taken).
                try { await Task.Delay(5000, ct); } catch (OperationCanceledException) { break; }
            }
        }
    }

    private static NamedPipeServerStream CreateServer()
    {
        var security = new PipeSecurity();
        var system = new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null);
        security.AddAccessRule(new PipeAccessRule(
            system, PipeAccessRights.ReadWrite | PipeAccessRights.CreateNewInstance,
            AccessControlType.Allow));
        // Administrators need FullControl on the object we create, or the ACL is rejected as
        // having no owner-equivalent access.
        security.AddAccessRule(new PipeAccessRule(
            new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null),
            PipeAccessRights.FullControl, AccessControlType.Allow));

        return NamedPipeServerStreamAcl.Create(
            PipeName, PipeDirection.In, 1, PipeTransmissionMode.Byte,
            PipeOptions.Asynchronous, 0, 0, security);
    }
}
