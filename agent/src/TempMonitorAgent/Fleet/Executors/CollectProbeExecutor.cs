using System.Diagnostics;
using System.Management;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.Logging;
using Microsoft.Win32;

namespace TempMonitorAgent.Fleet.Executors;

/// <summary>
/// collect_probe: read one specific thing the regular telemetry does not carry, and report it.
///
/// The rules engine needs answers to questions the sensor block cannot express -- "is this
/// app installed", "what version is that DLL", "does this file exist" -- and this is how it
/// gets them. The hub schedules one of these per probe per machine on the probe's own
/// interval; there is no polling loop here.
///
/// <b>Everything here is a READ except one kind.</b> registry / file_exists / file_version /
/// wmi cannot change the machine, which is why they are always available. `script` runs
/// arbitrary PowerShell and is gated behind a hub setting that starts off -- the agent still
/// honours it when asked, because the decision belongs to the operator who owns the fleet,
/// not to the agent.
///
/// The result is a JSON object (<c>{"value": ...}</c> or <c>{"error": "..."}</c>) rather than
/// prose, because the hub files it against a typed variable. An error is reported as a
/// SUCCESSFUL command carrying an error field: the command was delivered and answered, and
/// the distinction the hub needs is "this machine says the key is missing" versus "we never
/// heard back", which a failed command would blur.
/// </summary>
public sealed class CollectProbeExecutor : ICommandExecutor
{
    private readonly ILogger<CollectProbeExecutor> _log;

    public CollectProbeExecutor(ILogger<CollectProbeExecutor> log) => _log = log;

    public string Type => "collect_probe";

    public async Task<CommandResult> ExecuteAsync(FleetCommand cmd, Action<string>? onOutput,
                                                  CancellationToken ct)
    {
        string kind = cmd.Params.GetString("kind") ?? "";
        var spec = cmd.Params?["spec"];
        int timeout = Math.Clamp(cmd.Params.GetInt("timeout_seconds", 30), 5, 600);

        try
        {
            object? value = kind switch
            {
                "registry" => ReadRegistry(spec),
                "file_exists" => File.Exists(Expand(spec.GetString("path"))),
                "file_version" => ReadFileVersion(spec),
                "wmi" => ReadWmi(spec),
                "script" => await RunScriptAsync(spec, timeout, ct),
                _ => throw new InvalidOperationException($"unknown probe kind: {kind}"),
            };
            return Answer(new { value });
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception ex)
        {
            _log.LogInformation("collect_probe {Kind} failed: {Error}", kind, ex.Message);
            return Answer(new { error = ex.Message });
        }
    }

    private static CommandResult Answer(object payload) =>
        CommandResult.Ok(JsonSerializer.Serialize(payload));

    /// <summary>Expand %ENVIRONMENT% references so a probe can name %ProgramFiles% without
    /// the operator having to know whether the target is 32- or 64-bit.</summary>
    private static string Expand(string? path) =>
        Environment.ExpandEnvironmentVariables(path ?? "");

    private static object? ReadRegistry(JsonNode? spec)
    {
        string rootName = (spec.GetString("root") ?? "HKLM").ToUpperInvariant();
        RegistryKey root = rootName switch
        {
            "HKLM" => Registry.LocalMachine,
            "HKCU" => Registry.CurrentUser,
            "HKCR" => Registry.ClassesRoot,
            "HKU" => Registry.Users,
            _ => throw new InvalidOperationException($"unknown registry root: {rootName}"),
        };
        string path = spec.GetString("path") ?? "";
        string valueName = spec.GetString("value") ?? "";

        // 64-bit view explicitly. The agent is a 64-bit process so this is already the
        // default, but naming it means a future 32-bit build cannot silently start reading
        // WOW6432Node and reporting a different answer for the same probe.
        using var view = RegistryKey.OpenBaseKey(RootHive(rootName), RegistryView.Registry64);
        using var key = view.OpenSubKey(path);
        if (key is null) return null;
        // An empty value name means the key's DEFAULT value, which is what the registry
        // itself means by "", so it is passed straight through rather than special-cased.
        object? value = key.GetValue(valueName);
        return value is null ? null : Stringify(value);
    }

    private static RegistryHive RootHive(string root) => root switch
    {
        "HKLM" => RegistryHive.LocalMachine,
        "HKCU" => RegistryHive.CurrentUser,
        "HKCR" => RegistryHive.ClassesRoot,
        "HKU" => RegistryHive.Users,
        _ => RegistryHive.LocalMachine,
    };

    /// <summary>A registry value as a single scalar. A REG_MULTI_SZ becomes a comma-joined
    /// string and a REG_BINARY its length: both are answers a `contains` test can work with,
    /// and neither pretends to be a number the operator can compare.</summary>
    private static object Stringify(object value) => value switch
    {
        string[] many => string.Join(", ", many),
        byte[] bytes => bytes.Length,
        _ => value.ToString() ?? "",
    };

    private static object? ReadFileVersion(JsonNode? spec)
    {
        string path = Expand(spec.GetString("path"));
        if (!File.Exists(path)) return null;
        var info = FileVersionInfo.GetVersionInfo(path);
        // ProductVersion first: it is the marketing version people actually compare ("122.0")
        // where FileVersion is often a build stamp. Falls back when a binary carries only one.
        return info.ProductVersion ?? info.FileVersion;
    }

    private static object? ReadWmi(JsonNode? spec)
    {
        string query = spec.GetString("query") ?? "";
        if (!query.TrimStart().StartsWith("select", StringComparison.OrdinalIgnoreCase))
            throw new InvalidOperationException("a WMI probe must be a SELECT query");
        string property = spec.GetString("property") ?? "";

        using var searcher = new ManagementObjectSearcher(query);
        foreach (ManagementObject item in searcher.Get())
        {
            using (item)
            {
                if (!string.IsNullOrEmpty(property))
                {
                    object? value = item[property];
                    return value?.ToString();
                }
                // No property named: return the first one the query selected, so a probe can
                // be written as "select Version from ..." without saying "Version" twice.
                foreach (var prop in item.Properties)
                    return prop.Value?.ToString();
            }
        }
        // No rows is a real answer -- the thing being asked about is not present -- so it is
        // null rather than an error.
        return null;
    }

    private static async Task<object?> RunScriptAsync(JsonNode? spec, int timeout,
                                                      CancellationToken ct)
    {
        string script = spec.GetString("script") ?? "";
        if (string.IsNullOrWhiteSpace(script))
            throw new InvalidOperationException("a script probe needs a script");

        // -EncodedCommand so the script survives being a command-line argument whatever
        // quoting it contains, exactly as RunScriptExecutor does.
        string encoded = Convert.ToBase64String(System.Text.Encoding.Unicode.GetBytes(script));
        var outcome = await ProcessRunner.RunAsync(
            "powershell.exe",
            $"-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {encoded}",
            ct, timeoutSeconds: timeout);
        if (outcome.ExitCode != 0)
            throw new InvalidOperationException(
                $"script exited {outcome.ExitCode}: {Trim(outcome.Output)}");
        // The LAST non-empty line is the value. A script that writes progress and then its
        // answer is the normal shape, and taking the first line would report the progress.
        var lines = (outcome.Output ?? "").Split('\n');
        for (int i = lines.Length - 1; i >= 0; i--)
        {
            string line = lines[i].Trim();
            if (line.Length > 0) return line;
        }
        return null;
    }

    private static string Trim(string? text) =>
        string.IsNullOrEmpty(text) ? "" : (text.Length <= 300 ? text : text[..300]);
}
