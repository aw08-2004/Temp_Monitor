using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Signers;

namespace FleetHubAgent.Update;

/// <summary>
/// The agent's update trust root: verifies the Ed25519 signature over the self-update manifest
/// before any downloaded APK is allowed to be installed (see <see cref="SelfUpdater"/> and
/// AgentConfig.UpdatePublicKeyHex).
///
/// **Fails closed, on everything.** An unset key, a missing signature, malformed hex, a key or
/// signature of the wrong length, or a signature that simply does not verify all return false,
/// and no exception ever escapes as "valid". That matters more here than the identical code
/// matters on the other two agents: what this gates is a package installed silently by a
/// Device Owner, with no prompt and nobody watching.
///
/// A byte-for-byte port of the Windows and Linux agents' verifier. The same offline key signs
/// all three manifests, so a subtly different verification would mean an artifact one fleet
/// accepts and another refuses. BouncyCastle because .NET 10 still has no Ed25519 in the base
/// class library; it is the same package and version the other two already depend on.
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
