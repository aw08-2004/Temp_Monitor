using TempMonitorAgent.Remote;
using Xunit;

namespace TempMonitorAgent.Tests;

/// <summary>
/// Starting a capture on a screen that is not changing.
///
/// This is the logon screen, and it used to be unreachable: Desktop Duplication answers "no new
/// frame" for a desktop nobody is touching, the pipeline read that as "no frames at all", threw
/// away the picture it already had, and told the operator the agent was getting nothing -- on a
/// machine whose monitor was showing the logon prompt perfectly well.
/// </summary>
public class RemoteCaptureStartTests
{
    /// <summary>A capture that hands back one frame and then goes quiet, exactly as Desktop
    /// Duplication does on a static desktop.</summary>
    private sealed class OneFrameThenIdleCapture : IScreenCapture
    {
        private readonly int _framesToGive;
        public int Calls { get; private set; }

        public OneFrameThenIdleCapture(int framesToGive) => _framesToGive = framesToGive;

        public int Width { get; private set; }
        public int Height { get; private set; }
        public int Stride { get; private set; }
        public byte[] Frame { get; private set; } = Array.Empty<byte>();
        public bool HasFrame { get; private set; }

        public bool TryCapture(int timeoutMs)
        {
            if (Calls++ >= _framesToGive) return false;   // DXGI_ERROR_WAIT_TIMEOUT
            Width = 1920;
            Height = 1080;
            Stride = Width * 4;
            Frame = new byte[Stride * Height];
            HasFrame = true;
            return true;
        }

        public void Dispose() { }
    }

    [Fact]
    public void AFrameAlreadyInHand_StartsTheStream()
    {
        // The probe that chose this capture path consumed the only frame a static screen will
        // hand over. Requiring a fresh one here is what produced a black remote view.
        var capture = new OneFrameThenIdleCapture(framesToGive: 1);
        Assert.True(capture.TryCapture(100));   // stands in for the path probe
        Assert.False(capture.TryCapture(100));  // screen is static from here on

        Assert.True(CaptureEncodePipeline.WaitForFirstFrame(capture, out int w, out int h));
        Assert.Equal(1920, w);
        Assert.Equal(1080, h);
    }

    [Fact]
    public void AFreshFrame_StillStartsTheStreamImmediately()
    {
        var capture = new OneFrameThenIdleCapture(framesToGive: 1);

        Assert.True(CaptureEncodePipeline.WaitForFirstFrame(capture, out int w, out int h));
        Assert.Equal(1920, w);
        Assert.Equal(1, capture.Calls);   // took the first frame offered, no waiting
    }

    [Fact]
    public void ACaptureThatNeverProducesAnything_IsStillReportedAsFailed()
    {
        // The genuine "nothing to capture" case must keep failing, or the operator loses the
        // one message that explains a black screen.
        var capture = new OneFrameThenIdleCapture(framesToGive: 0);

        Assert.False(CaptureEncodePipeline.WaitForFirstFrame(capture, out int w, out int h));
        Assert.Equal(0, w);
        Assert.Equal(0, h);
    }
}
