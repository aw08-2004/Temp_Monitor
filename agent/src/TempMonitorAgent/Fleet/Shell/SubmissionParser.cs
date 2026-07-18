using System.Text;

namespace TempMonitorAgent.Fleet.Shell;

/// <summary>
/// Watches one persistent shell's two output streams for a submission's dual-stream
/// sentinel, emitting everything before it as live output and pulling the trailing
/// metadata (exit code + cwd) off the stdout marker line.
///
/// Why a sentinel on BOTH streams: stdout and stderr are separate pipes that flush
/// independently, so stderr written just before a submission ends can arrive AFTER the
/// stdout marker. Waiting until the marker has been seen on stdout AND stderr guarantees
/// both pipes are drained to the boundary (each pipe is individually ordered). The exact
/// framing this parses is produced by ShellSession; see the validated protocol there.
///
/// This is deliberately free of any Process/IO so it can be unit-tested with synthetic
/// stream fragments. Feed stdout fragments to <see cref="FeedStdout"/> and stderr to
/// <see cref="FeedStderr"/> in any interleaving; when both have seen the marker,
/// <see cref="Complete"/> flips true and <see cref="ExitCode"/>/<see cref="Cwd"/> are set.
/// </summary>
public sealed class SubmissionParser
{
    private readonly string _marker;
    private readonly Action<string> _emit;

    // Hold-back buffers: text that can't yet be emitted because it might be the start of a
    // marker split across reads. We only release up to the last (marker.Length - 1) chars.
    private readonly StringBuilder _outCarry = new();
    private readonly StringBuilder _errCarry = new();

    private bool _outMarkerSeen;
    private bool _errMarkerSeen;

    public SubmissionParser(string marker, Action<string> emit)
    {
        _marker = marker;
        _emit = emit;
    }

    /// <summary>True once the marker has been seen on both streams -- the submission's
    /// output is fully drained and <see cref="ExitCode"/>/<see cref="Cwd"/> are final.</summary>
    public bool Complete => _outMarkerSeen && _errMarkerSeen;

    /// <summary>Exit code parsed from the stdout marker line (null until seen).</summary>
    public int? ExitCode { get; private set; }

    /// <summary>Working directory parsed from the stdout marker line (null until seen).</summary>
    public string? Cwd { get; private set; }

    public void FeedStdout(string fragment) => FeedStream(fragment, _outCarry, isStdout: true);

    public void FeedStderr(string fragment) => FeedStream(fragment, _errCarry, isStdout: false);

    private void FeedStream(string fragment, StringBuilder carry, bool isStdout)
    {
        if ((isStdout ? _outMarkerSeen : _errMarkerSeen) || fragment.Length == 0) return;

        carry.Append(fragment);
        var text = carry.ToString();

        var idx = text.IndexOf(_marker, StringComparison.Ordinal);
        if (idx < 0)
        {
            // No complete marker yet. Hold back ONLY a trailing run that could be the start of
            // the marker split across reads; emit everything else immediately. Reserving a
            // fixed marker-length tail instead would swallow an interactive prompt that has no
            // trailing newline (e.g. "Continue? [Y/N] ") until the operator answered it.
            var hold = MarkerPrefixOverlap(text);
            var safe = text.Length - hold;
            if (safe > 0)
            {
                _emit(text[..safe]);
                carry.Remove(0, safe);
            }
            return;
        }

        // Marker found: emit only what precedes it, then stop consuming this stream.
        if (idx > 0) _emit(text[..idx]);
        carry.Clear();

        if (isStdout)
        {
            // The stdout marker carries "<marker> <exitcode> <cwd>" on one line.
            ParseMeta(text[(idx + _marker.Length)..]);
            _outMarkerSeen = true;
        }
        else
        {
            _errMarkerSeen = true;
        }
    }

    /// <summary>Length of the longest suffix of <paramref name="text"/> that is also a prefix
    /// of the marker -- i.e. how much of a possibly-split marker sits at the tail. 0 when the
    /// tail can't be the start of a marker, so ordinary output streams without delay.</summary>
    private int MarkerPrefixOverlap(string text)
    {
        var max = Math.Min(text.Length, _marker.Length - 1);
        for (var k = max; k > 0; k--)
            if (string.CompareOrdinal(text, text.Length - k, _marker, 0, k) == 0)
                return k;
        return 0;
    }

    private void ParseMeta(string afterMarker)
    {
        // afterMarker looks like " <exitcode> <cwd...>\r\n". cwd can contain spaces, so split
        // off only the exit-code token and keep the remainder verbatim.
        var line = afterMarker;
        var nl = line.IndexOf('\n');
        if (nl >= 0) line = line[..nl];
        line = line.Trim('\r', ' ', '\t');

        var sp = line.IndexOf(' ');
        var ecToken = sp < 0 ? line : line[..sp];
        var cwd = sp < 0 ? "" : line[(sp + 1)..].Trim();

        ExitCode = int.TryParse(ecToken, out var ec) ? ec : null;
        Cwd = cwd.Length > 0 ? cwd : null;
    }

    /// <summary>Flush any held-back text on give-up (timeout/shell death) so the operator
    /// still sees the tail. Safe to call once; returns the residual from both streams.</summary>
    public string DrainResidual()
    {
        var s = _outCarry.ToString() + _errCarry.ToString();
        _outCarry.Clear();
        _errCarry.Clear();
        return s;
    }
}
