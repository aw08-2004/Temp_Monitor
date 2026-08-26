using TempMonitorAgent.Files;
using TempMonitorAgent.Fleet;

namespace TempMonitorAgent.Tests;

/// <summary>
/// What open_item actually hands to Windows.
///
/// The interesting one is the batch file, and it is a real cmd.exe rather than a string
/// comparison on purpose: the bug this guards against is not in what the agent writes but in
/// how cmd.exe re-reads it, so an assertion about the string would have passed happily on the
/// version that ran the attacker's command. These tests build the command line the executor
/// would use and then let cmd.exe parse it.
/// </summary>
public class OpenItemExecutorTests : IDisposable
{
    private readonly string _dir = Path.Combine(
        Path.GetTempPath(), "openitem-" + Guid.NewGuid().ToString("N"));

    public OpenItemExecutorTests() => Directory.CreateDirectory(_dir);

    public void Dispose()
    {
        try { Directory.Delete(_dir, recursive: true); } catch { /* a temp dir */ }
        GC.SuppressFinalize(this);
    }

    /// <summary>The whole reason CmdQuoted exists.
    ///
    /// `&amp;` is legal in a Windows filename, and cmd.exe's /c parsing treats it as a command
    /// separator unless the quoting is exactly right -- so a file dropped on a machine under
    /// this name would run its own second command, in whichever account the operator picked,
    /// while the audit record still named only the file they clicked.
    /// </summary>
    [Fact]
    public async Task ABatchFileNamedWithAnAmpersandRunsOnlyItself()
    {
        var path = Path.Combine(_dir, "report & echo INJECTED-COMMAND-RAN #.bat");
        await File.WriteAllTextAsync(path, "@echo BATCH-FILE-CONTENTS-RAN\r\n");

        var (program, arguments, handsOff) = OpenItemExecutor.Resolve(path, isDirectory: false);
        Assert.Equal("cmd.exe", Path.GetFileName(program));
        Assert.False(handsOff);

        var outcome = await ProcessRunner.RunAsync(program, arguments, CancellationToken.None,
                                                   timeoutSeconds: 30, workingDir: _dir);

        Assert.Contains("BATCH-FILE-CONTENTS-RAN", outcome.Output);
        Assert.DoesNotContain("INJECTED-COMMAND-RAN", outcome.Output);
    }

    /// <summary>The ordinary case still has to work: the doubled quotes must not stop cmd.exe
    /// from finding a path with a space in it, which is most of them.</summary>
    [Fact]
    public async Task AnOrdinaryBatchFileStillRuns()
    {
        var path = Path.Combine(_dir, "quarterly report.bat");
        await File.WriteAllTextAsync(path, "@echo PLAIN-BATCH-RAN\r\n");

        var (program, arguments, _) = OpenItemExecutor.Resolve(path, isDirectory: false);
        var outcome = await ProcessRunner.RunAsync(program, arguments, CancellationToken.None,
                                                   timeoutSeconds: 30, workingDir: _dir);

        Assert.Contains("PLAIN-BATCH-RAN", outcome.Output);
    }

    /// <summary>A program is started as itself -- no interpreter, and therefore no second
    /// parser between the operator's click and the process.</summary>
    [Fact]
    public void AProgramIsLaunchedDirectly()
    {
        var path = Path.Combine(_dir, "setup & tool.exe");
        var (program, arguments, handsOff) = OpenItemExecutor.Resolve(path, isDirectory: false);

        Assert.Equal(path, program);
        Assert.Equal("", arguments);
        Assert.False(handsOff);
    }

    /// <summary>A document has no program of its own, so it goes to the shell in the user's
    /// session -- which is a handoff, and says so, because the pid it returns dies at once.</summary>
    [Fact]
    public void ADocumentGoesToExplorerAsAHandoff()
    {
        var path = Path.Combine(_dir, "invoice.pdf");
        var (program, arguments, handsOff) = OpenItemExecutor.Resolve(path, isDirectory: false);

        Assert.Equal("explorer.exe", Path.GetFileName(program));
        Assert.Equal($"\"{path}\"", arguments);
        Assert.True(handsOff);
    }

    [Fact]
    public void AFolderGoesToExplorerToo()
    {
        var (program, arguments, handsOff) = OpenItemExecutor.Resolve(_dir, isDirectory: true);

        Assert.Equal("explorer.exe", Path.GetFileName(program));
        Assert.Equal($"\"{_dir}\"", arguments);
        Assert.True(handsOff);
    }
}
