/// The running client's version (roadmap #11).
///
/// **This constant is the source of truth**, and `release_client.py` reads it rather than
/// taking a version on the command line. Three things have to agree about which version
/// this is -- the compiled binary, `pubspec.yaml`, and the signed manifest the update
/// check compares against -- and a release script that accepted a version as an argument
/// is a release script that can be given the wrong one. The script keeps all three in step
/// and refuses to build if they have drifted.
///
/// Bump it with `python release_client.py --set-version X.Y.Z`, not by hand.
library;

const String clientVersion = '1.1.0';
