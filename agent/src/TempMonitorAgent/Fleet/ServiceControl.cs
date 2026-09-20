using System.ServiceProcess;

namespace TempMonitorAgent.Fleet;

/// <summary>
/// Stop and start one Windows service properly, in ONE implementation.
///
/// **This was `RestartProcessExecutor.RestartService`, and it was lifted out here the moment
/// a second caller appeared** -- the agent-local watchdog loop (roadmap #20). A watchdog
/// restarting a service is the same act an operator's `restart_process` performs, and two
/// copies of it would have drifted on exactly the detail below that nobody looks at twice.
///
/// Dependents are restarted EXPLICITLY, because Windows does not do it. A plain stop/start of
/// the print spooler leaves everything that depended on it stopped, so an operator -- or a
/// watchdog acting unattended, which is worse -- restarting one service would silently have
/// turned two off.
///
/// Returns a result rather than throwing, and never throws for a service-control failure: a
/// caller that has to try again in thirty seconds should not be handling exceptions to decide
/// that, and the executor's own failure text is built from the same sentence.
/// </summary>
public static class ServiceControl
{
    private static readonly TimeSpan Wait = TimeSpan.FromSeconds(30);

    /// <summary>What a restart attempt did. <paramref name="Missing"/> is distinguished from a
    /// plain failure on purpose: a service that is not installed is a different problem from
    /// one that would not start, and the watchdog reports them as different statuses.</summary>
    public readonly record struct Result(bool Ok, bool Missing, string Summary);

    /// <summary>Is this service installed on this machine at all?</summary>
    public static bool Exists(string serviceName)
    {
        try
        {
            using var service = new ServiceController(serviceName);
            _ = service.Status;      // throws if there is no such service
            return true;
        }
        catch { return false; }
    }

    /// <summary>The service's current status, or null when there is no such service.</summary>
    public static ServiceControllerStatus? StatusOf(string serviceName)
    {
        try
        {
            using var service = new ServiceController(serviceName);
            return service.Status;
        }
        catch { return null; }
    }

    /// <summary>Stop the service if it is running, then start it, then bring back every
    /// dependent that was running before. A service that is already stopped is simply
    /// started, which is the watchdog's whole case.</summary>
    public static Result Restart(string serviceName)
    {
        try
        {
            using var service = new ServiceController(serviceName);
            var display = string.IsNullOrEmpty(service.DisplayName)
                ? serviceName : service.DisplayName;

            if (service.Status != ServiceControllerStatus.Stopped && !service.CanStop)
                return new Result(false, false,
                                  $"the {display} service does not accept a stop request");

            var dependents = new List<string>();
            foreach (var dependent in service.DependentServices)
            {
                using (dependent)
                {
                    if (dependent.Status != ServiceControllerStatus.Stopped)
                        dependents.Add(dependent.ServiceName);
                }
            }

            if (service.Status != ServiceControllerStatus.Stopped)
            {
                service.Stop(stopDependentServices: true);
                service.WaitForStatus(ServiceControllerStatus.Stopped, Wait);
            }
            service.Start();
            service.WaitForStatus(ServiceControllerStatus.Running, Wait);

            var restarted = new List<string>();
            var failed = new List<string>();
            foreach (var name in dependents)
            {
                try
                {
                    using var dependent = new ServiceController(name);
                    if (dependent.Status == ServiceControllerStatus.Stopped) dependent.Start();
                    dependent.WaitForStatus(ServiceControllerStatus.Running, Wait);
                    restarted.Add(name);
                }
                catch (Exception e)
                {
                    // The service the caller asked for IS running; a dependent that would not
                    // come back is a real problem but a different one, and it must be named
                    // rather than folded into a blanket failure.
                    failed.Add($"{name} ({e.Message})");
                }
            }

            var summary = $"restarted the {display} service";
            if (restarted.Count > 0) summary += $"; also restarted {string.Join(", ", restarted)}";
            if (failed.Count > 0)
                return new Result(false, false,
                    summary + $"; these dependents did NOT come back: {string.Join("; ", failed)}");
            return new Result(true, false, summary);
        }
        catch (InvalidOperationException e)
        {
            // What ServiceController throws for a name that is not installed -- and also for
            // some genuine control failures, so the inner exception is what tells them apart.
            // Getting this wrong would make a watchdog on a typo'd service name read as "the
            // machine is in trouble" rather than "nobody ever had that service".
            var missing = !Exists(serviceName);
            return new Result(false, missing,
                missing
                    ? $"there is no {serviceName} service on this machine"
                    : $"could not restart the {serviceName} service: {e.Message}");
        }
        catch (Exception e)
        {
            return new Result(false, false,
                              $"could not restart the {serviceName} service: {e.Message}");
        }
    }
}

/// <summary>
/// The service-control surface the watchdog runner uses, behind an interface so its state
/// machine can be tested.
///
/// **The interface exists for the tests and the design is better for it, but be clear about
/// which.** The flap limit, the grace period and the give-up-and-escalate decision are the
/// parts of roadmap #20 with interesting failure modes, and every one of them is about TIMING
/// against a service that is up or down. A test that has to stop a real Windows service, wait
/// out a real sixty-second grace period and hope the SCM cooperates is a test nobody runs, so
/// the alternative to this seam was no coverage of the only logic worth covering.
/// </summary>
public interface IServiceControl
{
    ServiceControllerStatus? StatusOf(string serviceName);
    ServiceControl.Result Restart(string serviceName);
}

/// <summary>The real thing: straight through to <see cref="ServiceControl"/>.</summary>
public sealed class WindowsServiceControl : IServiceControl
{
    public ServiceControllerStatus? StatusOf(string serviceName) =>
        ServiceControl.StatusOf(serviceName);

    public ServiceControl.Result Restart(string serviceName) =>
        ServiceControl.Restart(serviceName);
}
