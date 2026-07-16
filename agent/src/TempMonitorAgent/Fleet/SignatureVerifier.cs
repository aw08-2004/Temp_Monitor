using System.Text.Json.Nodes;
using Org.BouncyCastle.Crypto.Parameters;
using Org.BouncyCastle.Crypto.Signers;

namespace TempMonitorAgent.Fleet;

/// <summary>
/// Verifies Ed25519 signatures over canonical command bytes and over raw update
/// manifests. Fails closed exactly like fleet.verify_command_signature /
/// companion.verify_signature: an unset key, missing signature, malformed hex, or a
/// bad signature all return false — no exception ever escapes as "valid".
/// </summary>
public static class SignatureVerifier
{
    /// <summary>Verify a signed fleet command before executing it.</summary>
    public static bool VerifyCommand(
        string? publicKeyHex, string commandType, string machine,
        JsonNode? paramsNode, string? signatureHex)
    {
        var message = CommandCanonicalizer.CanonicalBytes(commandType, machine, paramsNode);
        return VerifyRaw(publicKeyHex, message, signatureHex);
    }

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
