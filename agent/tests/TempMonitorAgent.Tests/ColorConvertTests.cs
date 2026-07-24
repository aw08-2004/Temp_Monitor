using TempMonitorAgent.Remote;

namespace TempMonitorAgent.Tests;

/// <summary>BGRA -> NV12 conversion (roadmap #2). The rest of the capture pipeline needs a GPU
/// and an encoder to exercise, but this matrix is pure arithmetic, so its correctness -- the
/// thing that decides whether remote video shows the right colours -- is pinned here.</summary>
public class ColorConvertTests
{
    private static byte[] SolidBgra(int width, int height, byte b, byte g, byte r, int stride)
    {
        var buf = new byte[stride * height];
        for (int y = 0; y < height; y++)
            for (int x = 0; x < width; x++)
            {
                int p = y * stride + x * 4;
                buf[p] = b; buf[p + 1] = g; buf[p + 2] = r; buf[p + 3] = 255;
            }
        return buf;
    }

    [Fact]
    public void Nv12Size_IsYPlanePlusHalf()
    {
        Assert.Equal(1920 * 1080 * 3 / 2, ColorConvert.Nv12Size(1920, 1080));
    }

    [Theory]
    // (B, G, R) -> expected (Y, U, V), BT.601 limited-range integer coefficients.
    [InlineData(0, 0, 0, 16, 128, 128)]       // black
    [InlineData(255, 255, 255, 235, 128, 128)] // white
    [InlineData(0, 0, 255, 82, 90, 240)]       // pure red
    [InlineData(0, 255, 0, 145, 54, 34)]       // pure green
    [InlineData(255, 0, 0, 41, 240, 110)]      // pure blue
    public void BgraToNv12_SolidColour_ProducesExpectedYuv(
        byte b, byte g, byte r, byte expY, byte expU, byte expV)
    {
        const int w = 4, h = 4, stride = w * 4;
        var bgra = SolidBgra(w, h, b, g, r, stride);
        var nv12 = new byte[ColorConvert.Nv12Size(w, h)];

        ColorConvert.BgraToNv12(bgra, stride, w, h, nv12);

        // Every Y sample equals expY (allow +/-1 for rounding).
        for (int i = 0; i < w * h; i++)
            Assert.InRange(nv12[i], expY - 1, expY + 1);

        // Interleaved UV in the chroma plane.
        int uv = w * h;
        for (int i = uv; i < nv12.Length; i += 2)
        {
            Assert.InRange(nv12[i], expU - 1, expU + 1);
            Assert.InRange(nv12[i + 1], expV - 1, expV + 1);
        }
    }

    [Fact]
    public void BgraToNv12_HonoursStridePadding()
    {
        // A staging texture's row pitch is usually wider than width*4; the padding must be
        // skipped, not read as pixels.
        const int w = 2, h = 2, stride = w * 4 + 16;
        var bgra = SolidBgra(w, h, 0, 0, 255, stride); // red, padded rows
        var nv12 = new byte[ColorConvert.Nv12Size(w, h)];

        ColorConvert.BgraToNv12(bgra, stride, w, h, nv12);

        Assert.InRange(nv12[0], 81, 83); // Y for red
    }

    [Fact]
    public void BgraToNv12_RejectsOddDimensions()
    {
        var bgra = new byte[3 * 3 * 4];
        var nv12 = new byte[64];
        Assert.Throws<ArgumentException>(() => ColorConvert.BgraToNv12(bgra, 3 * 4, 3, 3, nv12));
    }

    [Fact]
    public void BgraToNv12_RejectsTooSmallDestination()
    {
        var bgra = new byte[4 * 4 * 4];
        var nv12 = new byte[4]; // way too small
        Assert.Throws<ArgumentException>(() => ColorConvert.BgraToNv12(bgra, 4 * 4, 4, 4, nv12));
    }
}
