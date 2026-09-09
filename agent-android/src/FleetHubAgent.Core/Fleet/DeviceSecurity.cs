namespace FleetHubAgent.Fleet;

/// <summary>
/// Locking a device's screen, and erasing it -- roadmap #23 phase H.
///
/// Deliberately tiny and deliberately in Core, like <see cref="IPolicyEnforcer"/> and
/// ILocationSource: everything about WHEN to do either of these, what to answer, and how long
/// to wait before the device stops existing is the executor's business, so the platform half
/// cannot accidentally decide any of it.
///
/// **Neither method throws.** A device that is not a Device Owner cannot do either of these,
/// and that is the ordinary state of a sideloaded build rather than an error. It is reported
/// through <see cref="CanEnforce"/> so the executor can answer "this device is not fully
/// managed" -- which reads, in the console, as something an operator can act on, unlike the
/// SecurityException that would otherwise arrive as "executor error: ...".
/// </summary>
public interface IDeviceSecurity
{
    /// <summary>Whether this device can lock or wipe itself at all. False on a device that is
    /// not a Device Owner.</summary>
    bool CanEnforce { get; }

    /// <summary>Lock the screen now. Returns whether it happened.
    ///
    /// The person holding the device unlocks it again with their own PIN, which is why this
    /// needs no confirmation at the hub end and no disclosure at this one: a locked screen is
    /// self-evident to the only person it affects.</summary>
    bool Lock();

    /// <summary>Erase the device. **This does not return.**
    ///
    /// <paramref name="clearResetProtection"/> asks the platform to clear factory-reset
    /// protection as part of the erase. Leaving it on means the device cannot be set up again
    /// without the account that was on it -- theft protection when the device was stolen, and
    /// a self-inflicted brick when it was a company handset with an ex-employee's account.
    /// The hub decides per request and says which it is doing; this only carries the answer.
    /// </summary>
    void Wipe(bool clearResetProtection);
}
