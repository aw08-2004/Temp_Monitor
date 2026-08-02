using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json.Nodes;
using TempMonitorAgent.Fleet.Executors;

namespace TempMonitorAgent.Network;

/// <summary>
/// Sends a magic packet on behalf of a sleeping machine on this one's subnet (roadmap #10).
///
/// **This command is about somebody ELSE.** It is the only command type in the product whose
/// target machine is not the machine it concerns: the hub picks an awake peer that shares
/// the sleeping machine's subnet and asks it to broadcast. A hub-sent broadcast reaches the
/// hub's own L2 segment and nothing else, so for a helpdesk with more than one site,
/// peer-relay is the only mechanism that works at all without touching router configuration.
///
/// **All of the intelligence is hub-side, deliberately.** This agent does not decide who to
/// wake, does not resolve subnets and does not know whether it worked. It receives MACs and
/// a broadcast address and sends frames. Nothing acknowledges a magic packet -- there is no
/// reply, no ICMP, no error for a MAC that does not exist -- so the honest report is "the
/// packet went out", and the hub confirms (or does not) from the target's own next check-in.
/// Anything more confident here would be invented.
///
/// **Every MAC in the list gets a frame.** A machine can have several wired adapters -- a
/// dock and an onboard NIC -- and neither the hub nor this agent can know which is cabled
/// right now. The extra 102-byte frames cost nothing and picking wrong is the difference
/// between the feature working and not.
/// </summary>
public sealed class WakeMachineExecutor : ICommandExecutor
{
    public string Type => "wake_machine";

    public Task<CommandResult> ExecuteAsync(
        FleetCommand cmd, Action<string>? onOutput, CancellationToken ct)
    {
        var target = cmd.Params.GetString("target") ?? "(unnamed)";
        var broadcast = (cmd.Params.GetString("broadcast") ?? "").Trim();
        var port = cmd.Params.GetInt("port", 9);
        var macs = ReadMacs(cmd.Params);

        if (macs.Count == 0)
            return Task.FromResult(CommandResult.Fail("no MAC address to wake"));
        if (!IPAddress.TryParse(broadcast, out var destination))
            return Task.FromResult(CommandResult.Fail(
                $"not a broadcast address: {broadcast}"));
        if (port is < 1 or > 65535)
            return Task.FromResult(CommandResult.Fail($"not a port: {port}"));

        var log = new StringBuilder();
        var sent = 0;
        var failures = new List<string>();

        try
        {
            using var socket = new UdpClient();
            // Without this the send is refused outright on a broadcast address. Set once,
            // before the loop, so a failure here fails the whole command rather than being
            // reported per MAC as if the addresses were at fault.
            socket.EnableBroadcast = true;
            foreach (var mac in macs)
            {
                ct.ThrowIfCancellationRequested();
                try
                {
                    var frame = MagicPacket(mac);
                    socket.Send(frame, frame.Length, new IPEndPoint(destination, port));
                    sent++;
                    log.AppendLine($"sent to {mac} via {broadcast}:{port}");
                }
                catch (Exception e)
                {
                    failures.Add($"{mac}: {e.Message}");
                }
            }
        }
        catch (OperationCanceledException) { throw; }
        catch (Exception e)
        {
            return Task.FromResult(CommandResult.Fail(
                $"could not open a broadcast socket: {e.Message}"));
        }

        foreach (var failure in failures) log.AppendLine($"FAILED {failure}");
        log.AppendLine(sent > 0
            ? $"woke {target}: {sent} magic packet(s) sent"
            : $"could not wake {target}");

        // Partial success is success: one adapter's frame reaching the wire is enough to
        // wake the machine, and reporting failure because a second address was rejected
        // would put the hub's request back in the queue for another relay to duplicate.
        return Task.FromResult(sent > 0
            ? CommandResult.Ok(log.ToString())
            : CommandResult.Fail(log.ToString()));
    }

    /// <summary>The 102-byte frame: six 0xFF bytes, then the target MAC sixteen times.</summary>
    public static byte[] MagicPacket(string mac)
    {
        var bytes = ParseMac(mac);
        var frame = new byte[6 + 16 * 6];
        for (var i = 0; i < 6; i++) frame[i] = 0xFF;
        for (var repeat = 0; repeat < 16; repeat++)
            Buffer.BlockCopy(bytes, 0, frame, 6 + repeat * 6, 6);
        return frame;
    }

    /// <summary>The six bytes of a MAC in any of the spellings that reach us. Throws on
    /// anything else -- a MAC we cannot parse must not become a frame of zeroes, which
    /// would be sent successfully and wake nothing.</summary>
    public static byte[] ParseMac(string mac)
    {
        var hex = new string((mac ?? "").Where(Uri.IsHexDigit).ToArray());
        if (hex.Length != 12) throw new FormatException($"not a MAC address: {mac}");
        return Convert.FromHexString(hex);
    }

    private static List<string> ReadMacs(JsonNode? paramsNode)
    {
        var macs = new List<string>();
        if (paramsNode is JsonObject obj && obj.TryGetPropertyValue("macs", out var value)
            && value is JsonArray array)
        {
            foreach (var item in array)
            {
                var text = item?.ToString()?.Trim();
                if (!string.IsNullOrEmpty(text)) macs.Add(text);
            }
        }
        return macs;
    }
}
