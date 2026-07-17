using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Signers;

namespace TempMonitorAgent.Fleet;

/// <summary>
/// The agent's update trust root: verifies the Ed25519 signature over the self-update
/// manifest before any downloaded binary is allowed to replace the running one (see
/// SelfUpdater + AgentConfig.UpdatePublicKeyHex). Fails closed exactly like
/// companion.verify_signature — an unset key, missing signature, malformed hex, or a
/// bad signature all return false, and no exception ever escapes as "valid".
///
/// This once also verified signed fleet commands. Commands are no longer signed (the
/// hub authorizes them on the console session instead), but this path is unrelated to
/// that change and must stay enforced: it is what stops a compromised hub from pushing
/// a malicious binary to the fleet.
/// </summary>
public static class SignatureVerifier
{
    /// <summary>Verify a detached Ed25519 signature (hex) over arbitrary bytes.</summary>
    public static bool VerifyRaw(string? publicKeyHex, byte[] message, string? signatureHex)
    {
        if (string.IsNullOrWhiteSpace(publicKeyHex)) return false;
        if (string.IsNullOrWhiteSpace(signatureHex)) return false;

        try
        {
            var pubBytes = Convert.FromHexString(publicKeyHex.Trim());
            var sigBytes = Convert.FromHexString(signatureHex.Trim());
            if (pubBytes.Length != 32 || sigBytes.Length != 64) return false;

            var pub = new Ed25519PublicKeyParameters(pubBytes, 0);
            var verifier = new Ed25519Signer();
            verifier.Init(forSigning: false, pub);
            verifier.BlockUpdate(message, 0, message.Length);
            return verifier.VerifySignature(sigBytes);
        }
        catch
        {
            return false; // fail closed on any malformed input
        }
    }
}
