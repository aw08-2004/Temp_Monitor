using System.Runtime.InteropServices;
using Vortice.MediaFoundation;

namespace TempMonitorAgent.Remote;

/// <summary>
/// H.264 encoder over a Media Foundation Transform (roadmap #2). Takes NV12 frames (from
/// <see cref="ColorConvert"/>) and emits an Annex-B H.264 bitstream for the WebRTC track.
///
/// This first cut drives a <b>synchronous</b> encoder MFT (the in-box "H.264 Video Encoder",
/// which MFTEnumEx returns for a SYNCMFT query). It is real H.264 and works for WebRTC; it is
/// software, so it is the safe, portable baseline. The <b>hardware</b> path (NVENC/QuickSync)
/// is an async MFT driven by an event loop (METransformNeedInput/HaveOutput) plus a shared
/// D3D11 device -- that is worth wiring only where we can measure it, so it is deferred to the
/// on-hardware debugging pass rather than written blind. <see cref="IsHardware"/> reports which
/// path is live.
///
/// Not thread-safe: one encoder per capture loop.
/// </summary>
public sealed class H264Encoder : IDisposable
{
    // MFTEnumEx flags: SYNCMFT (in-box software encoder) + SORTANDFILTER (best match first).
    private const uint MFT_ENUM_FLAG_SYNCMFT = 0x00000001;
    private const uint MFT_ENUM_FLAG_SORTANDFILTER = 0x00000040;

    // Media Foundation HRESULTs we branch on during the output drain.
    private const int MF_E_TRANSFORM_NEED_MORE_INPUT = unchecked((int)0xC00D6D72);
    private const int MF_E_TRANSFORM_STREAM_CHANGE = unchecked((int)0xC00D6D61);

    private const uint MFVideoInterlace_Progressive = 2;
    private const uint eAVEncH264VProfile_Base = 66; // constrained-baseline-friendly, best browser interop

    private readonly int _width, _height, _fps;
    private IMFTransform? _transform;
    private int _outputBufferSize;
    private bool _started;

    public bool IsHardware => false;

    public H264Encoder(int width, int height, int fps, int bitrateBps)
    {
        _width = width;
        _height = height;
        _fps = fps <= 0 ? 30 : fps;

        MediaFactory.MFStartup(false);

        _transform = CreateEncoder()
            ?? throw new InvalidOperationException("no H.264 encoder MFT available");

        ConfigureTypes(bitrateBps);

        var info = _transform.GetOutputStreamInfo(0);
        // If the MFT allocates its own output samples we would not pre-allocate; the in-box
        // encoder does not, so size a buffer from the reported minimum (with a floor for MFTs
        // that report 0 before the first frame).
        _outputBufferSize = info.Size > 0 ? info.Size : Math.Max(1 << 16, _width * _height);

        _transform.ProcessMessage(TMessageType.MessageNotifyBeginStreaming, UIntPtr.Zero);
        _transform.ProcessMessage(TMessageType.MessageNotifyStartOfStream, UIntPtr.Zero);
        _started = true;
    }

    private static IMFTransform? CreateEncoder()
    {
        var output = new RegisterTypeInfo
        {
            GuidMajorType = MediaTypeGuids.Video,
            GuidSubtype = VideoFormatGuids.H264,
        };
        using var activates = MediaFactory.MFTEnumEx(
            TransformCategoryGuids.VideoEncoder,
            MFT_ENUM_FLAG_SYNCMFT | MFT_ENUM_FLAG_SORTANDFILTER,
            null, output);

        foreach (var activate in activates)
        {
            try { return activate.ActivateObject<IMFTransform>(); }
            catch { /* try the next registered encoder */ }
        }
        return null;
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

        var inSample = MediaFactory.MFCreateSample();
        var inBuffer = MediaFactory.MFCreateMemoryBuffer(nv12Length);
        inBuffer.Lock(out var dst, out _, out _);
        Marshal.Copy(nv12, 0, dst, nv12Length);
        inBuffer.Unlock();
        inBuffer.CurrentLength = nv12Length;
        inSample.AddBuffer(inBuffer);
        inSample.SampleTime = timestamp100ns;
        inSample.SampleDuration = duration100ns;

        _transform.ProcessInput(0, inSample, 0);

        using var output = new MemoryStream();
        DrainOutput(output);
        return output.ToArray();
    }

    private void DrainOutput(Stream sink)
    {
        while (true)
        {
            var outBuffer = MediaFactory.MFCreateMemoryBuffer(_outputBufferSize);
            var outSample = MediaFactory.MFCreateSample();
            outSample.AddBuffer(outBuffer);

            var data = new OutputDataBuffer { StreamID = 0, Sample = outSample };
            var hr = _transform!.ProcessOutput(ProcessOutputFlags.None, 1, ref data, out _);

            if (hr.Code == MF_E_TRANSFORM_NEED_MORE_INPUT)
                return; // encoder wants the next frame
            if (hr.Code == MF_E_TRANSFORM_STREAM_CHANGE)
                return; // renegotiation not handled in this baseline path
            if (hr.Failure)
                return; // surface nothing rather than throwing mid-stream; logged by the caller on empty output

            CopySample(data.Sample, sink);
        }
    }

    private static void CopySample(IMFSample sample, Stream sink)
    {
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
        _transform?.Dispose();
        _transform = null;
        try { MediaFactory.MFShutdown(); } catch { }
    }
}
