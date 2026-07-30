using System.Runtime.InteropServices;

namespace TempMonitorAgent.Remote;

/// <summary>
/// The slice of <c>ICodecAPI</c> we need to control keyframes on a Media Foundation encoder.
///
/// Vortice does not wrap ICodecAPI, and for a long time this file did not need to exist: the GOP
/// was set through MF_MT_MAX_KEYFRAME_SPACING on the output media type, which is tidier. Then a
/// capture self-test on a real machine showed the in-box software encoder honouring it (8 IDRs in
/// 8 seconds) and the hardware encoder flatly ignoring it -- <b>one</b> IDR at the start of an
/// 8-second clip and never another. Over WebRTC that is fatal rather than merely suboptimal: the
/// browser attaches while DTLS is still completing, misses the only keyframe there will ever be,
/// and shows a permanently black video with a healthy-looking connection behind it.
///
/// So we ask through ICodecAPI as well, which is the interface the hardware encoders actually
/// implement. <see cref="H264Encoder"/> treats every call here as best-effort and verifies the
/// result by looking for IDR NALs in the bitstream, because an encoder that accepts a setting and
/// ignores it is exactly the failure this exists to work around.
///
/// Declared by hand with the vtable order from codecapi.h. Only the methods up to SetValue are
/// listed; the rest of the interface follows but we never call it, and a short declaration cannot
/// dispatch to the wrong slot as long as nothing is added above.
/// </summary>
[ComImport]
[Guid("901db4c7-31ce-41a2-85dc-8fa0bf41b8da")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface ICodecApi
{
    [PreserveSig] int IsSupported(ref Guid api);
    [PreserveSig] int IsModifiable(ref Guid api);
    [PreserveSig] int GetParameterRange(ref Guid api, out object min, out object max, out object step);
    [PreserveSig] int GetParameterValues(ref Guid api, out IntPtr values, out uint count);
    [PreserveSig] int GetDefaultValue(ref Guid api, out object value);
    [PreserveSig] int GetValue(ref Guid api, out object value);
    [PreserveSig] int SetValue(ref Guid api, [MarshalAs(UnmanagedType.Struct)] ref object value);
}

/// <summary>The codec API property GUIDs we set, from codecapi.h.</summary>
public static class CodecApiProperties
{
    /// <summary>CODECAPI_AVEncMPVGOPSize -- frames between keyframes (VT_UI4). The ICodecAPI
    /// equivalent of MF_MT_MAX_KEYFRAME_SPACING, and the one hardware encoders honour.</summary>
    public static Guid GopSize = new("95f31b26-95a4-41aa-9303-246a7fc6eef1");

    /// <summary>CODECAPI_AVEncVideoForceKeyFrame -- emit an IDR for the next frame (VT_UI4, 1).
    /// Self-clearing: it applies to one frame, so it is set again each time we need one.</summary>
    public static Guid ForceKeyFrame = new("398c1b98-8353-475a-9ef2-8f265d260345");

    /// <summary>CODECAPI_AVLowLatencyMode -- no lookahead and no B-frames (VT_BOOL). We stamp RTP
    /// timestamps by monotonic frame duration, so reordered output would desync the receiver
    /// regardless of the latency it costs.</summary>
    public static Guid LowLatencyMode = new("9c27891a-ed7a-40e1-88e8-b22727a024ee");
}
