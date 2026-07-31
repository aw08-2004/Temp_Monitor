using System.Drawing;
using System.Drawing.Drawing2D;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Windows.Forms;

namespace TempMonitorAgent.Remote;

/// <summary>
/// The window <see cref="ConsentBanner"/> puts on the logged-in user's desktop when a remote
/// session needs attended consent. It replaces a plain MessageBox because this prompt is the
/// one piece of the product an end user ever sees: it has to look like it belongs to FleetHub
/// and make "someone is about to watch my screen" unmissable, not like a stray system error.
///
/// Styling follows the hub's dark tokens (bg #0b0e14 / card #171b24 / accent #3b82f6) so the
/// prompt and the console an operator is looking at are recognisably the same product.
///
/// Every exit that is not the Allow button -- Deny, Esc, the timeout, an Alt+F4 -- leaves
/// <see cref="Approved"/> false, which is what keeps the fail-closed contract intact.
/// </summary>
internal sealed class ConsentDialog : Form
{
    // Hub dark-theme tokens (hub/static/css/tokens.css). Kept literal rather than themed: the
    // helper runs as SYSTEM, so HKCU's light/dark preference belongs to SYSTEM, not to the
    // person at the keyboard, and guessing wrong looks worse than committing to one look.
    private static readonly Color Bg = ColorTranslator.FromHtml("#0b0e14");
    private static readonly Color Card = ColorTranslator.FromHtml("#171b24");
    private static readonly Color CardBorder = ColorTranslator.FromHtml("#262b38");
    private static readonly Color TextColor = ColorTranslator.FromHtml("#e6e8ec");
    private static readonly Color Muted = ColorTranslator.FromHtml("#8b93a7");
    private static readonly Color Accent = ColorTranslator.FromHtml("#3b82f6");
    private static readonly Color AccentHover = ColorTranslator.FromHtml("#5b93f5");
    private static readonly Color Danger = ColorTranslator.FromHtml("#ef4444");

    private const int CornerRadius = 12;

    private readonly int _timeoutMs;
    private readonly Stopwatch _elapsed = Stopwatch.StartNew();
    private readonly System.Windows.Forms.Timer _tick;
    private readonly Label _countdown;
    private readonly CountdownBar _bar;
    private readonly string _machine;
    private readonly string _who;

    /// <summary>True only if the user pressed Allow. Read after <see cref="ShowDialog()"/>.</summary>
    public bool Approved { get; private set; }

    internal ConsentDialog(string machine, string who, int timeoutSeconds)
    {
        _machine = machine;
        _who = who;
        _timeoutMs = timeoutSeconds * 1000;

        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.Manual;
        ShowInTaskbar = false;
        TopMost = true;
        BackColor = Card;
        ForeColor = TextColor;
        Font = new Font("Segoe UI", 9f, FontStyle.Regular, GraphicsUnit.Point);
        AutoScaleMode = AutoScaleMode.Dpi;
        ClientSize = new Size(460, 268);
        DoubleBuffered = true;
        KeyPreview = true;

        _bar = new CountdownBar
        {
            Bounds = new Rectangle(28, ClientSize.Height - 96, ClientSize.Width - 56, 4),
            Anchor = AnchorStyles.Left | AnchorStyles.Right | AnchorStyles.Bottom,
        };
        Controls.Add(_bar);

        _countdown = new Label
        {
            AutoSize = true,
            ForeColor = Muted,
            BackColor = Color.Transparent,
            Location = new Point(28, ClientSize.Height - 86),
            Anchor = AnchorStyles.Left | AnchorStyles.Bottom,
        };
        Controls.Add(_countdown);

        // Deny is deliberately the quiet one. It is the safe answer, so it gets no alarm colour:
        // red here would read as "this is the dangerous button", which is backwards.
        var deny = new PillButton("Deny", Card, TextColor, CardBorder)
        {
            Size = new Size(104, 42),
            Location = new Point(ClientSize.Width - 28 - 104 - 12 - 116, ClientSize.Height - 26 - 42),
            Anchor = AnchorStyles.Right | AnchorStyles.Bottom,
            HoverBack = ColorTranslator.FromHtml("#1e2430"),
            HoverBorder = ColorTranslator.FromHtml("#3a4252"),
        };
        deny.Click += (_, _) => Close();

        var allow = new PillButton("Allow", Accent, Color.White, Accent)
        {
            Size = new Size(116, 42),
            Location = new Point(ClientSize.Width - 28 - 116, ClientSize.Height - 26 - 42),
            Anchor = AnchorStyles.Right | AnchorStyles.Bottom,
            HoverBack = AccentHover,
            HoverBorder = AccentHover,
        };
        allow.Click += (_, _) => { Approved = true; Close(); };

        Controls.Add(deny);
        Controls.Add(allow);

        // Esc denies (see OnKeyDown). No AcceptButton is set on purpose: Enter should never be
        // able to approve a screen share by reflex, and Deny holds the initial focus so a blind
        // Enter fails closed.
        ActiveControl = deny;

        UpdateCountdown();
        _tick = new System.Windows.Forms.Timer { Interval = 100 };
        _tick.Tick += (_, _) => UpdateCountdown();
        _tick.Start();
    }

    private void UpdateCountdown()
    {
        int remainingMs = Math.Max(0, _timeoutMs - (int)_elapsed.ElapsedMilliseconds);
        if (remainingMs == 0)
        {
            _tick.Stop();
            Close(); // Approved stays false: no answer is a denial.
            return;
        }

        int seconds = (int)Math.Ceiling(remainingMs / 1000.0);
        _countdown.Text = $"Denied automatically in {seconds} second{(seconds == 1 ? "" : "s")}";
        _countdown.ForeColor = remainingMs <= 10_000 ? Danger : Muted;
        _bar.SetProgress(remainingMs / (double)_timeoutMs, remainingMs <= 10_000 ? Danger : Accent);
    }

    protected override void OnKeyDown(KeyEventArgs e)
    {
        // KeyPreview routes this here whichever control has focus. Esc closes with Approved
        // still false, i.e. it denies.
        if (e.KeyCode == Keys.Escape)
        {
            e.Handled = true;
            Close();
            return;
        }
        base.OnKeyDown(e);
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        var g = e.Graphics;
        g.SmoothingMode = SmoothingMode.AntiAlias;
        g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;

        // Accent rail down the left edge -- the one bit of colour that says "this is FleetHub"
        // before the user has read a word.
        using (var rail = new LinearGradientBrush(
                   new Rectangle(0, 0, 4, ClientSize.Height), Accent, AccentHover, 90f))
        {
            g.FillRectangle(rail, 0, 0, 4, ClientSize.Height);
        }

        DrawMonitorGlyph(g, new Rectangle(28, 28, 44, 44));

        int textLeft = 88;
        int right = ClientSize.Width - 28;
        using var title = new Font(Font.FontFamily, 14.5f, FontStyle.Regular);
        using var strong = new Font(Font.FontFamily, 9.75f, FontStyle.Bold);
        using var body = new Font(Font.FontFamily, 9.75f, FontStyle.Regular);
        using var textBrush = new SolidBrush(TextColor);
        using var mutedBrush = new SolidBrush(Muted);

        g.DrawString("Remote support request", title, textBrush, textLeft, 26);

        // Operator on its own line, machine-independent copy under it: an email is long enough
        // that flowing it into a sentence would wrap unpredictably.
        var wrap = new StringFormat { Trimming = StringTrimming.EllipsisCharacter };
        g.DrawString(_who, strong, textBrush, new RectangleF(textLeft, 60, right - textLeft, 20), wrap);
        g.DrawString("wants to view and control this computer.", body, mutedBrush,
                     new RectangleF(textLeft, 80, right - textLeft, 20), wrap);

        // The machine name, in its own quiet chip: the user needs to confirm it is *their* PC
        // being asked about, not a lookalike prompt naming somewhere else.
        DrawChip(g, new Rectangle(28, 124, ClientSize.Width - 56, 34), _machine);
    }

    private void DrawChip(Graphics g, Rectangle r, string text)
    {
        using var path = RoundedRect(r, 8);
        using var fill = new SolidBrush(Bg);
        using var pen = new Pen(CardBorder);
        g.FillPath(fill, path);
        g.DrawPath(pen, path);

        using var label = new Font(Font.FontFamily, 8.25f, FontStyle.Regular);
        using var value = new Font(Font.FontFamily, 9.75f, FontStyle.Bold);
        using var mutedBrush = new SolidBrush(Muted);
        using var textBrush = new SolidBrush(TextColor);
        var mid = new StringFormat { LineAlignment = StringAlignment.Center };
        g.DrawString("THIS COMPUTER", label, mutedBrush,
                     new RectangleF(r.X + 12, r.Y, 110, r.Height), mid);
        g.DrawString(text, value, textBrush,
                     new RectangleF(r.X + 118, r.Y, r.Width - 130, r.Height),
                     new StringFormat(mid) { Trimming = StringTrimming.EllipsisCharacter });
    }

    /// <summary>A small monitor-with-an-eye mark, drawn rather than shipped as a resource so the
    /// single-file publish stays one file and the glyph scales cleanly at any DPI.</summary>
    private static void DrawMonitorGlyph(Graphics g, Rectangle box)
    {
        using (var badge = RoundedRect(box, 12))
        using (var fill = new SolidBrush(Color.FromArgb(38, Accent)))
        using (var pen = new Pen(Color.FromArgb(90, Accent)))
        {
            g.FillPath(fill, badge);
            g.DrawPath(pen, badge);
        }

        var screen = new Rectangle(box.X + 11, box.Y + 12, box.Width - 22, box.Height - 24);
        using var accent = new Pen(Accent, 1.8f);
        using var accentFill = new SolidBrush(Accent);
        using (var path = RoundedRect(screen, 3))
        {
            g.DrawPath(accent, path);
        }
        // Stand.
        g.DrawLine(accent, box.X + 18, screen.Bottom + 6, box.Right - 18, screen.Bottom + 6);
        g.DrawLine(accent, box.X + box.Width / 2, screen.Bottom, box.X + box.Width / 2, screen.Bottom + 6);
        // Pupil: the "being watched" cue.
        g.FillEllipse(accentFill, screen.X + screen.Width / 2 - 2, screen.Y + screen.Height / 2 - 2, 4, 4);
    }

    private static GraphicsPath RoundedRect(Rectangle r, int radius)
    {
        int d = radius * 2;
        var path = new GraphicsPath();
        path.AddArc(r.X, r.Y, d, d, 180, 90);
        path.AddArc(r.Right - d - 1, r.Y, d, d, 270, 90);
        path.AddArc(r.Right - d - 1, r.Bottom - d - 1, d, d, 0, 90);
        path.AddArc(r.X, r.Bottom - d - 1, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        ApplyRoundedRegion();
        CenterOnPrimaryScreen();
    }

    protected override void OnSizeChanged(EventArgs e)
    {
        base.OnSizeChanged(e);
        if (IsHandleCreated) ApplyRoundedRegion();
    }

    private void ApplyRoundedRegion()
    {
        var old = Region;
        using var path = RoundedRect(new Rectangle(0, 0, Width, Height), CornerRadius);
        Region = new Region(path);
        old?.Dispose();
    }

    private void CenterOnPrimaryScreen()
    {
        // Screen.PrimaryScreen can be null on a session with no attached display; the default
        // top-left placement is harmless in that case (nobody is looking at it anyway).
        var area = Screen.PrimaryScreen?.WorkingArea;
        if (area is { } a)
        {
            Location = new Point(a.X + (a.Width - Width) / 2, a.Y + (a.Height - Height) / 2);
        }
    }

    protected override CreateParams CreateParams
    {
        get
        {
            var cp = base.CreateParams;
            cp.ClassStyle |= CS_DROPSHADOW;
            return cp;
        }
    }

    protected override void OnShown(EventArgs e)
    {
        base.OnShown(e);
        // TopMost alone does not guarantee focus when the shell has an active window; the
        // MessageBox this replaced got that from MB_SETFOREGROUND.
        Activate();
        SetForegroundWindow(Handle);
    }

    protected override void OnMouseDown(MouseEventArgs e)
    {
        base.OnMouseDown(e);
        // Borderless windows have no title bar, so give the whole card one: dragging it lets the
        // user uncover whatever the prompt happens to be sitting on before deciding.
        if (e.Button == MouseButtons.Left)
        {
            ReleaseCapture();
            SendMessage(Handle, WM_NCLBUTTONDOWN, (IntPtr)HTCAPTION, IntPtr.Zero);
        }
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) _tick?.Dispose();
        base.Dispose(disposing);
    }

    /// <summary>The draining timeout bar. Its own control so a repaint every 100ms invalidates
    /// four pixels of height instead of the whole card.</summary>
    private sealed class CountdownBar : Control
    {
        private double _fraction = 1.0;
        private Color _color = Accent;

        internal CountdownBar()
        {
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer
                     | ControlStyles.UserPaint | ControlStyles.ResizeRedraw, true);
            TabStop = false;
        }

        internal void SetProgress(double fraction, Color color)
        {
            fraction = Math.Clamp(fraction, 0, 1);
            if (Math.Abs(fraction - _fraction) < 0.001 && color == _color) return;
            _fraction = fraction;
            _color = color;
            Invalidate();
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            var track = new Rectangle(0, 0, Width, Height);
            using (var path = RoundedRect(track, Height / 2))
            using (var brush = new SolidBrush(CardBorder))
            {
                g.FillPath(brush, path);
            }

            int filled = (int)Math.Round(Width * _fraction);
            if (filled < Height) return;
            using (var path = RoundedRect(new Rectangle(0, 0, filled, Height), Height / 2))
            using (var brush = new SolidBrush(_color))
            {
                g.FillPath(brush, path);
            }
        }
    }

    /// <summary>A flat rounded button. WinForms' own FlatStyle draws square corners and a focus
    /// rectangle that would break the card's look, so the whole face is drawn here.</summary>
    private sealed class PillButton : Control
    {
        private readonly Color _back;
        private readonly Color _fore;
        private readonly Color _border;
        private const int FocusRingInset = 3;
        private bool _hover;
        private bool _pressed;

        // Fields, not properties: WinForms' designer-serialization analyzer (WFO1000) wants
        // attributes on public control properties, and nothing here is ever designer-hosted.
        internal Color HoverBack;
        internal Color HoverBorder;

        internal PillButton(string text, Color back, Color fore, Color border)
        {
            _back = HoverBack = back;
            _fore = fore;
            _border = HoverBorder = border;
            Text = text;
            SetStyle(ControlStyles.AllPaintingInWmPaint | ControlStyles.OptimizedDoubleBuffer
                     | ControlStyles.UserPaint | ControlStyles.ResizeRedraw
                     | ControlStyles.Selectable, true);
            Cursor = Cursors.Hand;
        }

        protected override void OnMouseEnter(EventArgs e) { _hover = true; Invalidate(); base.OnMouseEnter(e); }
        protected override void OnMouseLeave(EventArgs e) { _hover = false; _pressed = false; Invalidate(); base.OnMouseLeave(e); }
        protected override void OnMouseDown(MouseEventArgs e) { _pressed = true; Focus(); Invalidate(); base.OnMouseDown(e); }
        protected override void OnMouseUp(MouseEventArgs e) { _pressed = false; Invalidate(); base.OnMouseUp(e); }
        protected override void OnGotFocus(EventArgs e) { Invalidate(); base.OnGotFocus(e); }
        protected override void OnLostFocus(EventArgs e) { Invalidate(); base.OnLostFocus(e); }

        protected override bool IsInputKey(Keys keyData) =>
            keyData is Keys.Enter or Keys.Space || base.IsInputKey(keyData);

        protected override void OnKeyDown(KeyEventArgs e)
        {
            if (e.KeyCode is Keys.Enter or Keys.Space)
            {
                e.Handled = true;
                PerformClick();
                return;
            }
            base.OnKeyDown(e);
        }

        protected override void OnPaint(PaintEventArgs e)
        {
            var g = e.Graphics;
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;

            // The face is inset so the focus ring can sit *outside* it. Recolouring the border on
            // focus would be invisible on Allow, whose border already is the accent.
            var face = new Rectangle(FocusRingInset, FocusRingInset,
                                     Width - 1 - FocusRingInset * 2, Height - 1 - FocusRingInset * 2);
            var back = _hover || _pressed ? HoverBack : _back;
            var border = _hover || _pressed ? HoverBorder : _border;
            if (_pressed) back = ControlPaint.Dark(back, 0.02f);

            if (Focused)
            {
                using var ring = new Pen(AccentHover, 1.5f);
                using var ringPath = RoundedRect(new Rectangle(0, 0, Width - 1, Height - 1), 11);
                g.DrawPath(ring, ringPath);
            }

            using (var path = RoundedRect(face, 8))
            using (var brush = new SolidBrush(back))
            using (var pen = new Pen(border, 1f))
            {
                g.FillPath(brush, path);
                g.DrawPath(pen, path);
            }

            using var brushText = new SolidBrush(_fore);
            using var font = new Font(Font.FontFamily, 9.75f, FontStyle.Bold);
            g.DrawString(Text, font, brushText, face, new StringFormat
            {
                Alignment = StringAlignment.Center,
                LineAlignment = StringAlignment.Center,
            });
        }

        internal void PerformClick() => OnClick(EventArgs.Empty);
    }

    private const int CS_DROPSHADOW = 0x00020000;
    private const int WM_NCLBUTTONDOWN = 0x00A1;
    private const int HTCAPTION = 2;

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll")]
    private static extern bool ReleaseCapture();

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr SendMessage(IntPtr hWnd, int msg, IntPtr wParam, IntPtr lParam);
}
