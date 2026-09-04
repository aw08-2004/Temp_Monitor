using TempMonitorAgent.Bios;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The firmware WRITE dispatch, against a recorded fake WMI invoker (roadmap #9).
///
/// Same constraint as <see cref="BiosReaderTests"/> and the same reason: a build machine is
/// none of these three vendors. What is asserted here is the part that is ours to get right --
/// which method gets called, on which class, with which argument SHAPE, and how each vendor's
/// return convention is read. Those are precisely the things that fail silently on real
/// hardware: Dell wants parallel arrays and rejects a bare string; HP wants its password
/// UTF-16-marked or a correct one is refused as wrong; Lenovo commits nothing at all without a
/// separate save, and returns "Success" from the write that changed nothing.
///
/// What this cannot cover is whether real firmware accepts what is sent. Expect the same shape
/// of first-contact finding the LDAP and TURN work produced.
/// </summary>
public class BiosWriterTests
{
    private sealed record Call(string Namespace, string Class, string Method,
                               IReadOnlyDictionary<string, object?> Args);

    /// <summary>A recording invoker. `results` maps a method name to what it returns; a
    /// method with no entry returns an empty bag, which is how "the interface answered
    /// nothing" is exercised.</summary>
    private static BiosWriter.WmiInvoke Fake(
        List<Call> log, Dictionary<string, Dictionary<string, object?>>? results = null)
        => (ns, cls, method, args) =>
        {
            log.Add(new Call(ns, cls, method, args));
            if (results is not null && results.TryGetValue(method, out var result))
                return result;
            return new Dictionary<string, object?>(StringComparer.OrdinalIgnoreCase);
        };

    private static Dictionary<string, Dictionary<string, object?>> Ok(params string[] methods)
    {
        var map = new Dictionary<string, Dictionary<string, object?>>();
        foreach (var method in methods)
        {
            map[method] = method.StartsWith("Save") || method == "SetBiosSetting"
                ? new Dictionary<string, object?> { ["return"] = "Success" }
                : new Dictionary<string, object?> { ["ReturnValue"] = (uint)0 };
        }
        return map;
    }

    /// <summary>A Dell with no stock-image provider: the biosattributes namespace is absent, so
    /// the writer must fall through to Command | Monitor. Written as a wrapper rather than a
    /// flag so that the ONLY thing sending a call down the legacy path is the exception the
    /// real adapter raises for a namespace that is not there.</summary>
    private static BiosWriter.WmiInvoke WithoutStockProvider(
        List<Call> log, Dictionary<string, Dictionary<string, object?>>? results = null)
    {
        var inner = Fake(log, results);
        return (ns, cls, method, args) =>
            ns.Contains("biosattributes", StringComparison.OrdinalIgnoreCase)
                ? throw new BiosInterfaceMissingException($"{ns} is not present")
                : inner(ns, cls, method, args);
    }

    // ------------------------------------------------------------------ dispatch

    [Fact]
    public void An_unknown_manufacturer_fails_every_attribute_rather_than_throwing()
    {
        var results = BiosWriter.Write("Acme Computers", [("WakeOnLan", "LanOnly")], null,
                                       Fake([]));
        // Reported per attribute, in the same shape a refused write uses, so the hub records
        // one vocabulary of failure rather than two.
        Assert.Single(results);
        Assert.False(results[0].Ok);
        Assert.Contains("Acme", results[0].Error);
    }

    [Fact]
    public void A_missing_manufacturer_says_so_rather_than_naming_an_empty_vendor()
    {
        var results = BiosWriter.Write("", [("WakeOnLan", "LanOnly")], null, Fake([]));
        Assert.Contains("no manufacturer", results[0].Error);
    }

    // ------------------------------------------------------------------ Dell

    [Fact]
    public void Dell_writes_through_the_stock_image_provider_first()
    {
        // Mirrors DellBiosSource: BIOSAttributeInterface is what a stock Dell business image
        // has, and aiming only at Command | Monitor's DCIM_BIOSService left every un-managed
        // Dell answering "DCIM_BIOSService has no instance" to a write whose settings the
        // console had just listed back from the other namespace.
        var log = new List<Call>();
        var results = BiosWriter.Write("Dell Inc.", [("WakeOnLan", "LanOnly")], null,
                                       Fake(log, Ok("SetAttribute")));

        Assert.True(results[0].Ok);
        var call = Assert.Single(log);
        Assert.Equal(@"root\dcim\sysman\biosattributes", call.Namespace);
        Assert.Equal("BIOSAttributeInterface", call.Class);
        Assert.Equal("SetAttribute", call.Method);
        // Scalars here, where Command | Monitor wants parallel arrays.
        Assert.Equal("WakeOnLan", call.Args["AttributeName"]);
        Assert.Equal("LanOnly", call.Args["AttributeValue"]);
    }

    [Fact]
    public void Dell_says_NONE_rather_than_an_empty_password_when_there_is_none()
    {
        // SecType is the provider's own ValueMap: 0 NONE, 1 PlainText. Sending 1 with no bytes
        // claims a supplied, empty password -- which fails as an authentication error rather
        // than as a missing one, and is the same trap the Command | Monitor branch documents
        // about an empty AuthorizationToken.
        var log = new List<Call>();
        BiosWriter.Write("Dell Inc.", [("WakeOnLan", "LanOnly")], null,
                         Fake(log, Ok("SetAttribute")));
        Assert.Equal(0u, log[0].Args["SecType"]);
        Assert.Empty(Assert.IsType<byte[]>(log[0].Args["SecHandle"]));
        Assert.Equal(0u, log[0].Args["SecHndCount"]);

        log.Clear();
        BiosWriter.Write("Dell Inc.", [("WakeOnLan", "LanOnly")], "pw",
                         Fake(log, Ok("SetAttribute")));
        Assert.Equal(1u, log[0].Args["SecType"]);
        // The password's BYTES, and a count that matches them -- the provider reads
        // SecHandle to SecHndCount, so a stale count truncates or overruns the password.
        Assert.Equal("pw"u8.ToArray(), Assert.IsType<byte[]>(log[0].Args["SecHandle"]));
        Assert.Equal(2u, log[0].Args["SecHndCount"]);
    }

    [Fact]
    public void Dell_falls_back_to_Command_Monitor_with_parallel_arrays_not_scalars()
    {
        var log = new List<Call>();
        var results = BiosWriter.Write("Dell Inc.", [("WakeOnLan", "LanOnly")], null,
                                       WithoutStockProvider(log, Ok("SetBIOSAttributes")));

        Assert.True(results[0].Ok);
        var call = Assert.Single(log);
        Assert.Equal(@"root\dcim\sysman", call.Namespace);
        Assert.Equal("DCIM_BIOSService", call.Class);
        Assert.Equal("SetBIOSAttributes", call.Method);
        // The shape is the assertion: a bare string here fails on real hardware with a type
        // error, which is not a message anyone would trace back to this line.
        Assert.Equal(new[] { "WakeOnLan" }, Assert.IsType<string[]>(call.Args["AttributeName"]));
        Assert.Equal(new[] { "LanOnly" }, Assert.IsType<string[]>(call.Args["AttributeValue"]));
    }

    [Fact]
    public void Dell_omits_the_token_entirely_when_there_is_no_password()
    {
        var log = new List<Call>();
        BiosWriter.Write("Dell Inc.", [("WakeOnLan", "LanOnly")], null,
                         WithoutStockProvider(log, Ok("SetBIOSAttributes")));
        // Not an empty string: some Dell models read "" as a SUPPLIED password and fail
        // authentication, turning "no password needed" into a refusal.
        Assert.False(log[0].Args.ContainsKey("AuthorizationToken"));

        log.Clear();
        BiosWriter.Write("Dell Inc.", [("WakeOnLan", "LanOnly")], "pw",
                         WithoutStockProvider(log, Ok("SetBIOSAttributes")));
        Assert.Equal("pw", log[0].Args["AuthorizationToken"]);
    }

    [Fact]
    public void A_nonzero_return_code_is_a_failure_and_carries_the_code()
    {
        var log = new List<Call>();
        var results = BiosWriter.Write("Dell Inc.", [("WakeOnLan", "LanOnly")], null,
            Fake(log, new Dictionary<string, Dictionary<string, object?>>
            {
                // `Status` on the stock provider, `SetResult`/`ReturnValue` on Command |
                // Monitor -- one more property name the two do not share.
                ["SetAttribute"] = new() { ["Status"] = 5 },
            }));
        Assert.False(results[0].Ok);
        // Verbatim rather than mapped to prose: the meanings differ per vendor, per model and
        // per firmware revision, so a friendly translation would be wrong somewhere.
        Assert.Contains("5", results[0].Error);
    }

    [Fact]
    public void No_return_value_at_all_is_an_error_never_an_assumed_success()
    {
        // The whole point of the write half is not claiming a change we cannot see. A silent
        // interface is the one case where assuming success would be most tempting.
        var results = BiosWriter.Write("Dell Inc.", [("WakeOnLan", "LanOnly")], null, Fake([]));
        Assert.False(results[0].Ok);
    }

    [Fact]
    public void A_Dell_with_neither_interface_reports_the_fallback_s_own_absence()
    {
        // The fallback is entered on a missing namespace and its exception is NOT caught
        // again, so what reaches the operator is the message from the interface tried LAST.
        // (The reader names both instead -- see DellBiosSource.Read for why the two halves
        // answer this differently on purpose.)
        var results = BiosWriter.Write("Dell Inc.", [("WakeOnLan", "LanOnly")], null,
            (ns, cls, method, args) =>
                throw new BiosInterfaceMissingException($"{ns} is not present"));
        Assert.False(results[0].Ok);

        // Asserted by EQUALITY, and with the stock namespace named as the thing that must be
        // absent from it. `Contains(@"root\dcim\sysman", ...)` -- the obvious spelling, and
        // what this test shipped with -- proves nothing at all here:
        // root\dcim\sysman\biosattributes has root\dcim\sysman as a PREFIX, so that assertion
        // passes whichever of the two messages arrives, including the first-tried one this
        // test exists to rule out. A substring check between two names where one contains the
        // other is not a check.
        Assert.Equal(@"root\dcim\sysman is not present", results[0].Error);
        Assert.DoesNotContain("biosattributes", results[0].Error);
    }

    [Fact]
    public void A_write_is_never_retried_on_a_failure_that_is_not_a_missing_namespace()
    {
        // The fallback exists for "this namespace is not here", which is raised before
        // anything is invoked. Retrying on any other error could apply a firmware change
        // TWICE -- once through each interface -- on a fault that happened after the write
        // had already landed.
        var calls = 0;
        var results = BiosWriter.Write("Dell Inc.", [("WakeOnLan", "LanOnly")], null,
            (ns, cls, method, args) =>
            {
                calls++;
                throw new InvalidOperationException("the firmware is busy");
            });
        Assert.Equal(1, calls);
        Assert.False(results[0].Ok);
    }

    // ------------------------------------------------------------------ HP

    [Fact]
    public void Hp_marks_its_password_as_utf16_and_sends_an_empty_one_when_unset()
    {
        var log = new List<Call>();
        BiosWriter.Write("HP", [("Wake On LAN", "Boot to Hard Drive")], "s3cret",
                         Fake(log, Ok("SetBIOSSetting")));
        Assert.Equal(@"root\hp\instrumentedBIOS", log[0].Namespace);
        Assert.Equal("HP_BIOSSettingInterface", log[0].Class);
        // Without the marker HP rejects a CORRECT password as wrong, which looks exactly like
        // the operator typing it in wrong.
        Assert.Equal("<utf-16/>s3cret", log[0].Args["Password"]);

        log.Clear();
        BiosWriter.Write("Hewlett-Packard", [("Wake On LAN", "Disable")], null,
                         Fake(log, Ok("SetBIOSSetting")));
        // Unlike Dell, HP wants the parameter present and empty.
        Assert.Equal("", log[0].Args["Password"]);
    }

    // ------------------------------------------------------------------ Lenovo

    [Fact]
    public void Lenovo_writes_one_string_and_then_commits()
    {
        var log = new List<Call>();
        var results = BiosWriter.Write("LENOVO", [("WakeOnLAN", "Enable")], null,
                                       Fake(log, Ok("SetBiosSetting", "SaveBiosSettings")));
        Assert.True(results[0].Ok);
        Assert.Equal(2, log.Count);
        Assert.Equal("WakeOnLAN,Enable", log[0].Args["parameter"]);
        // The trap this test exists for: without the save, Lenovo returns Success from a write
        // that changed nothing at all.
        Assert.Equal("Lenovo_SaveBiosSettings", log[1].Class);
    }

    [Fact]
    public void A_failed_lenovo_commit_invalidates_the_writes_that_thought_they_succeeded()
    {
        var log = new List<Call>();
        var results = BiosWriter.Write("Lenovo", [("WakeOnLAN", "Enable")], null,
            Fake(log, new Dictionary<string, Dictionary<string, object?>>
            {
                ["SetBiosSetting"] = new() { ["return"] = "Success" },
                ["SaveBiosSettings"] = new() { ["return"] = "Access Denied" },
            }));
        // The commit is what makes any of them real, so none of them is.
        Assert.False(results[0].Ok);
        Assert.Contains("not saved", results[0].Error);
        Assert.Contains("Access Denied", results[0].Error);
    }

    [Fact]
    public void Lenovos_password_argument_carries_its_encoding_and_layout()
    {
        var log = new List<Call>();
        BiosWriter.Write("Lenovo", [("WakeOnLAN", "Enable")], "pw",
                         Fake(log, Ok("SetBiosSetting", "SaveBiosSettings")));
        // ascii + us specifically: the alternative is keyboard-layout dependent, and this runs
        // from a service with no session and no idea what keyboard the machine has.
        Assert.Equal("pw,ascii,us", log[1].Args["parameter"]);
    }

    [Fact]
    public void A_comma_is_refused_rather_than_silently_writing_the_wrong_attribute()
    {
        var log = new List<Call>();
        var results = BiosWriter.Write("Lenovo", [("Owner", "Smith, John")], null, Fake(log));
        // Lenovo's format defines no escape, so a comma in the value would be read as the
        // separator -- writing an attribute nobody asked for.
        Assert.False(results[0].Ok);
        Assert.Empty(log);
    }

    // ------------------------------------------------------------------ partial application

    [Fact]
    public void One_refusal_does_not_stop_the_other_attributes()
    {
        var seen = 0;
        BiosWriter.WmiInvoke invoke = (ns, cls, method, args) =>
        {
            seen++;
            // The second write is refused; the third must still be attempted.
            return new Dictionary<string, object?> { ["ReturnValue"] = (uint)(seen == 2 ? 5 : 0) };
        };

        var results = BiosWriter.Write("Dell", [("A", "1"), ("B", "2"), ("C", "3")], null, invoke);
        Assert.Equal(3, results.Count);
        Assert.True(results[0].Ok);
        Assert.False(results[1].Ok);
        // An operator who ticked three settings is better served by two landing and a named
        // failure than by a half-applied change whose boundary depends on iteration order.
        Assert.True(results[2].Ok);
    }

    [Fact]
    public void A_throwing_interface_becomes_a_named_failure_not_an_escaped_exception()
    {
        var results = BiosWriter.Write("Dell", [("A", "1")], null,
            (ns, cls, method, args) => throw new InvalidOperationException("WMI is unwell"));
        Assert.False(results[0].Ok);
        Assert.Contains("WMI is unwell", results[0].Error);
    }
}
