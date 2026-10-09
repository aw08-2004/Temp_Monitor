"""The hub-relayed fallback for a remote session (roadmap #2): media and input carried over
plain HTTPS through the hub when WebRTC cannot find a path at all.

**Why this exists.** A session's media normally flows peer-to-peer or through the coturn relay,
and the TURN half of that is the most fragile thing the hub deploys: a WSL2 distro, a boot task,
mirrored networking, two firewalls and a secret that has to match in two places (see the
field-test findings in ROADMAP.MD #2). When any of that is down, every session fails with "ICE
never found a working path" -- while the operator's browser is, by definition, already talking
to the hub over HTTPS, and so is the agent. This module is the path that is left when the
relay is not: the agent POSTs its already-encoded frames here, the browser long-polls them back
out and decodes them with WebCodecs, and input travels the other way through a second queue.

**What it costs, and why it is a fallback and not the default.** Every frame crosses the hub
twice (in from the agent, out to the browser), so a 4 Mbps session is 8 Mbps of hub bandwidth
and two of its request threads, and latency is one HTTP round trip more than a direct path.
And the hub sees the picture in the clear: SRTP through coturn is end-to-end encrypted between
the two peers, HTTPS through here is encrypted only to the hub. That is the same hub that
already holds the agent's bearer token and can start a session on any PC in scope, so it gains
no capability it did not have -- but it is the reason the switch is recorded in the audit log,
and the reason it happens only after WebRTC has actually failed.

**State is in memory, deliberately.** A relay is a few seconds of video and a queue of mouse
moves, and none of it is worth surviving a hub restart -- a restart ends the session's helper
signaling anyway. Keeping it out of SQLite means a 25 fps stream is not 25 database writes a
second through the single `db_writer`. It relies on the hub being ONE process, which it is (the
in-memory sensor caches in app.py rely on the same thing); a multi-worker deployment would need
this moved, and the per-session `open()` would be where that shows first.

Kept free of Flask so it can be unit-tested in isolation; remote_web.py wires the routes, and
owns every authorization decision -- nothing here knows who is asking.

Rejected alternatives, each tried on paper first:
  * **One long streaming response** (chunked, `multipart/x-mixed-replace`) instead of
    long-polls. Lower latency on paper, but the hub sits behind a TLS terminator that is free to
    buffer a response it has not seen the end of, and a buffered stream is a frozen screen with
    no error anywhere. A long-poll that returns whatever has arrived is immune to that.
  * **Socket.IO.** The hub's Socket.IO is configured polling-only, so it would be long-polls
    anyway, with base64 framing on top of binary frames.
  * **MJPEG / re-encoding on the hub.** The agent already produces H.264 (or VP8), and every
    browser the console supports can decode that with WebCodecs. Re-encoding would put a video
    encoder in the hub process for no gain.
"""
import collections
import json
import struct
import threading
import time

# Record kinds on the wire, both directions of the down stream share one framing.
KIND_VIDEO = 1
KIND_CONTROL = 2
_KINDS = frozenset({KIND_VIDEO, KIND_CONTROL})

FLAG_KEYFRAME = 0x01

# [kind:1][flags:1][length:4 big-endian][payload]
_HEADER = struct.Struct(">BBI")

#: Largest single record we will take from an agent. A 4K keyframe at a generous bitrate is
#: well under this; anything bigger is a bug or an attack, not a picture.
MAX_RECORD_BYTES = 4 * 1024 * 1024

#: Largest single upload. The agent batches whatever queued while its previous POST was in
#: flight, so a slow uplink produces bigger batches, not more of them.
MAX_UPLOAD_BYTES = 16 * 1024 * 1024

#: Down-stream buffer per session. Bounded by count AND bytes: count bounds a burst of tiny
#: delta frames, bytes bounds a run of keyframes. Old records fall off the front; a viewer that
#: falls behind the front is resynchronised to the newest keyframe (see read_down).
MAX_DOWN_RECORDS = 240
MAX_DOWN_BYTES = 24 * 1024 * 1024

#: Up-stream (input) queue per session. Mouse moves are already throttled to ~25/s in the
#: browser, so this is minutes of input; hitting it means the agent stopped reading.
MAX_UP_MESSAGES = 1000
MAX_UP_MESSAGE_BYTES = 4096

#: How long one long-poll waits for something to arrive before returning empty. Short enough
#: to sit well inside any proxy's idle timeout, long enough that an idle screen is a handful of
#: requests a minute rather than a busy loop.
DEFAULT_WAIT_SECONDS = 8.0

#: A relay nobody has touched for this long is dropped on the next open(). Sessions end
#: through remote_web.py's stop/ended routes, which close their relay; this is the backstop for
#: the tab that vanished and the agent that died.
IDLE_DROP_SECONDS = 10 * 60


class Relay:
    """One session's two queues. All access goes through `cond`."""

    def __init__(self, session_id, machine):
        self.session_id = session_id
        self.machine = machine
        self.cond = threading.Condition()
        self.down = collections.deque()   # (seq, kind, flags, payload)
        self.down_bytes = 0
        self.down_next = 1
        self.up = collections.deque()     # (seq, message_json_str)
        self.up_next = 1
        # The newest geometry report. Kept aside because a viewer resynchronised to a keyframe
        # would otherwise skip the `geom` record the agent sent just BEFORE that keyframe, and
        # without it the viewer has no capture size to map clicks against and no monitor list.
        self.last_geom = None
        self.closed = False
        self.opened_at = time.time()
        self.last_activity = self.opened_at
        self.bytes_in = 0

    def _touch(self):
        self.last_activity = time.time()


_registry = {}
_registry_lock = threading.Lock()


def open_relay(session_id, machine):
    """Create (or return) the relay for a session. Idempotent -- the console may ask twice if
    its first request timed out -- and the place idle relays are swept, since every new one is
    a moment someone is paying attention to relay memory."""
    session_id = str(session_id)
    now = time.time()
    with _registry_lock:
        for sid in [s for s, r in _registry.items()
                    if r.closed or now - r.last_activity > IDLE_DROP_SECONDS]:
            _drop_locked(sid)
        relay = _registry.get(session_id)
        if relay is None:
            relay = Relay(session_id, str(machine))
            _registry[session_id] = relay
        return relay


def get_relay(session_id):
    """The open relay for a session, or None."""
    with _registry_lock:
        relay = _registry.get(str(session_id))
    if relay is None or relay.closed:
        return None
    return relay


def close_relay(session_id):
    """Close a session's relay and wake anyone waiting on it, so a long-poll returns at once
    with the session's end instead of sitting out its timeout. Returns True if one was open."""
    with _registry_lock:
        return _drop_locked(str(session_id))


def _drop_locked(session_id):
    relay = _registry.pop(session_id, None)
    if relay is None:
        return False
    with relay.cond:
        relay.closed = True
        relay.down.clear()
        relay.up.clear()
        relay.cond.notify_all()
    return True


def active_count():
    """How many relays are open -- for the Remote settings status card."""
    with _registry_lock:
        return sum(1 for r in _registry.values() if not r.closed)


# --------------------------------------------------------------------------- framing
def parse_records(body):
    """Split an agent upload into (kind, flags, payload) records.

    Raises ValueError on anything malformed -- a truncated header, a length that runs past the
    end, an unknown kind, an oversized record. The whole upload is refused rather than the good
    prefix kept: a stream that lost a frame in the middle decodes as garbage until the next
    keyframe, and the agent will re-key after a refused upload anyway.
    """
    if not isinstance(body, (bytes, bytearray, memoryview)):
        raise ValueError("upload must be bytes")
    view = memoryview(body)
    if len(view) > MAX_UPLOAD_BYTES:
        raise ValueError("upload too large")
    records = []
    offset = 0
    while offset < len(view):
        if len(view) - offset < _HEADER.size:
            raise ValueError("truncated record header")
        kind, flags, length = _HEADER.unpack_from(view, offset)
        offset += _HEADER.size
        if kind not in _KINDS:
            raise ValueError(f"unknown record kind {kind}")
        if length > MAX_RECORD_BYTES:
            raise ValueError("record too large")
        if len(view) - offset < length:
            raise ValueError("record runs past the end of the upload")
        records.append((kind, flags, bytes(view[offset:offset + length])))
        offset += length
    return records


def frame_records(records):
    """The inverse of parse_records: concatenate (kind, flags, payload) into one body."""
    parts = []
    for kind, flags, payload in records:
        parts.append(_HEADER.pack(kind, flags, len(payload)))
        parts.append(payload)
    return b"".join(parts)


def _is_geom(payload):
    """True for an agent `geom` control record. A byte test rather than a JSON parse: this runs
    for every control record on the upload path, and the agent's serializer writes the field
    first and without spaces, which is the one shape we need to recognise."""
    return payload.startswith(b'{"t":"geom"')


# --------------------------------------------------------------------------- down stream
def push_down(session_id, records):
    """Append agent records to a session's down stream. Returns how many were stored, or None
    if the relay is not open (the caller answers 409 -- the console never asked for a relay, or
    it has been closed)."""
    relay = get_relay(session_id)
    if relay is None:
        return None
    with relay.cond:
        if relay.closed:
            return None
        for kind, flags, payload in records:
            relay.down.append((relay.down_next, kind, flags, payload))
            relay.down_next += 1
            relay.down_bytes += len(payload)
            relay.bytes_in += len(payload)
            if kind == KIND_CONTROL and _is_geom(payload):
                relay.last_geom = payload
        while relay.down and (len(relay.down) > MAX_DOWN_RECORDS
                              or relay.down_bytes > MAX_DOWN_BYTES):
            _, _, _, old = relay.down.popleft()
            relay.down_bytes -= len(old)
        relay._touch()
        relay.cond.notify_all()
    return len(records)


def _start_index(relay, after):
    """Where a viewer whose cursor is `after` should start reading, and whether that is a
    resync. Must be called with relay.cond held.

    Three cases:
      * `after` is inside the buffer -> continue straight on from it.
      * `after` is 0 (a new viewer) or has fallen off the front (a viewer that stalled) ->
        start at the NEWEST keyframe, because a decoder can only begin at one and every frame
        before the newest is latency nobody wants on a remote-control screen.
      * no keyframe buffered yet -> start at the front and let the browser discard deltas until
        the keyframe arrives (the agent re-keys on relay open, so this is brief).
    """
    if not relay.down:
        return 0, False
    oldest = relay.down[0][0]
    if after >= oldest - 1 and after != 0:
        return max(0, after - oldest + 1), False
    for i in range(len(relay.down) - 1, -1, -1):
        _, kind, flags, _ = relay.down[i]
        if kind == KIND_VIDEO and flags & FLAG_KEYFRAME:
            return i, True
    return 0, True


def read_down(session_id, after, wait_seconds=DEFAULT_WAIT_SECONDS, max_bytes=MAX_UPLOAD_BYTES):
    """Long-poll a session's down stream for records newer than `after`.

    Returns (records, next_cursor, state), where state is "open" or "closed". Records are
    (kind, flags, payload). Waits up to `wait_seconds` for something to arrive; an empty list
    with state "open" just means "ask again".
    """
    try:
        after = max(0, int(after))
    except (TypeError, ValueError):
        after = 0
    relay = get_relay(session_id)
    if relay is None:
        return [], after, "closed"
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    with relay.cond:
        while True:
            if relay.closed:
                return [], after, "closed"
            if relay.down and relay.down[-1][0] > after:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                relay._touch()
                return [], after, "open"
            relay.cond.wait(remaining)

        index, resync = _start_index(relay, after)
        out, size = [], 0
        if resync and relay.last_geom is not None:
            # Put the geometry first so the viewer knows the picture's size before its first
            # frame -- see Relay.last_geom.
            out.append((KIND_CONTROL, 0, relay.last_geom))
            size += len(relay.last_geom)
        cursor = after
        for seq, kind, flags, payload in list(relay.down)[index:]:
            if out and size + len(payload) > max_bytes:
                break
            out.append((kind, flags, payload))
            size += len(payload)
            cursor = seq
        relay._touch()
        return out, cursor, "open"


# --------------------------------------------------------------------------- up stream
def push_up(session_id, messages):
    """Queue console input/config messages for the agent. `messages` is a list of dicts; each
    is serialised once here so the agent receives exactly the JSON the WebRTC control channel
    would have carried. Returns how many were queued, or None if the relay is not open.

    Raises ValueError for a message that is not an object or is oversized -- input arrives from
    a browser, and a 1 MB "mouse move" is not one.
    """
    encoded = []
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("t"), str):
            raise ValueError("each control message must be an object with a string 't'")
        text = json.dumps(message, separators=(",", ":"))
        if len(text) > MAX_UP_MESSAGE_BYTES:
            raise ValueError("control message too large")
        encoded.append(text)
    relay = get_relay(session_id)
    if relay is None:
        return None
    with relay.cond:
        if relay.closed:
            return None
        for text in encoded:
            relay.up.append((relay.up_next, text))
            relay.up_next += 1
        while len(relay.up) > MAX_UP_MESSAGES:
            relay.up.popleft()
        relay._touch()
        relay.cond.notify_all()
    return len(encoded)


def read_up(session_id, after, wait_seconds=DEFAULT_WAIT_SECONDS):
    """Long-poll a session's up stream (the agent's side). Returns (messages, next_cursor,
    state) where messages are JSON strings, oldest first. Unlike the down stream there is no
    resync: input is never skipped to catch up, because a dropped key-up is a key held down."""
    try:
        after = max(0, int(after))
    except (TypeError, ValueError):
        after = 0
    relay = get_relay(session_id)
    if relay is None:
        return [], after, "closed"
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    with relay.cond:
        while True:
            if relay.closed:
                return [], after, "closed"
            if relay.up and relay.up[-1][0] > after:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                relay._touch()
                return [], after, "open"
            relay.cond.wait(remaining)
        out = [(seq, text) for seq, text in relay.up if seq > after]
        relay._touch()
        return [text for _, text in out], out[-1][0], "open"
