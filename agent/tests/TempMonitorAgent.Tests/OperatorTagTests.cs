using TempMonitorAgent.Fleet;

namespace TempMonitorAgent.Tests;

/// <summary>
/// The agent log is readable by BUILTIN\Users (StateDirectory.Harden leaves it that way on
/// purpose, so support can open it), so what the shell paths write about an operator is a
/// privacy boundary rather than cosmetics. These pin both halves of the trade: the address
/// never reaches the log, and the tag is stable enough that the log stays followable.
/// </summary>
public class OperatorTagTests
{
    private const string Email = "Alice.Smith@example.com";

    [Fact]
    public void The_tag_does_not_carry_the_address_it_was_made_from()
    {
        var tag = OperatorTag.For(Email);

        Assert.DoesNotContain("alice", tag, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("example", tag, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("@", tag, StringComparison.Ordinal);
    }

    /// <summary>A log you cannot follow across lines would be no better than dropping the
    /// field, so stability is the half of this that earns the tag its place.</summary>
    [Fact]
    public void The_same_operator_tags_the_same_way_every_time()
    {
        Assert.Equal(OperatorTag.For(Email), OperatorTag.For(Email));
    }

    /// <summary>ShellSessionManager.Key trims and lowercases before it keys a session, so a
    /// tag that did not would read one person as two the moment a command arrived with the
    /// address cased differently.</summary>
    [Theory]
    [InlineData("alice.smith@example.com")]
    [InlineData("ALICE.SMITH@EXAMPLE.COM")]
    [InlineData("  Alice.Smith@example.com  ")]
    public void Case_and_padding_do_not_split_one_operator_into_several(string variant)
    {
        Assert.Equal(OperatorTag.For(Email), OperatorTag.For(variant));
    }

    [Fact]
    public void Different_operators_get_different_tags()
    {
        Assert.NotEqual(OperatorTag.For(Email), OperatorTag.For("bob@example.com"));
    }

    /// <summary>The hub sets issued_by from the trusted session, so a missing one means a
    /// locally-issued or malformed command -- it must still log as something, not throw.</summary>
    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("   ")]
    public void A_missing_issuer_is_reported_rather_than_thrown_on(string? missing)
    {
        Assert.Equal(OperatorTag.Unknown, OperatorTag.For(missing));
    }
}
