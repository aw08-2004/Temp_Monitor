"""Unit tests for remote_relay.py, the hub-relayed fallback for a remote session (roadmap #2).

The silent failure this file exists to catch is **a relayed screen that stays black or frozen
while every request succeeds**: a viewer handed delta frames it cannot decode because the
buffer was not rewound to a keyframe, a viewer resynchronised past the `geom` record so clicks
map to nothing, or a long-poll that keeps waiting after its session closed. Each of those looks
healthy from the network tab -- 200s all round -- and only the picture says otherwise.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hub"))
import remote_relay as rr

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [XX] {name}")


def video(payload, key=False):
    return (rr.KIND_VIDEO, rr.FLAG_KEYFRAME if key else 0, payload)


def control(payload):
    return (rr.KIND_CONTROL, 0, payload)


def test_framing():
    print("framing")
    records = [video(b"\x00\x00\x01\x67abc", key=True), control(b'{"t":"geom","w":1}'),
               video(b"delta")]
    body = rr.frame_records(records)
    check("a framed upload parses back to the same records", rr.parse_records(body) == records)
    check("an empty upload is no records", rr.parse_records(b"") == [])
    # The same bytes HubRelayTests.Frame_MatchesTheLayoutTheHubParses pins on the agent side:
    # a change to either end's framing fails one of the two suites, not a live session.
    check("the byte layout matches the agent's RelayFraming",
          rr.frame_records([video(bytes([0xAA, 0xBB]), key=True), control(b"{}")])
          == bytes([1, 1, 0, 0, 0, 2, 0xAA, 0xBB, 2, 0, 0, 0, 0, 2, ord("{"), ord("}")]))

    def refused(body):
        try:
            rr.parse_records(body)
            return False
        except ValueError:
            return True

    check("a truncated header is refused", refused(body[:3]))
    check("a length that runs past the end is refused", refused(body[:-1]))
    check("an unknown record kind is refused", refused(b"\x09\x00\x00\x00\x00\x01x"))
    check("a record over the size cap is refused",
          refused(b"\x01\x00" + (rr.MAX_RECORD_BYTES + 1).to_bytes(4, "big")))


def test_requires_open():
    print("a relay exists only once opened")
    check("push_down into a session nobody switched over is refused",
          rr.push_down("never-opened", [video(b"x", key=True)]) is None)
    check("push_up likewise", rr.push_up("never-opened", [{"t": "m"}]) is None)
    records, cursor, state = rr.read_down("never-opened", 0, wait_seconds=0)
    check("reading an unopened relay answers closed at once", state == "closed" and not records)


def test_new_viewer_starts_at_newest_keyframe():
    print("a new viewer starts at the newest keyframe, with the geometry first")
    rr.open_relay("s1", "PC-01")
    rr.push_down("s1", [control(b'{"t":"geom","w":800,"h":600}'), video(b"K1", key=True),
                        video(b"d1"), video(b"d2"),
                        control(b'{"t":"geom","w":1920,"h":1080}'), video(b"K2", key=True),
                        video(b"d3")])
    records, cursor, state = rr.read_down("s1", 0, wait_seconds=0)
    payloads = [p for _, _, p in records]
    check("the first record is the NEWEST geometry, not the first one",
          payloads[0] == b'{"t":"geom","w":1920,"h":1080}')
    check("then the newest keyframe, so the decoder can begin", payloads[1] == b"K2")
    check("nothing older than that keyframe is sent", b"K1" not in payloads and b"d1" not in payloads)
    check("the delta after it follows", payloads[-1] == b"d3")
    check("the cursor is the last record handed out", cursor == 7 and state == "open")

    records, cursor2, _ = rr.read_down("s1", cursor, wait_seconds=0)
    check("a caught-up viewer gets nothing and keeps its cursor", records == [] and cursor2 == cursor)

    rr.push_down("s1", [video(b"d4")])
    records, cursor3, _ = rr.read_down("s1", cursor, wait_seconds=0)
    check("a viewer inside the buffer continues straight on, with no resync",
          [p for _, _, p in records] == [b"d4"] and cursor3 == 8)
    rr.close_relay("s1")


def test_fallen_behind_viewer_resyncs():
    print("a viewer that fell off the front is resynchronised, not fed undecodable deltas")
    rr.open_relay("s2", "PC-01")
    rr.push_down("s2", [video(b"K", key=True)])
    first, cursor, _ = rr.read_down("s2", 0, wait_seconds=0)
    # Overflow the buffer by count so the viewer's cursor falls off the front. Exactly
    # MAX_DOWN_RECORDS would not do it: the viewer's next record would still be the oldest one
    # buffered, which is a viewer that is behind, not one that has missed anything.
    flood = [video(b"d%d" % i) for i in range(rr.MAX_DOWN_RECORDS + 10)]
    flood[-5] = video(b"K-late", key=True)
    rr.push_down("s2", flood)
    records, _, _ = rr.read_down("s2", cursor, wait_seconds=0)
    payloads = [p for _, _, p in records]
    check("the resync starts at a keyframe", payloads and payloads[0] == b"K-late")
    check("and skips the deltas a decoder could not use", len(payloads) == 5)
    rr.close_relay("s2")


def test_bounded_buffer():
    print("the down buffer is bounded by bytes as well as count")
    rr.open_relay("s3", "PC-01")
    big = b"x" * (rr.MAX_DOWN_BYTES // 4 + 1)
    for _ in range(6):
        rr.push_down("s3", [video(big, key=True)])
    relay = rr.get_relay("s3")
    check("a run of large keyframes is trimmed under the byte cap",
          relay.down_bytes <= rr.MAX_DOWN_BYTES)
    check("but the newest keyframe always survives", relay.down and relay.down[-1][3] is big)
    rr.close_relay("s3")


def test_up_stream():
    print("input up to the agent")
    rr.open_relay("s4", "PC-01")
    check("a message must be an object with a string t", _raises(lambda: rr.push_up("s4", ["m"])))
    check("an oversized message is refused",
          _raises(lambda: rr.push_up("s4", [{"t": "k", "key": "x" * rr.MAX_UP_MESSAGE_BYTES}])))
    rr.push_up("s4", [{"t": "d", "b": 0, "x": 0.5, "y": 0.5}, {"t": "u", "b": 0}])
    messages, cursor, state = rr.read_up("s4", 0, wait_seconds=0)
    check("messages arrive in order as the JSON the DataChannel would have carried",
          messages == ['{"t":"d","b":0,"x":0.5,"y":0.5}', '{"t":"u","b":0}'] and cursor == 2)
    messages, _, _ = rr.read_up("s4", cursor, wait_seconds=0)
    check("and are not handed out twice", messages == [])
    rr.close_relay("s4")


def test_close_wakes_waiters():
    print("closing a relay wakes a waiting long-poll at once")
    rr.open_relay("s5", "PC-01")
    result = {}

    def wait():
        started = time.monotonic()
        result["down"] = rr.read_down("s5", 0, wait_seconds=5)
        result["took"] = time.monotonic() - started

    thread = threading.Thread(target=wait)
    thread.start()
    time.sleep(0.2)
    rr.close_relay("s5")
    thread.join(3)
    check("the waiter returned well before its timeout", result.get("took", 99) < 2)
    check("and was told the relay is closed", result.get("down", (0, 0, ""))[2] == "closed")
    check("a closed relay refuses further frames", rr.push_down("s5", [video(b"x")]) is None)


def test_new_frame_wakes_waiter():
    print("a frame arriving wakes a waiting viewer")
    rr.open_relay("s6", "PC-01")
    result = {}

    def wait():
        result["down"] = rr.read_down("s6", 0, wait_seconds=5)

    thread = threading.Thread(target=wait)
    thread.start()
    time.sleep(0.2)
    rr.push_down("s6", [video(b"K", key=True)])
    thread.join(3)
    records = result.get("down", ([], 0, ""))[0]
    check("the viewer got the frame without waiting out the poll",
          [p for _, _, p in records] == [b"K"])
    rr.close_relay("s6")


def test_open_is_idempotent_and_sweeps():
    print("open is idempotent and sweeps idle relays")
    a = rr.open_relay("s7", "PC-01")
    check("opening twice returns the same relay", rr.open_relay("s7", "PC-01") is a)
    a.last_activity -= rr.IDLE_DROP_SECONDS + 1
    rr.open_relay("s8", "PC-02")
    check("a relay idle past the limit is dropped on the next open", rr.get_relay("s7") is None)
    rr.close_relay("s8")
    check("nothing is left open", rr.active_count() == 0)


def _raises(fn):
    try:
        fn()
        return False
    except ValueError:
        return True


def main():
    test_framing()
    test_requires_open()
    test_new_viewer_starts_at_newest_keyframe()
    test_fallen_behind_viewer_resyncs()
    test_bounded_buffer()
    test_up_stream()
    test_close_wakes_waiters()
    test_new_frame_wakes_waiter()
    test_open_is_idempotent_and_sweeps()
    print(f"\n==== {PASS} passed, {FAIL} failed ====")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
