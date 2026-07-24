namespace TempMonitorAgent.Remote;

/// <summary>
/// BGRA -> NV12 colour conversion (roadmap #2). DXGI Desktop Duplication hands back a BGRA
/// (B8G8R8A8) surface; the Media Foundation H.264 encoder wants NV12, so every captured frame
/// crosses this boundary. Kept pure and separate from the capture/encode I/O precisely so it
/// can be unit-tested without a GPU or an encoder.
///
/// NV12 layout: a full-resolution Y (luma) plane of width*height bytes, followed by a
/// half-resolution interleaved UV (chroma) plane -- one U,V pair per 2x2 block of pixels, so
/// width*height/2 bytes. Width and height must therefore be even; the caller crops any odd
/// edge before getting here.
///
/// Coefficients are BT.601 limited ("video") range -- the matrix the in-box encoder and most
/// decoders assume by default. Screen content is full-range RGB, so colours may read slightly
/// washed until we confirm the VUI signalling on real hardware; that is a deliberate, easily
/// adjusted default (swap to BT.709 / full-range here + signal it on the encoder), not a bug
/// to chase blind.
/// </summary>
public static class ColorConvert
{
    public static int Nv12Size(int width, int height) => width * height + width * height / 2;

    /// <summary>Convert a BGRA frame into a caller-provided NV12 buffer.</summary>
    /// <param name="bgra">Source pixels, BGRA order (as DXGI staging maps give them).</param>
    /// <param name="stride">Source row pitch in bytes (>= width*4; staging rows are padded).</param>
    /// <param name="width">Even.</param>
    /// <param name="height">Even.</param>
    /// <param name="nv12">Destination, at least <see cref="Nv12Size"/> bytes.</param>
    public static void BgraToNv12(
        ReadOnlySpan<byte> bgra, int stride, int width, int height, Span<byte> nv12)
    {
        if ((width & 1) != 0 || (height & 1) != 0)
            throw new ArgumentException("NV12 requires even width and height");
        if (stride < width * 4)
            throw new ArgumentException("stride is smaller than width*4");
        if (nv12.Length < Nv12Size(width, height))
            throw new ArgumentException("nv12 buffer too small");

        int ySize = width * height;
        // Y plane: one luma sample per pixel.
        for (int y = 0; y < height; y++)
        {
            int row = y * stride;
            int yOut = y * width;
            for (int x = 0; x < width; x++)
            {
                int p = row + x * 4;
                int b = bgra[p], g = bgra[p + 1], r = bgra[p + 2];
                nv12[yOut + x] = (byte)(((66 * r + 129 * g + 25 * b + 128) >> 8) + 16);
            }
        }

        // UV plane: one averaged chroma pair per 2x2 pixel block.
        int uvOut = ySize;
        for (int y = 0; y < height; y += 2)
        {
            int row0 = y * stride;
            int row1 = (y + 1) * stride;
            for (int x = 0; x < width; x += 2)
            {
                int p00 = row0 + x * 4;
                int p01 = row0 + (x + 1) * 4;
                int p10 = row1 + x * 4;
                int p11 = row1 + (x + 1) * 4;

                // Average the block's RGB, then derive chroma (better than point-sampling one
                // corner, which shimmers on text edges).
                int b = (bgra[p00] + bgra[p01] + bgra[p10] + bgra[p11] + 2) >> 2;
                int g = (bgra[p00 + 1] + bgra[p01 + 1] + bgra[p10 + 1] + bgra[p11 + 1] + 2) >> 2;
                int r = (bgra[p00 + 2] + bgra[p01 + 2] + bgra[p10 + 2] + bgra[p11 + 2] + 2) >> 2;

                int u = ((-38 * r - 74 * g + 112 * b + 128) >> 8) + 128;
                int v = ((112 * r - 94 * g - 18 * b + 128) >> 8) + 128;
                nv12[uvOut++] = Clamp(u);
                nv12[uvOut++] = Clamp(v);
            }
        }
    }

    private static byte Clamp(int v) => (byte)(v < 0 ? 0 : v > 255 ? 255 : v);
}
