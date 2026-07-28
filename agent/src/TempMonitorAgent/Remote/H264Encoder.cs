using System.Runtime.InteropServices;
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
///     work if driven synchronously: they must be unlocked via MF_TRANSFORM_ASYNC_UNLOCK and
///     then driven from their event queue (METransformNeedInput / METransformHaveOutput).
///     Because the capture loop owns a dedicated thread, we can block on GetEvent rather than
///     wiring an IMFAsyncCallback, which keeps the control flow readable.
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
/// Not thread-safe: one encoder per capture loop.
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
    /// capture loop with it.</summary>
    private const int AsyncEventTimeoutMs = 2000;

    private readonly int _width, _height, _fps;
    private IMFTransform? _transform;
    private IMFMediaEventGenerator? _events;
    private int _outputBufferSize;
    private bool _started;
    private bool _isAsync;
    private bool _mftProvidesSamples;
    /// <summary>An async MFT signals NeedInput independently of our cadence; if one arrives
    /// while we are draining output, remember it rather than blocking for another.</summary>
    private int _pendingNeedInput;

    public bool IsHardware { get; private set; }

    public string Description => $"H.264 ({(IsHardware ? "hardware" : "software")})";

    public H264Encoder(int width, int height, int fps, int bitrateBps,
                       EncoderPreference preference = EncoderPreference.Auto)
    {
        _width = width;
        _height = height;
        _fps = fps <= 0 ? 30 : fps;

        MediaFoundationRuntime.EnsureStarted();

        _transform = CreateEncoder(preference, out bool hardware)
            ?? throw new InvalidOperationException("no H.264 encoder MFT available");
        IsHardware = hardware;

        if (hardware) PrepareAsync();

        ConfigureTypes(bitrateBps);

        var info = _transform.GetOutputStreamInfo(0);
        _mftProvidesSamples =
            (info.Flags & (int)(OutputStreamInfoFlags.OutputStreamProvidesSamples |
                                OutputStreamInfoFlags.OutputStreamCanProvideSamples)) != 0;
        // When the MFT allocates its own output samples we must not pre-allocate one. The in-box
        // software encoder does not, so size a buffer from the reported minimum (with a floor for
        // MFTs that report 0 before the first frame).
        _outputBufferSize = info.Size > 0 ? info.Size : Math.Max(1 << 16, _width * _height);

        _transform.ProcessMessage(TMessageType.MessageNotifyBeginStreaming, UIntPtr.Zero);
        _transform.ProcessMessage(TMessageType.MessageNotifyStartOfStream, UIntPtr.Zero);
        _started = true;
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
        catch
        {
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

    /// <summary>Encode one NV12 frame. Returns the encoded Annex-B bytes produced for it
    /// (usually one access unit; occasionally empty while the encoder buffers).</summary>
    public byte[] Encode(byte[] nv12, int nv12Length, long timestamp100ns, long duration100ns)
    {
        if (!_started || _transform is null) return Array.Empty<byte>();

        using var output = new MemoryStream();
        if (_isAsync)
        {
            if (!AwaitNeedInput(output)) return output.ToArray();
            _transform.ProcessInput(0, BuildSample(nv12, nv12Length, timestamp100ns, duration100ns), 0);
            DrainAsyncOutput(output, blocking: false);
        }
        else
        {
            _transform.ProcessInput(0, BuildSample(nv12, nv12Length, timestamp100ns, duration100ns), 0);
            DrainSyncOutput(output);
        }
        return output.ToArray();
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
            if (hr.Code == MF_E_TRANSFORM_STREAM_CHANGE) return;   // handled by rebuilding, not here
            if (hr.Failure) return; // surface nothing rather than throwing mid-stream

            CopySample(data.Sample, sink);
        }
    }

    // ---------------------------------------------------------------- asynchronous MFT drain
    /// <summary>Block until the async MFT asks for input, servicing any output events that
    /// arrive first. Returns false if it never asks within the timeout, which we treat as a
    /// dropped frame rather than a dead session.</summary>
    private bool AwaitNeedInput(Stream sink)
    {
        if (_pendingNeedInput > 0) { _pendingNeedInput--; return true; }

        var deadline = Environment.TickCount64 + AsyncEventTimeoutMs;
        while (Environment.TickCount64 < deadline)
        {
            if (!TryGetEvent(blocking: true, out var type)) return false;
            if (type == MediaEventTypes.TransformNeedInput) return true;
            if (type == MediaEventTypes.TransformHaveOutput) PullOne(sink);
        }
        return false;
    }

    /// <summary>Drain whatever the async MFT has ready right now.</summary>
    private void DrainAsyncOutput(Stream sink, bool blocking)
    {
        while (TryGetEvent(blocking, out var type))
        {
            if (type == MediaEventTypes.TransformHaveOutput) PullOne(sink);
            // A NeedInput that arrives while we are draining belongs to the NEXT frame; banking
            // it keeps the loop from blocking for an event the MFT has already sent.
            else if (type == MediaEventTypes.TransformNeedInput) { _pendingNeedInput++; return; }
        }
    }

    private bool TryGetEvent(bool blocking, out MediaEventTypes type)
    {
        type = MediaEventTypes.TransformUnknown;
        if (_events is null) return false;
        try
        {
            // MF_EVENT_FLAG_NO_WAIT = 1. Non-blocking throws MF_E_NO_EVENTS_AVAILABLE when the
            // queue is empty, which is the ordinary "nothing more to drain" answer, not an error.
            using var mediaEvent = _events.GetEvent(blocking ? 0 : 1);
            if (mediaEvent is null) return false;
            type = mediaEvent.EventType;
            return true;
        }
        catch { return false; }
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
        if (hr.Failure) return;
        CopySample(data.Sample, sink);
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

    public void Dispose()
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
        _events?.Dispose();
        _events = null;
        _transform?.Dispose();
        _transform = null;
        // Media Foundation itself stays up -- MediaFoundationRuntime owns that, so a replacement
        // encoder built moments from now does not find MF torn down under it.
    }
}
