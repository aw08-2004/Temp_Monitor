using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Signers;

namespace FleetHubAgent.Fleet;

/// <summary>
/// The agent's update trust root: verifies the Ed25519 signature over the self-update manifest
/// before any downloaded binary is allowed to replace the running one (see SelfUpdater and
/// AgentConfig.UpdatePublicKeyHex).
///
/// **Fails closed.** An unset key, a missing signature, malformed hex, a wrong-length key or
/// signature, and a bad signature all return false, and no exception ever escapes as "valid" --
/// which is why the whole body sits in one try/catch that returns false rather than letting a
/// parse error propagate to a caller that might treat a thrown exception as something other
/// than a rejection.
///
/// **Byte-for-byte identical to the Windows agent's verifier, deliberately.** Both fleets
/// verify manifests signed by the same offline key. Two implementations that disagreed about
/// what a valid signature is would mean a release that installs on one and is refused on the
/// other, and the refusing side fails silently -- a fleet that quietly stops updating is
/// exactly the failure the whole signing arrangement exists to prevent.
///
/// Fleet COMMANDS are not signed; the hub authorizes them on its console session instead. That
/// is unrelated to this path, which must stay enforced: it is what stops a compromised hub, or
/// anyone who can answer for raw.githubusercontent.com, from pushing a binary to the fleet.
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
