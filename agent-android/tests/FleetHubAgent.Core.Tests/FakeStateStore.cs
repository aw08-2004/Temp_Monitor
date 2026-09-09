using FleetHubAgent.State;

namespace FleetHubAgent.Core.Tests;

/// <summary>An in-memory <see cref="IStateStore"/> that can be told to fail its writes.
///
/// The failure mode is the point. A store that always succeeds tests the happy path of
/// MachineNameProvider and FleetClient, and both of those have a persist-before-adopt ordering
/// whose whole reason for existing is the case where the write does NOT succeed -- on Android
/// that is a device with a full data partition or a profile being removed underneath the app.
/// Without this switch that ordering is untested and reads like ceremony.</summary>
internal sealed class FakeStateStore : IStateStore
{
    private readonly Dictionary<string, string> _values = new(StringComparer.Ordinal);

    /// <summary>When true, every Set reports failure and stores nothing.</summary>
    public bool WritesFail { get; set; }

    public int WriteCount { get; private set; }

    public string? Get(string key) => _values.GetValueOrDefault(key);

    public bool Set(string key, string? value)
    {
        WriteCount++;
        if (WritesFail) return false;
        if (value is null) _values.Remove(key);
        else _values[key] = value;
        return true;
    }
}
