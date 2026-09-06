using System.Runtime.Versioning;

// Says out loud what the RuntimeIdentifier in the csproj only implies, and makes the compiler
// enforce it.
//
// The agent calls Unix-only APIs on purpose -- File.SetUnixFileMode and friends, which are how
// the enrollment token is kept readable only by root (see State/StateDirectory). Without this
// attribute those calls raise CA1416 on every build, because the analyzer has to assume a
// net10.0 assembly might run anywhere; with it, the analyzer knows the target and the warnings
// become the useful thing they were meant to be -- a real one would mean code that genuinely
// cannot run where this agent is deployed.
//
// An assembly attribute rather than an MSBuild property because there is no MSBuild property
// for this: a platform-specific TFM (net10.0-windows, net10.0-android) is what normally sets
// it, and there is no net10.0-linux.
[assembly: SupportedOSPlatform("linux")]
