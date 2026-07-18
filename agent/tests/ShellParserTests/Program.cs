using System.Text;
using TempMonitorAgent.Fleet.Shell;

// Hand-rolled assertions in the spirit of the repo's Python tests (check(name, cond)).
// Exercises SubmissionParser: the dual-stream completion rule, marker-split hold-back, and
// exit-code/cwd parsing -- the parts most likely to break silently.

int pass = 0, fail = 0;
void Check(string name, bool cond)
{
    if (cond) { pass++; Console.WriteLine($"  [ok] {name}"); }
    else { fail++; Console.WriteLine($"  [XX] {name}"); }
}

const string M = "TMDONE_abc123";

// --- Happy path: output on both streams, marker last, meta on stdout ---
{
    var emitted = new StringBuilder();
    var p = new SubmissionParser(M, t => emitted.Append(t));
    p.FeedStdout("hello\nworld\n");
    Check("not complete until marker on both streams", !p.Complete);
    p.FeedStderr($"a warning\n{M}\n");
    Check("still not complete with only stderr marker", !p.Complete);
    p.FeedStdout($"{M} 0 C:\\Windows\n");
    Check("complete once marker seen on both", p.Complete);
    Check("exit code parsed", p.ExitCode == 0);
    Check("cwd parsed", p.Cwd == "C:\\Windows");
    Check("stdout body emitted without marker", emitted.ToString().Contains("hello\nworld"));
    Check("stderr body emitted", emitted.ToString().Contains("a warning"));
    Check("marker text never emitted", !emitted.ToString().Contains(M));
}

// --- cwd with spaces (Program Files) is kept whole ---
{
    var p = new SubmissionParser(M, _ => { });
    p.FeedStderr($"{M}\n");
    p.FeedStdout($"{M} 3 C:\\Program Files\\App\n");
    Check("nonzero exit parsed", p.ExitCode == 3);
    Check("cwd with spaces kept whole", p.Cwd == "C:\\Program Files\\App");
}

// --- Marker split across two reads must not leak a partial marker into output ---
{
    var emitted = new StringBuilder();
    var p = new SubmissionParser(M, t => emitted.Append(t));
    var half = M.Length / 2;
    p.FeedStdout("data" + M[..half]);          // trailing partial marker
    Check("partial marker held back, not emitted", !emitted.ToString().Contains(M[..half]));
    Check("real content before it still emitted", emitted.ToString().Contains("data"));
    p.FeedStdout(M[half..] + " 0 C:\\x\n");     // completes the marker
    p.FeedStderr($"{M}\n");
    Check("completes after split marker", p.Complete);
    Check("no marker fragment leaked", !emitted.ToString().Contains("TMDONE"));
    Check("cwd parsed across the split", p.Cwd == "C:\\x");
}

// --- An interactive prompt (no trailing newline, unlike a marker) must stream at once ---
{
    var emitted = new StringBuilder();
    var p = new SubmissionParser(M, t => emitted.Append(t));
    p.FeedStdout("Continue? [Y/N] ");   // program now blocks on stdin; no more output coming
    Check("prompt streams immediately, not held back", emitted.ToString() == "Continue? [Y/N] ");
    Check("prompt did not complete the submission", !p.Complete);
}

// --- A stream that never sees the marker keeps the submission incomplete ---
{
    var p = new SubmissionParser(M, _ => { });
    p.FeedStdout($"{M} 0 C:\\x\n");
    Check("stdout-only marker is not completion", !p.Complete);
    Check("DrainResidual returns held-back text", p.DrainResidual() is not null);
}

// --- Non-numeric exit code degrades to null rather than throwing ---
{
    var p = new SubmissionParser(M, _ => { });
    p.FeedStderr($"{M}\n");
    p.FeedStdout($"{M} notanumber C:\\x\n");
    Check("bad exit code -> null", p.ExitCode is null);
    Check("cwd still parsed with bad exit code", p.Cwd == "C:\\x");
}

Console.WriteLine($"\n==== {pass} passed, {fail} failed ====");
return fail == 0 ? 0 : 1;
