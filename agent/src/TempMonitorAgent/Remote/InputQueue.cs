namespace TempMonitorAgent.Remote;

/// <summary>
/// Hands input events from the WebRTC data-channel thread to the desktop-bound input thread.
///
/// This queue exists because of a hard Win32 constraint: <c>SendInput</c> only reaches the
/// input desktop if the calling thread is attached to it, and <c>SetThreadDesktop</c> is
/// per-thread. The control-channel callback fires on a SIPSorcery thread we neither own nor
/// may rebind, so it cannot inject directly. It enqueues; a dedicated thread that owns a
/// <see cref="ThreadDesktopBinder"/> drains and injects.
///
/// The drop policy is not symmetric, and that asymmetry matters. Mouse moves are a stream of
/// approximations -- dropping stale ones under pressure is strictly better than replaying them
/// late. Key events are transitions: dropping a keyUp leaves a modifier stuck down on the
/// remote machine, so the operator's next keystroke arrives as Ctrl+something. Everything that
/// is not a mouse move is therefore preserved, and only if the queue is full of those do we
/// drop the oldest and say so.
/// </summary>
internal sealed class InputQueue : IDisposable
{
    private readonly int _capacity;
    private readonly Action<string>? _log;
    private readonly Queue<string> _items = new();
    private readonly object _gate = new();
    private readonly SemaphoreSlim _signal = new(0);

    private bool _completed;
    private int _dropped;
    private bool _loggedDrop;

    public InputQueue(int capacity = 512, Action<string>? log = null)
    {
        _capacity = capacity <= 0 ? 512 : capacity;
        _log = log;
    }

    /// <summary>Number of events discarded because the queue was saturated. Non-zero means the
    /// input thread is not keeping up -- usually because it is stuck on a desktop it cannot
    /// attach to.</summary>
    public int Dropped { get { lock (_gate) return _dropped; } }

    /// <summary>Enqueue a raw control message. Called on the SIPSorcery thread: it must not
    /// block, must not touch Win32, and must never throw.</summary>
    public void Enqueue(string json)
    {
        if (string.IsNullOrEmpty(json)) return;
        lock (_gate)
        {
            if (_completed) return;
            if (_items.Count >= _capacity && !TrimOneMouseMove())
            {
                // Nothing droppable left; sacrifice the oldest event rather than the newest, so
                // the remote machine ends up in the state the operator most recently asked for.
                _items.Dequeue();
                _dropped++;
                if (!_loggedDrop)
                {
                    _loggedDrop = true;
                    _log?.Invoke($"input queue saturated at {_capacity} events; dropping. " +
                                 "The input thread is not draining -- check the desktop binder.");
                }
            }
            _items.Enqueue(json);
        }
        _signal.Release();
    }

    /// <summary>Drop the oldest mouse-move, if there is one. Returns false when the queue holds
    /// nothing but events we refuse to lose.</summary>
    private bool TrimOneMouseMove()
    {
        int count = _items.Count;
        bool removed = false;
        for (int i = 0; i < count; i++)
        {
            string item = _items.Dequeue();
            if (!removed && IsMouseMove(item)) { removed = true; _dropped++; continue; }
            _items.Enqueue(item);
        }
        return removed;
    }

    /// <summary>Cheap structural test for <c>{"t":"m",...}</c> that avoids parsing JSON on the
    /// data-channel thread for every event.</summary>
    private static bool IsMouseMove(string json) => json.Contains("\"t\":\"m\"", StringComparison.Ordinal);

    /// <summary>Wait for the next event. Returns false on timeout or once the queue is
    /// completed and drained.</summary>
    public bool TryTake(out string json, int timeoutMs)
    {
        json = "";
        if (!_signal.Wait(timeoutMs)) return false;
        lock (_gate)
        {
            if (_items.Count == 0) return false;
            json = _items.Dequeue();
            return true;
        }
    }

    /// <summary>Stop accepting events and wake any waiter so the input thread can exit.</summary>
    public void Complete()
    {
        lock (_gate) _completed = true;
        _signal.Release();
    }

    public void Dispose()
    {
        Complete();
        _signal.Dispose();
    }
}
