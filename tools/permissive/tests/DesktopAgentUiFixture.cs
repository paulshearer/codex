using System;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.Text;
using System.Web.Script.Serialization;
using System.Windows.Forms;

// Build and run only inside the isolated desktop acceptance VM.
internal static class DesktopAgentUiFixture {
    [STAThread]
    private static void Main(string[] args) {
        if (args.Length != 1) throw new ArgumentException("Expected an absolute evidence JSON output path.");
        string evidence = Path.GetFullPath(args[0]);
        var json = new JavaScriptSerializer();
        var form = new Form { Text = "Codex desktop acceptance fixture", Width = 660, Height = 560, StartPosition = FormStartPosition.CenterScreen };
        var left = new TextBox { Name = "LeftOperand", AccessibleName = "Left operand", Left = 20, Top = 20, Width = 120, Text = "0" };
        var right = new TextBox { Name = "RightOperand", AccessibleName = "Right operand", Left = 160, Top = 20, Width = 120, Text = "0" };
        var calculate = new Button { Name = "Calculate", AccessibleName = "Calculate", Text = "Calculate", Left = 300, Top = 18, Width = 120 };
        var result = new Label { Name = "Product", AccessibleName = "Product result", Text = "Result: 0", Left = 440, Top = 23, Width = 180 };
        var note = new TextBox { Name = "Note", AccessibleName = "Note", Left = 20, Top = 65, Width = 590, Height = 90, Multiline = true };
        var check = new CheckBox { Name = "Enabled", AccessibleName = "Enabled", Text = "Enabled", ThreeState = true, Left = 20, Top = 170, Width = 120 };
        var choice = new ListBox { Name = "Choice", AccessibleName = "Choice", Left = 160, Top = 170, Width = 150, Height = 80 };
        choice.Items.AddRange(new object[] { "Alpha", "Beta", "Gamma" });
        var save = new Button { Name = "Save", AccessibleName = "Save evidence", Text = "Save evidence", Left = 350, Top = 170, Width = 150 };
        var modal = new Button { Name = "Modal", AccessibleName = "Open modal", Text = "Open modal", Left = 350, Top = 215, Width = 150 };
        var canvas = new Panel { Name = "Canvas", AccessibleName = "Physical input canvas", Left = 20, Top = 280, Width = 590, Height = 160, BackColor = Color.White };
        int product = 0, saves = 0, clicks = 0, drags = 0; Point? origin = null;
        Action write = () => {
            var directory = Path.GetDirectoryName(evidence); if (!Directory.Exists(directory)) Directory.CreateDirectory(directory);
            File.WriteAllText(evidence, json.Serialize(new { left = left.Text, right = right.Text, product, note = note.Text,
                enabled = check.CheckState.ToString(), selected = choice.SelectedItem, saves, clicks, drags }), new UTF8Encoding(false));
        };
        calculate.Click += (sender, ev) => {
            int first, second;
            if (Int32.TryParse(left.Text, NumberStyles.Integer, CultureInfo.InvariantCulture, out first) && Int32.TryParse(right.Text, NumberStyles.Integer, CultureInfo.InvariantCulture, out second)) {
                product = first * second; result.Text = "Result: " + product; write();
            }
        };
        save.Click += (sender, ev) => { saves++; write(); };
        modal.Click += (sender, ev) => MessageBox.Show(form, "Modal evidence is visible", "Fixture modal", MessageBoxButtons.OK);
        canvas.Paint += (sender, ev) => { ev.Graphics.DrawRectangle(Pens.Navy, 15, 15, 550, 120); ev.Graphics.DrawString("Canvas: click or drag here", SystemFonts.DefaultFont, Brushes.Navy, 25, 25); };
        canvas.MouseDown += (sender, ev) => { origin = ev.Location; };
        canvas.MouseUp += (sender, ev) => { if (origin.HasValue && (Math.Abs(origin.Value.X - ev.X) > 5 || Math.Abs(origin.Value.Y - ev.Y) > 5)) drags++; else clicks++; origin = null; write(); };
        form.Controls.AddRange(new Control[] { left, right, calculate, result, note, check, choice, save, modal, canvas });
        Application.EnableVisualStyles(); Application.Run(form);
    }
}
