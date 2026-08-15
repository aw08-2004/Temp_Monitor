using System.Drawing;
using System.Drawing.Drawing2D;
using System.Diagnostics;
using System.Windows.Forms;

namespace TempMonitorAgent.UserMessage;

/// <summary>
/// The dialog a rule puts on the signed-in user's desktop, and the buttons it offers.
///
/// Styling deliberately mirrors <see cref="Remote.ConsentDialog"/> -- the hub's dark tokens
/// (bg #0b0e14 / card #171b24 / accent #3b82f6) -- for the same reason: this and the consent
/// prompt are the only two things an end user ever sees of this product, and they should
/// look like the same product rather than like two different stray system errors.
///
/// <b>The dialog knows nothing about what its buttons MEAN.</b> It is handed a list of ids
/// and labels, and it reports back which id was pressed. Deciding that "yes" restarts the
/// machine and "later" defers for four hours is the hub's business (see rules.py's
/// on_response routing), which is what lets an operator change what "Later" does without a
/// new agent release having to be built, signed and rolled out.
///
/// Every exit reports something. Esc and the close box are <c>dismissed</c>, the countdown
/// running out is <c>timeout</c>, and neither is silently treated as one of the buttons --
/// "nobody answered" and "they said no" are different facts and the rule may want to treat
/// them differently.
/// </summary>
internal sealed class MessageDialog : Form
{
    private static readonly Color Bg = ColorTranslator.FromHtml("#0b0e14");
    private static readonly Color Card = ColorTranslator.FromHtml("#171b24");
    private static readonly Color CardBorder = ColorTranslator.FromHtml("#262b38");
    private static readonly Color TextColor = ColorTranslator.FromHtml("#e6e8ec");
    private static readonly Color Muted = ColorTranslator.FromHtml("#8b93a7");
    private static readonly Color Accent = ColorTranslator.FromHtml("#3b82f6");
    private static readonly Color AccentHover = ColorTranslator.FromHtml("#5b93f5");

    private const int CornerRadius = 12;
    private const int Pad = 28;

    /// <summary>What the user did: a button id, or "dismissed"/"timeout".</summary>
    public string Outcome { get; private set; } = MessageOutcomes.Dismissed;

    private readonly int _timeoutMs;
    private readonly Stopwatch _elapsed = Stopwatch.StartNew();
    private readonly System.Windows.Forms.Timer? _tick;
    private readonly Label? _countdown;

    internal MessageDialog(MessageRequest request)
    {
        _timeoutMs = request.TimeoutSeconds > 0 ? request.TimeoutSeconds * 1000 : 0;

        FormBorderStyle = FormBorderStyle.None;
        StartPosition = FormStartPosition.Manual;
        ShowInTaskbar = false;
        TopMost = true;
        BackColor = Card;
        ForeColor = TextColor;
        Font = new Font("Segoe UI", 9f, FontStyle.Regular, GraphicsUnit.Point);
        AutoScaleMode = AutoScaleMode.Dpi;
        DoubleBuffered = true;
        KeyPreview = true;

        var title = new Label
        {
            Text = request.Title,
            AutoSize = false,
            ForeColor = TextColor,
            BackColor = Color.Transparent,
            Font = new Font("Segoe UI", 13f, FontStyle.Bold, GraphicsUnit.Point),
            Location = new Point(Pad, Pad),
            Size = new Size(460 - (Pad * 2), 30),
        };

        var body = new Label
        {
            Text = request.Body,
            AutoSize = false,
            ForeColor = Muted,
            BackColor = Color.Transparent,
            Location = new Point(Pad, Pad + 38),
            Size = new Size(460 - (Pad * 2), 96),
        };

        Controls.Add(title);
        Controls.Add(body);

        // Buttons are laid out right-to-left from the bottom-right corner, so the primary
        // action lands where Windows users expect it whatever the count.
        int buttonTop = 176;
        int right = 460 - Pad;
        var buttons = request.Buttons.Count > 0
            ? request.Buttons
            : new List<MessageButton> { new() { Id = MessageOutcomes.Ok, Label = "OK" } };

        Button? defaultButton = null;
        foreach (var spec in Enumerable.Reverse(buttons))
        {
            bool primary = spec.Id == request.DefaultButton
                           || (request.DefaultButton is null && ReferenceEquals(spec, buttons[0]));
            var button = MakeButton(spec.Label ?? spec.Id, primary);
            button.Location = new Point(right - button.Width, buttonTop);
            right -= button.Width + 10;
            string id = spec.Id;
            button.Click += (_, _) => Finish(id);
            Controls.Add(button);
            if (spec.Id == request.DefaultButton) defaultButton = button;
        }

        ClientSize = new Size(460, _timeoutMs > 0 ? 254 : 226);

        if (_timeoutMs > 0)
        {
            _countdown = new Label
            {
                AutoSize = true,
                ForeColor = Muted,
                BackColor = Color.Transparent,
                Location = new Point(Pad, ClientSize.Height - 30),
            };
            Controls.Add(_countdown);
            _tick = new System.Windows.Forms.Timer { Interval = 250 };
            _tick.Tick += OnTick;
            _tick.Start();
            UpdateCountdown();
        }

        if (defaultButton is not null)
        {
            AcceptButton = defaultButton;
            Shown += (_, _) => defaultButton.Focus();
        }

        // Esc is "dismissed", never a button. A user hitting Escape has declined to engage,
        // which is not the same as pressing No -- and a rule that treats them the same can
        // say so explicitly by routing both outcomes to the same follow-up.
        KeyDown += (_, e) =>
        {
            if (e.KeyCode == Keys.Escape) Finish(MessageOutcomes.Dismissed);
        };

        CenterOnPrimaryScreen();
    }

    private void CenterOnPrimaryScreen()
    {
        var area = Screen.PrimaryScreen?.WorkingArea ?? new Rectangle(0, 0, 1280, 800);
        Location = new Point(area.Left + ((area.Width - ClientSize.Width) / 2),
                             area.Top + ((area.Height - ClientSize.Height) / 3));
    }

    private Button MakeButton(string text, bool primary)
    {
        var button = new Button
        {
            Text = text,
            AutoSize = false,
            Size = new Size(Math.Max(96, TextRenderer.MeasureText(text, Font).Width + 36), 34),
            FlatStyle = FlatStyle.Flat,
            ForeColor = primary ? Color.White : TextColor,
            BackColor = primary ? Accent : Card,
            Cursor = Cursors.Hand,
            UseVisualStyleBackColor = false,
        };
        button.FlatAppearance.BorderSize = primary ? 0 : 1;
        button.FlatAppearance.BorderColor = CardBorder;
        button.FlatAppearance.MouseOverBackColor = primary ? AccentHover : CardBorder;
        return button;
    }

    private void OnTick(object? sender, EventArgs e)
    {
        if (_elapsed.ElapsedMilliseconds >= _timeoutMs)
        {
            Finish(MessageOutcomes.Timeout);
            return;
        }
        UpdateCountdown();
    }

    private void UpdateCountdown()
    {
        if (_countdown is null) return;
        int left = Math.Max(0, (int)Math.Ceiling((_timeoutMs - _elapsed.ElapsedMilliseconds) / 1000.0));
        _countdown.Text = left == 1
            ? "This message closes in 1 second."
            : $"This message closes in {left} seconds.";
    }

    private void Finish(string outcome)
    {
        Outcome = outcome;
        _tick?.Stop();
        DialogResult = DialogResult.OK;
        Close();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;
        using var pen = new Pen(CardBorder);
        using var path = RoundedRect(new Rectangle(0, 0, ClientSize.Width - 1, ClientSize.Height - 1),
                                     CornerRadius);
        e.Graphics.DrawPath(pen, path);
    }

    protected override void OnHandleCreated(EventArgs e)
    {
        base.OnHandleCreated(e);
        using var path = RoundedRect(new Rectangle(0, 0, ClientSize.Width, ClientSize.Height),
                                     CornerRadius);
        Region = new Region(path);
    }

    private static GraphicsPath RoundedRect(Rectangle bounds, int radius)
    {
        int d = radius * 2;
        var path = new GraphicsPath();
        path.AddArc(bounds.X, bounds.Y, d, d, 180, 90);
        path.AddArc(bounds.Right - d, bounds.Y, d, d, 270, 90);
        path.AddArc(bounds.Right - d, bounds.Bottom - d, d, d, 0, 90);
        path.AddArc(bounds.X, bounds.Bottom - d, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }

    protected override void Dispose(bool disposing)
    {
        if (disposing) _tick?.Dispose();
        base.Dispose(disposing);
    }
}
