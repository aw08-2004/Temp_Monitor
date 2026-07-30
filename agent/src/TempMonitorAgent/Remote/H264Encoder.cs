using System.Collections.Concurrent;
using System.Runtime.InteropServices;
using Vortice.Direct3D;
using Vortice.Direct3D11;
using Vortice.DXGI;
using Vortice.MediaFoundation;

namespace TempMonitorAgent.Remote;

/// <summary>
/// H.264 encoder over a Media Foundation Transform (roadmap #2). Takes NV12 frames (from
/// <see cref="ColorConvert"/>) and emits an Annex-B H.264 bitstream for the WebRTC track.
///
/// Two MFT flavours, because Media Foundation splits them:
///   * <b>Synchronous</b> — the in-box software "H.264 Video Encoder". Drive it by alternating
///     ProcessInput / ProcessOutput. Always available, always the safe fallback.
///   * <b>Asynchronous</b> — the hardware encoders (NVENC, QuickSync, AMF). These will not
///     work if driven synchronously: they must be unlocked via MF_TRANSFORM_ASYNC_UNLOCK, given
///     a D3D device manager, and then driven from their event queue (METransformNeedInput /
///     METransformHaveOutput).
///
/// <b>Every wait on that event queue is bounded, and every failure is logged.</b> That is the
/// hard-won rule here. IMFMediaEventGenerator::GetEvent with dwFlags=0 blocks with no timeout,
/// so calling it on the capture thread parks the whole session the moment a hardware MFT
/// declines to produce its first event — which is exactly what a hardware encoder does when it
/// has no usable D3D device (an RDP session, for instance, where MFTEnumEx still cheerfully
/// hands back the machine's NVENC). The capture loop then never runs again, so nothing logs,
/// nothing rebuilds, and the operator sees a black screen that looks identical to a healthy
/// session. Hence: the blocking GetEvent lives on its own pump thread and the capture thread
/// only ever waits on a queue with a deadline.
///
/// The GOP is bounded through MF_MT_MAX_KEYFRAME_SPACING on the output type rather than
/// ICodecAPI. That matters: a WebRTC receiver that loses the packet carrying SPS/PPS cannot
/// decode anything until the next IDR, so an unbounded GOP turns one lost packet into an
/// indefinitely frozen screen. Setting it on the media type also avoids ICodecAPI entirely,
/// which Vortice does not wrap.
///
/// There is no reconfigure path by design — see <see cref="IVideoEncoder"/>. Media Foundation
/// itself is started once per process by <see cref="MediaFoundationRuntime"/>, not here:
/// MFShutdown is refcounted and would otherwise tear MF down under a replacement encoder.
///
/// Not thread-safe: one encoder per capture loop (the event pump is internal and owns nothing
/// the capture thread touches except the queue).
/// </summary>
public sealed class H264Encoder : IVideoEncoder
{
    // MFTEnumEx flags.
    private const uint MFT_ENUM_FLAG_SYNCMFT = 0x00000001;
    private const uint MFT_ENUM_FLAG_ASYNCMFT = 0x00000002;
    private const uint MFT_ENUM_FLAG_HARDWARE = 0x00000004;
    private const uint MFT_ENUM_FLAG_SORTANDFILTER = 0x00000040;

    // Media Foundation HRESULTs we branch on during the output drain.
    private const int MF_E_TRANSFORM_NEED_MORE_INPUT = unchecked((int)0xC00D6D72);
    private const int MF_E_TRANSFORM_STREAM_CHANGE = unchecked((int)0xC00D6D61);

    private const uint MFVideoInterlace_Progressive = 2;
    private const uint eAVEncH264VProfile_Base = 66; // constrained-baseline-friendly, best browser interop

    /// <summary>Cap on how long the async encoder may stall waiting for a NeedInput event
    /// before we give up on the frame. A hardware MFT that has wedged must not wedge the whole
    /// capture loop with it — see the class remarks for why this is enforced against a queue
    /// rather than against GetEvent directly.</summary>
    private const int AsyncEventTimeoutMs = 2000;

    /// <summary>Consecutive frames an async MFT may swallow before we stop believing in it and
    /// rebuild on the software path. At 15 fps this is ~2s of black screen, which is long enough
    /// to ride out an encoder that is merely buffering and short enough that the operator sees a
    /// picture rather than a mystery.</summary>
    private const int SilentFrameLimit = 30;

    /// <summary>Frames of grace after asking for a keyframe before asking again. An encoder that
    /// honours ForceKeyFrame answers on the very next frame; this is slack for one that queues.</summary>
    private const int KeyFrameGraceFrames = 5;

    /// <summary>How many times we will rebuild the transform purely to force a keyframe before
    /// concluding that this encoder is never going to produce one and leaving it alone. Rebuilding
    /// forever would turn a bad encoder into a bad encoder that also churns.</summary>
    private const int MaxKeyFrameRebuilds = 3;

    private readonly int _width, _height, _fps, _bitrateBps;
    private readonly EncoderPreference _preference;
    private readonly Action<string> _log;
    /// <summary>Target frames between IDRs -- the same interval we ask the MFT for, and the
    /// deadline the bitstream watchdog holds it to.</summary>
    private readonly int _keyFrameGop;

    private IMFTransform? _transform;
    private ICodecApi? _codecApi;
    private object? _codecApiRcw;
    private IMFMediaEventGenerator? _events;
    private BlockingCollection<MediaEventTypes>? _eventQueue;
    private Thread? _eventPump;
    private IMFDXGIDeviceManager? _deviceManager;
    private ID3D11Device? _d3dDevice;
    private ID3D11DeviceContext? _d3dContext;

    private int _outputBufferSize;
    private volatile bool _started;
    private volatile bool _disposed;
    private bool _isAsync;
    private bool _mftProvidesSamples;
    /// <summary>An async MFT signals NeedInput independently of our cadence; if one arrives
    /// while we are draining output, remember it rather than blocking for another.</summary>
    private int _pendingNeedInput;
    /// <summary>How many frames in a row have produced nothing. Drives the software fallback.</summary>
    private int _silentFrames;
    /// <summary>The software fallback is one-shot: if the in-box encoder is silent too, the
    /// problem is not the MFT flavour and rebuilding again would just churn.</summary>
    private bool _fellBack;
    /// <summary>Per-encoder latches so a persistent fault logs once instead of once per frame.</summary>
    private bool _warnedNoInput, _warnedPullFailed, _warnedNoKeyFrames;
    /// <summary>Frames emitted since the last IDR we actually saw in the bitstream, and how many
    /// times we have asked for one since. See <see cref="NoteEncodedFrame"/>.</summary>
    private int _framesSinceIdr, _keyFrameAsks, _keyFrameRebuilds;

    public bool IsHardware { get; private set; }

    public string Description => $"H.264 ({(IsHardware ? "hardware" : "software")})";

    public H264Encoder(int width, int height, int fps, int bitrateBps,
                       EncoderPreference preference = EncoderPreference.Auto,
                       Action<string>? log = null)
    {
        _width = width;
        _height = height;
        _fps = fps <= 0 ? 30 : fps;
        _bitrateBps = bitrateBps;
        _preference = preference;
        _log = log ?? (_ => { });
        _keyFrameGop = Math.Max(2, _fps * 2);

        MediaFoundationRuntime.EnsureStarted();
        BuildPipeline(preference);
    }

    /// <summary>Activate an MFT and drive it all the way to "streaming". Shared by construction
    /// and by every rebuild, so there is exactly one order in which an encoder gets set up --
    /// which matters, because that order is load-bearing (see <see cref="AttachD3DManager"/>).</summary>
    private void BuildPipeline(EncoderPreference preference)
    {
        _transform = CreateEncoder(preference, out bool hardware)
            ?? throw new InvalidOperationException("no H.264 encoder MFT available");
        IsHardware = hardware;

        if (hardware) PrepareAsync();

        // Order matters: unlock, then hand over the D3D manager, then set types. A hardware MFT
        // that is given its device manager after SetOutputType may accept every call and still
        // never produce a frame.
        if (_isAsync) AttachD3DManager();

        ConfigureTypes(_bitrateBps);
        AcquireCodecApi();
        ApplyCodecApiSettings();
        CacheOutputStreamInfo();
        StartStreaming();
        if (_isAsync) StartEventPump();

        _framesSinceIdr = 0;
        _keyFrameAsks = 0;
        _pendingNeedInput = 0;
        _silentFrames = 0;
        _warnedNoInput = _warnedPullFailed = false;
    }

    /// <summary>Find an encoder MFT. Hardware is tried first unless the operator forced
    /// software; a machine with no hardware encoder silently gets the in-box one, which is why
    /// <see cref="IsHardware"/> is reported to the viewer rather than assumed.</summary>
    private static IMFTransform? CreateEncoder(EncoderPreference preference, out bool hardware)
    {
        if (preference != EncoderPreference.Software)
        {
            var hw = Activate(MFT_ENUM_FLAG_HARDWARE | MFT_ENUM_FLAG_ASYNCMFT | MFT_ENUM_FLAG_SORTANDFILTER);
            if (hw is not null) { hardware = true; return hw; }
            // Forced hardware still falls back rather than failing the session outright: a black
            // screen is a worse answer to "this machine has no NVENC" than a software stream.
        }
        hardware = false;
        return Activate(MFT_ENUM_FLAG_SYNCMFT | MFT_ENUM_FLAG_SORTANDFILTER);
    }

    private static IMFTransform? Activate(uint flags)
    {
        var output = new RegisterTypeInfo
        {
            GuidMajorType = MediaTypeGuids.Video,
            GuidSubtype = VideoFormatGuids.H264,
        };
        try
        {
            using var activates = MediaFactory.MFTEnumEx(
                TransformCategoryGuids.VideoEncoder, flags, null, output);
            foreach (var activate in activates)
            {
                try { return activate.ActivateObject<IMFTransform>(); }
                catch { /* try the next registered encoder */ }
            }
        }
        catch { /* no MFT matched this flag combination */ }
        return null;
    }

    /// <summary>Unlock an async (hardware) MFT and grab its event queue. A hardware MFT that is
    /// not unlocked rejects every call, so a failure here demotes us to the software path rather
    /// than producing a silently dead encoder.</summary>
    private void PrepareAsync()
    {
        try
        {
            var attributes = _transform!.Attributes;
            if (attributes is null) return;
            if (attributes.GetUInt32(TransformAttributeKeys.TransformAsync) == 0) return;

            attributes.Set(TransformAttributeKeys.TransformAsyncUnlock, 1u);
            _events = _transform.QueryInterfaceOrNull<IMFMediaEventGenerator>();
            _isAsync = _events is not null;
        }
        catch (Exception e)
        {
            _log($"H.264 hardware MFT could not be unlocked ({e.Message}); using software");
            _isAsync = false;
            _events = null;
        }
        if (!_isAsync)
        {
            // We asked for a hardware MFT and got something we cannot drive. Rather than limp,
            // rebuild on the software path so the caller's IsHardware reporting stays honest.
            _transform?.Dispose();
            _transform = Activate(MFT_ENUM_FLAG_SYNCMFT | MFT_ENUM_FLAG_SORTANDFILTER)
                ?? throw new InvalidOperationException("no H.264 encoder MFT available");
            IsHardware = false;
        }
    }

    /// <summary>Give the async MFT a D3D11 device to encode against.
    ///
    /// This is what was missing when hardware H.264 produced nothing at all: without
    /// MFT_MESSAGE_SET_D3D_MANAGER a hardware encoder accepts the unlock, accepts both media
    /// types, accepts BEGIN_STREAMING — and then never raises METransformNeedInput, because it
    /// has no device to allocate against. Everything looks healthy right up to the point where
    /// nothing happens.
    ///
    /// The device is ours rather than the capture's on purpose: MF calls into it from its own
    /// worker threads, so it needs multithread protection, and imposing that on the duplication
    /// device would slow down the capture path for no benefit. Best-effort — if the device or
    /// the manager cannot be created we simply do not send the message, and the frame watchdog
    /// in <see cref="Encode"/> catches an MFT that then refuses to run.</summary>
    private void AttachD3DManager()
    {
        try
        {
            var levels = new[] { FeatureLevel.Level_11_1, FeatureLevel.Level_11_0, FeatureLevel.Level_10_1 };
            var hr = D3D11.D3D11CreateDevice(
                null, DriverType.Hardware,
                DeviceCreationFlags.BgraSupport | DeviceCreationFlags.VideoSupport, levels,
                out _d3dDevice, out _d3dContext);
            if (hr.Failure || _d3dDevice is null)
            {
                _log($"H.264 hardware MFT: no D3D11 device ({hr}); encoder may not start");
                return;
            }

            // MF drives this device from its own threads. Without this the MFT can deadlock or
            // corrupt state under load, and the failure looks like a random stall.
            using (var multithread = _d3dDevice.QueryInterfaceOrNull<ID3D11Multithread>())
                multithread?.SetMultithreadProtected(true);

            // Vortice's parameterless overload keeps the reset token on the manager and applies
            // it in ResetDevice, so the token never has to be carried around here.
            _deviceManager = MediaFactory.MFCreateDXGIDeviceManager();
            if (_deviceManager is null)
            {
                _log("H.264 hardware MFT: MFCreateDXGIDeviceManager returned nothing");
                return;
            }
            _deviceManager.ResetDevice(_d3dDevice).CheckError();
            _transform!.ProcessMessage(TMessageType.MessageSetD3DManager,
                                       (UIntPtr)(ulong)(long)_deviceManager.NativePointer);
        }
        catch (Exception e)
        {
            _log($"H.264 hardware MFT: attaching a D3D manager failed ({e.Message}); " +
                 "the encoder will be watched for silence and may fall back to software");
        }
    }

    private void ConfigureTypes(int bitrateBps)
    {
        // Output type MUST be set before the input type on an encoder MFT.
        var outType = MediaFactory.MFCreateMediaType();
        outType.Set(MediaTypeAttributeKeys.MajorType, MediaTypeGuids.Video);
        outType.Set(MediaTypeAttributeKeys.Subtype, VideoFormatGuids.H264);
        outType.Set(MediaTypeAttributeKeys.AvgBitrate, (uint)bitrateBps);
        outType.Set(MediaTypeAttributeKeys.FrameSize, PackU64(_width, _height));
        outType.Set(MediaTypeAttributeKeys.FrameRate, PackU64(_fps, 1));
        outType.Set(MediaTypeAttributeKeys.InterlaceMode, MFVideoInterlace_Progressive);
        outType.Set(MediaTypeAttributeKeys.Mpeg2Profile, eAVEncH264VProfile_Base);
        // Bounded GOP: an IDR (with in-band SPS/PPS) at least every two seconds, so a lost
        // keyframe costs a brief freeze instead of a permanently black viewer.
        outType.Set(MediaTypeAttributeKeys.MaxKeyframeSpacing, (uint)(_fps * 2));
        _transform!.SetOutputType(0, outType, 0);

        var inType = MediaFactory.MFCreateMediaType();
        inType.Set(MediaTypeAttributeKeys.MajorType, MediaTypeGuids.Video);
        inType.Set(MediaTypeAttributeKeys.Subtype, VideoFormatGuids.NV12);
        inType.Set(MediaTypeAttributeKeys.FrameSize, PackU64(_width, _height));
        inType.Set(MediaTypeAttributeKeys.FrameRate, PackU64(_fps, 1));
        inType.Set(MediaTypeAttributeKeys.InterlaceMode, MFVideoInterlace_Progressive);
        _transform.SetInputType(0, inType, 0);
    }

    /// <summary>QueryInterface the MFT for ICodecAPI. Absent on some software MFTs, which is why
    /// every use of <see cref="_codecApi"/> is null-checked rather than assumed.</summary>
    private void AcquireCodecApi()
    {
        try
        {
            _codecApiRcw = Marshal.GetObjectForIUnknown(_transform!.NativePointer);
            _codecApi = _codecApiRcw as ICodecApi;
            if (_codecApi is null) ReleaseCodecApi();
        }
        catch (Exception e)
        {
            _log($"H.264 encoder exposes no ICodecAPI ({e.Message}); relying on media-type settings");
            ReleaseCodecApi();
        }
    }

    private void ReleaseCodecApi()
    {
        _codecApi = null;
        if (_codecApiRcw is null) return;
        try { Marshal.ReleaseComObject(_codecApiRcw); } catch { /* already released */ }
        _codecApiRcw = null;
    }

    /// <summary>Ask for the GOP and low-latency behaviour we want. Best-effort in both directions:
    /// an MFT may not implement a property, and one that accepts it may still ignore it -- which
    /// is what the keyframe watchdog in <see cref="NoteEncodedFrame"/> is there to catch.</summary>
    private void ApplyCodecApiSettings()
    {
        TrySetCodecProperty(ref CodecApiProperties.GopSize, (uint)_keyFrameGop, "GOP size");
        TrySetCodecProperty(ref CodecApiProperties.LowLatencyMode, true, "low-latency mode");
    }

    private bool TrySetCodecProperty(ref Guid property, object value, string what)
    {
        if (_codecApi is null) return false;
        try
        {
            int hr = _codecApi.SetValue(ref property, ref value);
            if (hr >= 0) return true;
            _log($"H.264 encoder rejected {what}: 0x{hr:X8}");
        }
        catch (Exception e)
        {
            _log($"H.264 encoder rejected {what}: {e.Message}");
        }
        return false;
    }

    private void CacheOutputStreamInfo()
    {
        var info = _transform!.GetOutputStreamInfo(0);
        _mftProvidesSamples =
            (info.Flags & (int)(OutputStreamInfoFlags.OutputStreamProvidesSamples |
                                OutputStreamInfoFlags.OutputStreamCanProvideSamples)) != 0;
        // When the MFT allocates its own output samples we must not pre-allocate one. The in-box
        // software encoder does not, so size a buffer from the reported minimum (with a floor for
        // MFTs that report 0 before the first frame).
        _outputBufferSize = info.Size > 0 ? info.Size : Math.Max(1 << 16, _width * _height);
    }

    private void StartStreaming()
    {
        _transform!.ProcessMessage(TMessageType.MessageNotifyBeginStreaming, UIntPtr.Zero);
        _transform.ProcessMessage(TMessageType.MessageNotifyStartOfStream, UIntPtr.Zero);
        _started = true;
    }

    /// <summary>Encode one NV12 frame. Returns the encoded Annex-B bytes produced for it
    /// (usually one access unit; occasionally empty while the encoder buffers).</summary>
    public byte[] Encode(byte[] nv12, int nv12Length, long timestamp100ns, long duration100ns)
    {
        if (!_started || _transform is null) return Array.Empty<byte>();

        using var output = new MemoryStream();
        try
        {
            if (_isAsync)
            {
                if (AwaitNeedInput(output))
                {
                    _transform.ProcessInput(0, BuildSample(nv12, nv12Length, timestamp100ns, duration100ns), 0);
                    DrainAsyncOutput(output);
                }
            }
            else
            {
                _transform.ProcessInput(0, BuildSample(nv12, nv12Length, timestamp100ns, duration100ns), 0);
                DrainSyncOutput(output);
            }
        }
        catch (Exception e)
        {
            // A failed frame is a dropped frame, not a dead session -- but an encoder that fails
            // every frame is caught by the watchdog below rather than failing in silence.
            _log($"H.264 encode failed: {e.Message}");
        }

        var encoded = output.ToArray();
        NoteFrameProduced(encoded.Length > 0);
        NoteEncodedFrame(encoded);
        return encoded;
    }

    /// <summary>Hold the encoder to the GOP it was asked for, by reading the bitstream rather
    /// than trusting the setting.
    ///
    /// Hardware MFTs routinely accept MF_MT_MAX_KEYFRAME_SPACING and CODECAPI_AVEncMPVGOPSize and
    /// then emit exactly one IDR for the life of the encoder. A WebRTC receiver that misses that
    /// one keyframe -- which it will, because media starts flowing while DTLS is still completing
    /// -- has no way back and shows black forever. So: count frames since the last IDR we actually
    /// saw, ask for one when overdue, and if asking does not work, rebuild the transform, because
    /// a fresh encoder always emits SPS/PPS + IDR.</summary>
    private void NoteEncodedFrame(byte[] encoded)
    {
        if (encoded.Length == 0) return;
        if (ContainsIdr(encoded)) { _framesSinceIdr = 0; _keyFrameAsks = 0; return; }
        if (++_framesSinceIdr < _keyFrameGop) return;

        if (_keyFrameAsks < 2 &&
            TrySetCodecProperty(ref CodecApiProperties.ForceKeyFrame, (uint)1, "a forced keyframe"))
        {
            _keyFrameAsks++;
            _framesSinceIdr -= KeyFrameGraceFrames; // let it answer before we ask again
            return;
        }

        if (_keyFrameRebuilds >= MaxKeyFrameRebuilds)
        {
            WarnOnce(ref _warnedNoKeyFrames,
                "H.264 encoder will not produce periodic keyframes; a viewer that joins late or " +
                "drops a packet will not recover (further occurrences not logged)");
            _framesSinceIdr = 0; // stop re-triggering; the warning stands
            return;
        }
        _keyFrameRebuilds++;
        RebuildTransform(_preference, $"went {_framesSinceIdr} frames without a keyframe");
    }

    /// <summary>True if this access unit carries an IDR (NAL type 5) or a parameter set (7/8),
    /// scanning Annex-B start codes. Cheap: it stops at the first one it finds.</summary>
    internal static bool ContainsIdr(byte[] annexB)
    {
        for (int i = 0; i + 3 < annexB.Length; i++)
        {
            if (annexB[i] != 0 || annexB[i + 1] != 0) continue;
            int payload;
            if (annexB[i + 2] == 1) payload = i + 3;
            else if (annexB[i + 2] == 0 && annexB[i + 3] == 1) payload = i + 4;
            else continue;
            if (payload >= annexB.Length) return false;
            int nalType = annexB[payload] & 0x1f;
            if (nalType == 5 || nalType == 7) return true;
            i = payload;
        }
        return false;
    }

    /// <summary>Watch for an encoder that accepts everything and produces nothing, and demote it
    /// to the software path. This is the backstop for the whole hardware story: whatever the
    /// reason a given machine's NVENC/QuickSync will not run in this session, the operator ends
    /// up with a picture and a log line rather than a black rectangle.</summary>
    private void NoteFrameProduced(bool produced)
    {
        if (produced) { _silentFrames = 0; return; }
        if (++_silentFrames < SilentFrameLimit || _fellBack || !_isAsync) return;
        _fellBack = true;
        RebuildTransform(EncoderPreference.Software,
                         $"produced no output for {_silentFrames} consecutive frames");
    }

    /// <summary>Tear the encoder down and stand a new one up, keeping the same geometry. Used
    /// both to demote a hardware MFT that will not run and to re-key one that will not emit
    /// keyframes -- the same recovery the pipeline already relies on for a desktop or resolution
    /// change, applied to the encoder alone so the capture keeps running.</summary>
    private void RebuildTransform(EncoderPreference preference, string reason)
    {
        _log($"H.264 {(IsHardware ? "hardware" : "software")} encoder {reason}; rebuilding" +
             (preference == EncoderPreference.Software && IsHardware ? " on the in-box software encoder" : ""));
        try
        {
            if (_isAsync) StopEventPump();
            ReleaseCodecApi();
            ShutdownTransform();
            ReleaseD3D();
            _isAsync = false;

            BuildPipeline(preference);
            _log($"H.264 encoder ready: {Description}, {_width}x{_height} @ {_fps}fps");
        }
        catch (Exception e)
        {
            _log($"H.264 encoder rebuild failed: {e.Message}; this session has no video");
            _started = false;
        }
    }

    private static IMFSample BuildSample(byte[] nv12, int length, long timestamp100ns, long duration100ns)
    {
        var sample = MediaFactory.MFCreateSample();
        var buffer = MediaFactory.MFCreateMemoryBuffer(length);
        buffer.Lock(out var dst, out _, out _);
        Marshal.Copy(nv12, 0, dst, length);
        buffer.Unlock();
        buffer.CurrentLength = length;
        sample.AddBuffer(buffer);
        sample.SampleTime = timestamp100ns;
        sample.SampleDuration = duration100ns;
        return sample;
    }

    // ---------------------------------------------------------------- synchronous MFT drain
    private void DrainSyncOutput(Stream sink)
    {
        while (true)
        {
            var data = new OutputDataBuffer { StreamID = 0, Sample = NewOutputSample() };
            var hr = _transform!.ProcessOutput(ProcessOutputFlags.None, 1, ref data, out _);

            if (hr.Code == MF_E_TRANSFORM_NEED_MORE_INPUT) return; // wants the next frame
            if (hr.Code == MF_E_TRANSFORM_STREAM_CHANGE)
            {
                // The MFT wants to renegotiate its output type. Accepting the new type is the
                // whole handling -- returning here (as this used to) leaves the encoder in a
                // state where every subsequent ProcessOutput fails the same way and the stream
                // is dead for the rest of the session with nothing logged.
                if (!RenegotiateOutputType()) return;
                continue;
            }
            if (hr.Failure)
            {
                _log($"H.264 ProcessOutput failed: 0x{hr.Code:X8}");
                return; // surface nothing rather than throwing mid-stream
            }

            CopySample(data.Sample, sink);
        }
    }

    /// <summary>Accept the MFT's new output type after a stream change, and re-read the sizing
    /// that depends on it.</summary>
    private bool RenegotiateOutputType()
    {
        try
        {
            using var newType = _transform!.GetOutputAvailableType(0, 0);
            _transform.SetOutputType(0, newType, 0);
            CacheOutputStreamInfo();
            _log("H.264 encoder renegotiated its output type");
            return true;
        }
        catch (Exception e)
        {
            _log($"H.264 output type renegotiation failed: {e.Message}");
            return false;
        }
    }

    // ---------------------------------------------------------------- asynchronous MFT drain
    /// <summary>Pump the MFT's event queue on a dedicated thread.
    ///
    /// GetEvent(0) is a blocking call with NO timeout, so it can only be issued somewhere that
    /// is allowed to block forever. That is this thread and nowhere else — the capture loop
    /// waits on <see cref="_eventQueue"/> with a deadline instead. See the class remarks.</summary>
    private void StartEventPump()
    {
        _eventQueue = new BlockingCollection<MediaEventTypes>();
        _eventPump = new Thread(PumpEvents)
        {
            IsBackground = true,
            Name = "h264-mft-events",
        };
        _eventPump.Start();
    }

    private void PumpEvents()
    {
        var events = _events;
        var queue = _eventQueue;
        while (events is not null && queue is not null && !_disposed)
        {
            MediaEventTypes type;
            try
            {
                using var mediaEvent = events.GetEvent(0);
                if (mediaEvent is null) break;
                type = mediaEvent.EventType;
            }
            catch
            {
                // MF_E_SHUTDOWN once the transform is released -- the ordinary way out.
                break;
            }
            try { queue.Add(type); }
            catch { break; } // queue completed underneath us during teardown
        }
        try { queue?.CompleteAdding(); } catch { /* already completed */ }
    }

    /// <summary>Take the next event, waiting at most <paramref name="timeoutMs"/>. Unlike the
    /// GetEvent it replaces, this genuinely cannot outlast its timeout.</summary>
    private bool TryTakeEvent(int timeoutMs, out MediaEventTypes type)
    {
        type = MediaEventTypes.TransformUnknown;
        var queue = _eventQueue;
        if (queue is null) return false;
        try { return queue.TryTake(out type, Math.Max(0, timeoutMs)); }
        catch { return false; } // completed/disposed while we waited
    }

    /// <summary>Block until the async MFT asks for input, servicing any output events that
    /// arrive first. Returns false if it never asks within the timeout, which we treat as a
    /// dropped frame rather than a dead session.</summary>
    private bool AwaitNeedInput(Stream sink)
    {
        if (_pendingNeedInput > 0) { _pendingNeedInput--; return true; }

        var deadline = Environment.TickCount64 + AsyncEventTimeoutMs;
        while (true)
        {
            int remaining = (int)(deadline - Environment.TickCount64);
            if (remaining <= 0)
            {
                WarnOnce(ref _warnedNoInput,
                    $"H.264 async MFT did not ask for input within {AsyncEventTimeoutMs}ms; " +
                    "dropping frames (further occurrences not logged)");
                return false;
            }
            if (!TryTakeEvent(remaining, out var type))
            {
                WarnOnce(ref _warnedNoInput, "H.264 async MFT event queue closed; dropping frames");
                return false;
            }
            if (type == MediaEventTypes.TransformNeedInput) return true;
            if (type == MediaEventTypes.TransformHaveOutput) PullOne(sink);
            else if (type == MediaEventTypes.StreamFormatChanged) RenegotiateOutputType();
            else if (type == MediaEventTypes.TransformDrainComplete) return false;
        }
    }

    /// <summary>Drain whatever the async MFT has ready right now.</summary>
    private void DrainAsyncOutput(Stream sink)
    {
        while (TryTakeEvent(0, out var type))
        {
            if (type == MediaEventTypes.TransformHaveOutput) PullOne(sink);
            // A NeedInput that arrives while we are draining belongs to the NEXT frame; banking
            // it keeps the loop from blocking for an event the MFT has already sent.
            else if (type == MediaEventTypes.TransformNeedInput) { _pendingNeedInput++; return; }
            else if (type == MediaEventTypes.StreamFormatChanged) RenegotiateOutputType();
        }
    }

    private void PullOne(Stream sink)
    {
        // A hardware MFT allocates its own output sample; passing one would be rejected.
        var data = new OutputDataBuffer
        {
            StreamID = 0,
            Sample = _mftProvidesSamples ? null! : NewOutputSample(),
        };
        var hr = _transform!.ProcessOutput(ProcessOutputFlags.None, 1, ref data, out _);
        if (hr.Code == MF_E_TRANSFORM_STREAM_CHANGE) { RenegotiateOutputType(); return; }
        if (hr.Failure)
        {
            WarnOnce(ref _warnedPullFailed,
                $"H.264 ProcessOutput failed: 0x{hr.Code:X8} (further occurrences not logged)");
            return;
        }
        CopySample(data.Sample, sink);
    }

    private void WarnOnce(ref bool latch, string message)
    {
        if (latch) return;
        latch = true;
        _log(message);
    }

    private IMFSample NewOutputSample()
    {
        var sample = MediaFactory.MFCreateSample();
        sample.AddBuffer(MediaFactory.MFCreateMemoryBuffer(_outputBufferSize));
        return sample;
    }

    private static void CopySample(IMFSample? sample, Stream sink)
    {
        if (sample is null) return;
        using var buffer = sample.ConvertToContiguousBuffer();
        buffer.Lock(out var ptr, out _, out var current);
        try
        {
            if (current > 0)
            {
                var managed = new byte[current];
                Marshal.Copy(ptr, managed, 0, current);
                sink.Write(managed, 0, current);
            }
        }
        finally
        {
            buffer.Unlock();
        }
    }

    private static ulong PackU64(int high, int low) => ((ulong)(uint)high << 32) | (uint)low;

    /// <summary>Tell the async MFT to stop and let the pump thread fall out of its blocking
    /// GetEvent. FLUSH + END_OF_STREAM + END_STREAMING is the documented shutdown for an async
    /// MFT and is what releases a thread parked in GetEvent; the pump is a background thread, so
    /// an MFT that ignores all three costs us a parked thread for the life of the process rather
    /// than blocking exit.</summary>
    private void StopEventPump()
    {
        var pump = _eventPump;
        var queue = _eventQueue;
        _eventPump = null;
        _eventQueue = null;

        try
        {
            if (_started && _transform is not null)
            {
                _transform.ProcessMessage(TMessageType.MessageCommandFlush, UIntPtr.Zero);
                _transform.ProcessMessage(TMessageType.MessageNotifyEndOfStream, UIntPtr.Zero);
                _transform.ProcessMessage(TMessageType.MessageNotifyEndStreaming, UIntPtr.Zero);
            }
        }
        catch { /* shutting down */ }

        _events?.Dispose();
        _events = null;
        pump?.Join(500);
        try { queue?.CompleteAdding(); } catch { }
        queue?.Dispose();
    }

    private void ShutdownTransform()
    {
        try
        {
            if (_started && _transform is not null)
            {
                _transform.ProcessMessage(TMessageType.MessageNotifyEndOfStream, UIntPtr.Zero);
                _transform.ProcessMessage(TMessageType.MessageNotifyEndStreaming, UIntPtr.Zero);
            }
        }
        catch { /* shutting down */ }
        _started = false;
        _transform?.Dispose();
        _transform = null;
    }

    private void ReleaseD3D()
    {
        _deviceManager?.Dispose();
        _deviceManager = null;
        _d3dContext?.Dispose();
        _d3dContext = null;
        _d3dDevice?.Dispose();
        _d3dDevice = null;
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;

        if (_isAsync) StopEventPump();
        ReleaseCodecApi();
        ShutdownTransform();
        ReleaseD3D();
        // Media Foundation itself stays up -- MediaFoundationRuntime owns that, so a replacement
        // encoder built moments from now does not find MF torn down under it.
    }
}
