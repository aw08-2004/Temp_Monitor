using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Signers;

namespace FleetHubAgent.Update;

/// <summary>
/// The agent's update trust root: verifies the Ed25519 signature over the self-update manifest
/// before any downloaded binary is allowed to replace the running one (see
/// <see cref="SelfUpdater"/> and AgentConfig.UpdatePublicKeyHex).
///
/// **Fails closed, on everything.** An unset key, a missing signature, malformed hex, a key or
/// signature of the wrong length, or a signature that simply does not verify all return false,
/// and no exception ever escapes as "valid". That matters more than it looks: every other
/// failure in this agent degrades to a machine that reports nothing for a while, and this one
/// degrades to a machine that runs somebody else's code as root.
///
/// A byte-for-byte port of the Windows agent's verifier, which is deliberate -- the same key
/// signs both manifests, and a subtly different verification would mean an artifact that one
/// fleet accepts and the other does not. BouncyCastle because .NET 10 still has no Ed25519 in
/// the base class library; it is the same package and version the Windows agent already
/// depends on.
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
