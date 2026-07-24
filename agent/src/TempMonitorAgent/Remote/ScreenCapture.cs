using System.Runtime.InteropServices;
using Vortice.Direct3D;
using Vortice.Direct3D11;
using Vortice.DXGI;

namespace TempMonitorAgent.Remote;

/// <summary>Produces BGRA frames of one monitor for the remote-view pipeline (roadmap #2).
/// The frame buffer is reused between calls, so a caller consumes <see cref="Frame"/> before
/// the next <see cref="TryCapture"/>.</summary>
public interface IScreenCapture : IDisposable
{
    int Width { get; }
    int Height { get; }
    /// <summary>Row pitch of <see cref="Frame"/> in bytes (>= Width*4).</summary>
    int Stride { get; }
    /// <summary>BGRA pixels; valid only after <see cref="TryCapture"/> returns true.</summary>
    byte[] Frame { get; }
    /// <summary>Capture the next frame. Returns false on a timeout with no screen change
    /// (nothing new to send) so the caller can hold the previous frame.</summary>
    bool TryCapture(int timeoutMs);
}

/// <summary>
/// DXGI Desktop Duplication capture. This is the fast path: the compositor hands us the
/// desktop surface on the GPU, we copy it to a CPU-readable staging texture, and expose the
/// BGRA bytes.
///
/// Two events must be handled or the stream dies silently:
///   * <b>Timeout</b> (no pixels changed) -- normal and frequent; return false, keep the last
///     frame.
///   * <b>Access lost</b> -- the duplication is invalidated whenever the desktop switches
///     (lock screen, UAC secure desktop, resolution change, a full-screen exclusive app).
///     We tear down and rebuild the duplication; the caller just retries next tick. Because the
///     helper runs as SYSTEM-in-session it has rights to the secure (Winlogon) desktop, so a
///     rebuild after the switch should re-duplicate it -- but this is the single piece that most
///     needs on-hardware validation (some setups need the capture thread attached to the input
///     desktop, and SendInput to the secure desktop is a separate concern in InputInjector).
///
/// Not thread-safe: one capture loop owns one instance.
/// </summary>
public sealed class DxgiScreenCapture : IScreenCapture
{
    private readonly int _monitorIndex;
    private ID3D11Device? _device;
    private ID3D11DeviceContext? _context;
    private IDXGIOutputDuplication? _duplication;
    private ID3D11Texture2D? _staging;
    private uint _stagingW, _stagingH;

    public int Width { get; private set; }
    public int Height { get; private set; }
    public int Stride { get; private set; }
    public byte[] Frame { get; private set; } = Array.Empty<byte>();

    public DxgiScreenCapture(int monitorIndex) => _monitorIndex = monitorIndex;

    public bool TryCapture(int timeoutMs)
    {
        if (_duplication is null && !EnsureDuplication())
            return false;

        IDXGIResource? desktopResource = null;
        try
        {
            var result = _duplication!.AcquireNextFrame((uint)timeoutMs, out _, out desktopResource);
            if (result == Vortice.DXGI.ResultCode.WaitTimeout)
                return false; // no change
            if (result.Failure)
            {
                // Access lost (desktop switch) or anything else: rebuild next tick.
                Teardown();
                return false;
            }

            using var texture = desktopResource!.QueryInterface<ID3D11Texture2D>();
            var desc = texture.Description;
            EnsureStaging(desc.Width, desc.Height, desc.Format);

            _context!.CopyResource(_staging!, texture);
            var mapped = _context.Map(_staging!, 0, MapMode.Read, Vortice.Direct3D11.MapFlags.None);
            try
            {
                Width = (int)desc.Width;
                Height = (int)desc.Height;
                Stride = (int)mapped.RowPitch;
                int needed = Stride * Height;
                if (Frame.Length < needed) Frame = new byte[needed];
                Marshal.Copy(mapped.DataPointer, Frame, 0, needed);
                return true;
            }
            finally
            {
                _context.Unmap(_staging!, 0);
            }
        }
        catch
        {
            Teardown();
            return false;
        }
        finally
        {
            desktopResource?.Dispose();
            try { _duplication?.ReleaseFrame(); } catch { /* already released on teardown */ }
        }
    }

    private bool EnsureDuplication()
    {
        try
        {
            using var factory = DXGI.CreateDXGIFactory1<IDXGIFactory1>();
            if (factory.EnumAdapters1(0, out var adapter).Failure || adapter is null)
                return false;

            using (adapter)
            {
                var levels = new[] { FeatureLevel.Level_11_0, FeatureLevel.Level_10_1, FeatureLevel.Level_10_0 };
                var hr = D3D11.D3D11CreateDevice(
                    adapter, DriverType.Unknown, DeviceCreationFlags.BgraSupport, levels,
                    out _device, out _context);
                if (hr.Failure || _device is null)
                    return false;

                // Pick the requested monitor, falling back to the primary if it's gone.
                IDXGIOutput? output = null;
                if (adapter.EnumOutputs((uint)_monitorIndex, out output).Failure || output is null)
                    adapter.EnumOutputs(0, out output);
                if (output is null) return false;

                using (output)
                using (var output1 = output.QueryInterface<IDXGIOutput1>())
                {
                    _duplication = output1.DuplicateOutput(_device);
                }
            }
            return _duplication is not null;
        }
        catch
        {
            Teardown();
            return false;
        }
    }

    private void EnsureStaging(uint width, uint height, Format format)
    {
        if (_staging is not null && _stagingW == width && _stagingH == height) return;
        _staging?.Dispose();
        _staging = _device!.CreateTexture2D(new Texture2DDescription
        {
            Width = width,
            Height = height,
            MipLevels = 1,
            ArraySize = 1,
            Format = format,
            SampleDescription = new SampleDescription(1, 0),
            Usage = ResourceUsage.Staging,
            BindFlags = BindFlags.None,
            CPUAccessFlags = CpuAccessFlags.Read,
            MiscFlags = ResourceOptionFlags.None,
        });
        _stagingW = width;
        _stagingH = height;
    }

    private void Teardown()
    {
        try { _duplication?.ReleaseFrame(); } catch { }
        _staging?.Dispose(); _staging = null; _stagingW = _stagingH = 0;
        _duplication?.Dispose(); _duplication = null;
        _context?.Dispose(); _context = null;
        _device?.Dispose(); _device = null;
    }

    public void Dispose() => Teardown();
}

/// <summary>
/// GDI BitBlt fallback for when Desktop Duplication is unavailable (some VMs, older RDP
/// sessions). Slower and with no change detection -- it blits the whole primary display every
/// call -- but it always produces a frame. It cannot see the secure desktop either; that is a
/// phase-5 concern for both capture paths.
/// </summary>
public sealed class GdiScreenCapture : IScreenCapture
{
    public int Width { get; private set; }
    public int Height { get; private set; }
    public int Stride { get; private set; }
    public byte[] Frame { get; private set; } = Array.Empty<byte>();

    public bool TryCapture(int timeoutMs)
    {
        IntPtr screenDc = GetDC(IntPtr.Zero);
        if (screenDc == IntPtr.Zero) return false;
        IntPtr memDc = IntPtr.Zero, bmp = IntPtr.Zero;
        try
        {
            int w = GetSystemMetrics(SM_CXSCREEN);
            int h = GetSystemMetrics(SM_CYSCREEN);
            if (w <= 0 || h <= 0) return false;

            memDc = CreateCompatibleDC(screenDc);
            bmp = CreateCompatibleBitmap(screenDc, w, h);
            IntPtr old = SelectObject(memDc, bmp);
            BitBlt(memDc, 0, 0, w, h, screenDc, 0, 0, SRCCOPY | CAPTUREBLT);

            Width = w; Height = h; Stride = w * 4;
            int needed = Stride * h;
            if (Frame.Length < needed) Frame = new byte[needed];

            var bmi = new BITMAPINFOHEADER
            {
                biSize = Marshal.SizeOf<BITMAPINFOHEADER>(),
                biWidth = w,
                biHeight = -h,          // top-down
                biPlanes = 1,
                biBitCount = 32,
                biCompression = 0,      // BI_RGB
            };
            var handle = GCHandle.Alloc(Frame, GCHandleType.Pinned);
            try { GetDIBits(memDc, bmp, 0, (uint)h, handle.AddrOfPinnedObject(), ref bmi, 0); }
            finally { handle.Free(); }

            SelectObject(memDc, old);
            return true;
        }
        catch { return false; }
        finally
        {
            if (bmp != IntPtr.Zero) DeleteObject(bmp);
            if (memDc != IntPtr.Zero) DeleteDC(memDc);
            ReleaseDC(IntPtr.Zero, screenDc);
        }
    }

    public void Dispose() { }

    private const int SM_CXSCREEN = 0, SM_CYSCREEN = 1;
    private const int SRCCOPY = 0x00CC0020;
    private const int CAPTUREBLT = 0x40000000;

    [StructLayout(LayoutKind.Sequential)]
    private struct BITMAPINFOHEADER
    {
        public int biSize, biWidth, biHeight;
        public short biPlanes, biBitCount;
        public int biCompression, biSizeImage, biXPelsPerMeter, biYPelsPerMeter, biClrUsed, biClrImportant;
    }

    [DllImport("user32.dll")] private static extern IntPtr GetDC(IntPtr hWnd);
    [DllImport("user32.dll")] private static extern int ReleaseDC(IntPtr hWnd, IntPtr hDc);
    [DllImport("user32.dll")] private static extern int GetSystemMetrics(int index);
    [DllImport("gdi32.dll")] private static extern IntPtr CreateCompatibleDC(IntPtr hDc);
    [DllImport("gdi32.dll")] private static extern bool DeleteDC(IntPtr hDc);
    [DllImport("gdi32.dll")] private static extern IntPtr CreateCompatibleBitmap(IntPtr hDc, int w, int h);
    [DllImport("gdi32.dll")] private static extern bool DeleteObject(IntPtr obj);
    [DllImport("gdi32.dll")] private static extern IntPtr SelectObject(IntPtr hDc, IntPtr obj);
    [DllImport("gdi32.dll")] private static extern bool BitBlt(
        IntPtr hDc, int x, int y, int w, int h, IntPtr hSrcDc, int xSrc, int ySrc, int rop);
    [DllImport("gdi32.dll")] private static extern int GetDIBits(
        IntPtr hDc, IntPtr bmp, uint start, uint lines, IntPtr bits, ref BITMAPINFOHEADER bmi, uint usage);
}
