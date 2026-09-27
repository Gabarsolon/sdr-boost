#!/usr/bin/env python3
"""
SDR Boost GUI
=============
Pick an SDR white level and a softness, watch the curve, and apply it in one click.
Uses the same engine as the sdr-boost command line tool (sdr_boost.py).

Author: Gabarsolon
"""

import ctypes
import os
import re
import tkinter as tk
import traceback
from tkinter import font as tkfont
from tkinter import messagebox, ttk

import sdr_boost as sb

BOOST_RE = re.compile(r"SDR Boost (?:Max (?P<max>\d+)|(?P<white>\d+))(?P<soft> Soft(?: (?P<amount>[\d.]+))?)?"
                      r"(?P<neutral> Neutral)?\.icc$", re.IGNORECASE)
ACCENT, NORMAL, GRID, MUTED, WARN = "#2563eb", "#9ca3af", "#e5e7eb", "#6b7280", "#b45309"
TABLE_COLUMNS = sb.SDR_PROBES + (("HDR 1000", None),)


def parse_boost(name):
    """(white, soft, neutral) from a profile name this tool made, else None."""
    m = BOOST_RE.search(name or "")
    if not m:
        return None
    soft = float(m["amount"]) if m["amount"] else 1.0 if m["soft"] else 0.0
    return float(m["max"] or m["white"]), soft, bool(m["neutral"])


def enable_dpi_awareness():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.zoom = root.winfo_fpixels("1i") / 96
        self.small = tkfont.Font(family="Segoe UI", size=8)
        self.white = tk.DoubleVar(value=1000)
        self.soft = tk.DoubleVar(value=0.0)
        self.neutral = tk.BooleanVar(value=False)
        self.displays, self.display, self.cal, self.peak_in = [], None, None, 0.0
        root.title(f"SDR Boost {sb.__version__}")
        root.resizable(False, False)
        root.report_callback_exception = lambda *exc: messagebox.showerror(
            "SDR Boost", "".join(traceback.format_exception(*exc))[-1500:])
        self._build()
        self.load_displays()

    def px(self, v: float) -> int:
        return int(round(v * self.zoom))

    # ------------------------------------------------------------ layout
    def _build(self):
        gap = self.px(10)
        ttk.Style(self.root).configure("Treeview", rowheight=self.px(22))
        outer = ttk.Frame(self.root, padding=gap)
        outer.grid(sticky="nsew")

        head = ttk.Frame(outer)
        head.grid(row=0, column=0, columnspan=2, sticky="ew")
        head.columnconfigure(1, weight=1)
        ttk.Label(head, text="Display").grid(row=0, column=0, sticky="w")
        self.display_box = ttk.Combobox(head, state="readonly", width=48)
        self.display_box.grid(row=0, column=1, sticky="w", padx=gap)
        self.display_box.bind("<<ComboboxSelected>>", lambda _: self.load_display())
        ttk.Button(head, text="Refresh", command=self.load_displays).grid(row=0, column=2, sticky="e")
        self.info = ttk.Label(head, justify="left")
        self.info.grid(row=1, column=0, columnspan=3, sticky="w", pady=(gap // 2, 0))
        self.warning = ttk.Label(head, foreground=WARN, justify="left")
        self.warning.grid(row=2, column=0, columnspan=3, sticky="w")

        ctl = ttk.LabelFrame(outer, text="New boost", padding=gap)
        ctl.grid(row=1, column=0, sticky="nsew", pady=gap, padx=(0, gap))
        ttk.Label(ctl, text="SDR white").grid(row=0, column=0, sticky="w")
        self.white_scale = ttk.Scale(ctl, from_=490, to=1940, variable=self.white,
                                     command=self.update_preview, length=self.px(250))
        self.white_scale.grid(row=0, column=1, sticky="ew", padx=gap)
        self.white_label = ttk.Label(ctl, width=9)
        self.white_label.grid(row=0, column=2, sticky="w")
        ttk.Label(ctl, text="Softness").grid(row=1, column=0, sticky="w", pady=(gap // 2, 0))
        ttk.Scale(ctl, from_=0, to=3, variable=self.soft, command=self.update_preview,
                  length=self.px(250)).grid(row=1, column=1, sticky="ew", padx=gap, pady=(gap // 2, 0))
        self.soft_label = ttk.Label(ctl, width=9)
        self.soft_label.grid(row=1, column=2, sticky="w", pady=(gap // 2, 0))
        ttk.Label(ctl, text="0 = normal contrast, 1 = brighter darks and gentler whites",
                  foreground=MUTED).grid(row=2, column=1, columnspan=2, sticky="w", padx=gap)
        ttk.Checkbutton(ctl, text="Neutral colors (drop the calibration's saturation boost)",
                        variable=self.neutral, command=self.update_preview).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(gap, 0))

        table = ttk.Frame(ctl)
        table.grid(row=4, column=0, columnspan=3, sticky="w", pady=(gap, 0))
        for c, (label, _) in enumerate(TABLE_COLUMNS, start=1):
            ttk.Label(table, text=label, foreground=MUTED).grid(row=0, column=c, sticky="e", padx=(gap, 0))
        self.cells = {}
        for r, row in enumerate(("normal", "boosted"), start=1):
            ttk.Label(table, text=row, foreground=MUTED).grid(row=r, column=0, sticky="w")
            for c in range(1, len(TABLE_COLUMNS) + 1):
                self.cells[row, c] = ttk.Label(table, width=7, anchor="e")
                self.cells[row, c].grid(row=r, column=c, sticky="e", padx=(gap, 0))
        ttk.Label(ctl, text="nits; dark bg = a dark-mode window background", foreground=MUTED).grid(
            row=5, column=0, columnspan=3, sticky="w")

        self.name_label = ttk.Label(ctl, font=("Segoe UI", 9, "bold"))
        self.name_label.grid(row=6, column=0, columnspan=3, sticky="w", pady=(gap, 0))
        self.notes = ttk.Label(ctl, foreground=WARN, justify="left", wraplength=self.px(420))
        self.notes.grid(row=7, column=0, columnspan=3, sticky="w")
        buttons = ttk.Frame(ctl)
        buttons.grid(row=8, column=0, columnspan=3, sticky="w", pady=(gap, 0))
        self.create_button = ttk.Button(buttons, text="Create & Apply", command=self.create_apply, default="active")
        self.create_button.grid(row=0, column=0)
        ttk.Button(buttons, text="Back to normal", command=self.back_to_normal).grid(row=0, column=1, padx=gap)

        plot = ttk.LabelFrame(outer, text="Curve", padding=gap // 2)
        plot.grid(row=1, column=1, sticky="nsew", pady=gap)
        self.cw, self.ch = self.px(400), self.px(330)
        self.canvas = tk.Canvas(plot, width=self.cw, height=self.ch, background="white", highlightthickness=0)
        self.canvas.grid()

        lst = ttk.LabelFrame(outer, text="Profiles for this display (double-click to apply)", padding=gap)
        lst.grid(row=2, column=0, columnspan=2, sticky="nsew")
        lst.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(lst, columns=("state", "name"), show="headings", height=7, selectmode="browse")
        self.tree.heading("state", text="")
        self.tree.heading("name", text="Profile", anchor="w")
        self.tree.column("state", width=self.px(64), stretch=False, anchor="center")
        self.tree.column("name", width=self.px(600))
        self.tree.tag_configure("active", font=("Segoe UI", 9, "bold"))
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", lambda _: self.apply_selected())
        bar = ttk.Scrollbar(lst, orient="vertical", command=self.tree.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=bar.set)
        side = ttk.Frame(lst)
        side.grid(row=0, column=2, sticky="n", padx=(gap, 0))
        ttk.Button(side, text="Apply", command=self.apply_selected).grid(row=0, column=0, sticky="ew")
        ttk.Button(side, text="Remove", command=self.remove_selected).grid(row=1, column=0, sticky="ew",
                                                                            pady=(gap // 2, 0))

        self.status = ttk.Label(outer, foreground=MUTED)
        self.status.grid(row=3, column=0, columnspan=2, sticky="w", pady=(gap // 2, 0))

    # ------------------------------------------------------------ state
    def load_displays(self):
        try:
            self.displays = sb.active_displays()
        except SystemExit as e:
            messagebox.showerror("SDR Boost", str(e))
            return
        self.display_box["values"] = [f"{d.name}  (HDR {'on' if d.hdr else 'off'})" for d in self.displays]
        keep = [i for i, d in enumerate(self.displays) if self.display and d.path == self.display.path]
        hdr = [i for i, d in enumerate(self.displays) if d.hdr]
        self.display_box.current((keep or hdr or [0])[0])
        self.display_box.configure(state="readonly" if len(self.displays) > 1 else "disabled")
        self.load_display()

    def load_display(self):
        self.display = self.displays[self.display_box.current()]
        try:
            self.cal = sb.calibration_for(self.display, None)
            self.peak_in = sb.peak_in_for(self.display, self.cal)
            self.white_scale.configure(to=self.cal.peak)
        except SystemExit:
            self.cal = None
        self.sync_sliders(sb.active_profile(self.display))
        self.refresh()

    def sync_sliders(self, profile):
        boost = parse_boost(profile)
        if boost:
            self.white.set(boost[0])
            self.soft.set(boost[1])
            self.neutral.set(boost[2])

    def refresh(self):
        d, c = self.display, self.cal
        active = sb.active_profile(d)
        lines = [f"HDR {'on' if d.hdr else 'off'}" + (
                     f", SDR content brightness {d.sdr_white:.0f} nits (slider {(d.sdr_white - 80) / 4:.0f})"
                     if d.hdr else ""),
                 f"Active profile: {active or '-'}"]
        if c:
            lines.append(f"Calibration: {os.path.basename(c.path)}  (peak {c.peak:.0f}, full-screen "
                         f"{c.full_frame:.0f} nits, saturation boost {'on' if c.saturation else 'off'})")
        self.info.config(text="\n".join(lines))

        warnings = []
        if not d.hdr:
            warnings.append("HDR is off. Boost profiles only work with HDR on (Win+Alt+B).")
        elif abs(d.sdr_white - sb.SDR_WHITE_IN) > 1:
            warnings.append("SDR content brightness isn't at 100, so white lands below the chosen level.")
        if not c:
            warnings.append("No Windows HDR Calibration profile for this display. Run the HDR Calibration app first.")
        self.warning.config(text="\n".join(warnings))
        self.warning.grid() if warnings else self.warning.grid_remove()
        self.create_button.state(["!disabled"] if c else ["disabled"])
        self.load_profiles(active)
        self.update_preview()

    def load_profiles(self, active):
        self.tree.delete(*self.tree.get_children())
        base = os.path.basename(self.cal.path) if self.cal else None

        def order(name):
            boost = parse_boost(name)
            if boost:
                return 1, boost[0], boost[1], name
            path = os.path.join(sb.COLOR_DIR, name)
            return 0, -(os.path.getmtime(path) if os.path.exists(path) else 0), 0, name  # newest calibration first

        for name in sorted(sb.known_profiles(self.display), key=order):
            state = "active" if name == active else "normal" if name == base else ""
            self.tree.insert("", "end", iid=name, values=(state, name.removesuffix(".icc")),
                             tags=("active",) if name == active else ())
        if active and self.tree.exists(active):
            self.tree.see(active)

    # ------------------------------------------------------------ preview
    def settings(self):
        return round(self.white.get() / 10) * 10, round(self.soft.get(), 1), self.neutral.get()

    def update_preview(self, *_):
        white, soft, neutral = self.settings()
        self.white_label.config(text=f"{white:.0f} nits")
        self.soft_label.config(text=f"{soft:.1f}")
        if not self.cal:
            self.canvas.delete("all")
            return
        f, effective, name = sb.boost_for(self.display, self.cal, white, soft, neutral, self.peak_in)
        self.name_label.config(text=name)
        self.notes.config(text="\n".join(sb.boost_notes(self.cal, white, effective)))
        for c, (_, share) in enumerate(TABLE_COLUMNS, start=1):
            n = share * sb.SDR_WHITE_IN if share is not None else 1000.0
            self.cells["normal", c].config(text=sb.fmt(min(n, self.cal.peak)))
            self.cells["boosted", c].config(text=sb.fmt(f(n)))
        self.draw_curve(f, effective)

    def draw_curve(self, f, white):
        c, cal, s = self.canvas, self.cal, self.px
        c.delete("all")
        left, right, top, bottom = s(46), s(14), s(12), s(38)
        span_x, span_y = self.cw - left - right, self.ch - top - bottom
        top_pq = sb.nits_to_pq(max(self.peak_in, cal.peak))
        x = lambda n: left + span_x * sb.nits_to_pq(n) / top_pq  # both axes on the PQ (perceptual) scale
        y = lambda n: top + span_y * (1 - sb.nits_to_pq(n) / top_pq)

        for t in [t for t in (1, 10, 100, 480, 1000) if t < cal.peak * 0.8] + [round(cal.peak)]:
            c.create_line(x(t), top, x(t), top + span_y, fill=GRID)
            c.create_line(left, y(t), left + span_x, y(t), fill=GRID)
            c.create_text(x(t), top + span_y + s(3), text=t, anchor="n", fill=MUTED, font=self.small)
            c.create_text(left - s(4), y(t), text=t, anchor="e", fill=MUTED, font=self.small)
        c.create_text(left + span_x / 2, self.ch - s(2), text="signal from Windows (nits)", anchor="s",
                      fill=MUTED, font=self.small)
        c.create_text(s(9), top + span_y / 2, text="on screen (nits)", angle=90, fill=MUTED, font=self.small)

        if cal.full_frame:
            fy = y(cal.full_frame)
            c.create_line(left, fy, left + span_x, fy, fill=WARN, dash=(4, 3))
            c.create_text(left + s(4), fy + s(2), text=f"full-screen max {cal.full_frame:.0f}", anchor="nw",
                          fill=WARN, font=self.small)
        sx = x(sb.SDR_WHITE_IN)
        c.create_line(sx, top, sx, top + span_y, fill=MUTED, dash=(2, 3))
        c.create_text(sx - s(4), top + span_y - s(3), text="SDR", anchor="se", fill=MUTED, font=self.small)
        c.create_text(sx + s(4), top + span_y - s(3), text="HDR", anchor="sw", fill=MUTED, font=self.small)

        steps = 240
        signal = [sb.pq_to_nits(top_pq * i / steps) for i in range(steps + 1)]
        c.create_line(*[v for n in signal for v in (x(n), y(min(n, cal.peak)))], fill=NORMAL, width=s(1.5), dash=(5, 4))
        c.create_line(*[v for n in signal for v in (x(n), y(f(n)))], fill=ACCENT, width=s(2.5))
        wx, wy, r = sx, y(f(sb.SDR_WHITE_IN)), s(4)
        c.create_oval(wx - r, wy - r, wx + r, wy + r, fill=ACCENT, outline="")
        c.create_text(wx - s(8), wy - s(4), text=f"SDR white {white:.0f}", anchor="se", fill=ACCENT, font=self.small)

        lx, ly = x(10) + s(14), y(1) - s(14)  # below both curves, left of the SDR/HDR divider
        c.create_line(lx, ly, lx + s(18), ly, fill=ACCENT, width=s(2.5))
        c.create_text(lx + s(24), ly, text="boosted", anchor="w", fill=MUTED, font=self.small)
        c.create_line(lx, ly + s(15), lx + s(18), ly + s(15), fill=NORMAL, width=s(1.5), dash=(5, 4))
        c.create_text(lx + s(24), ly + s(15), text="normal", anchor="w", fill=MUTED, font=self.small)

    # ------------------------------------------------------------ actions
    def run(self, action, done) -> bool:
        self.root.config(cursor="watch")
        self.root.update_idletasks()
        try:
            result = action()
        except (SystemExit, OSError) as e:
            messagebox.showerror("SDR Boost", str(e))
            return False
        finally:
            self.root.config(cursor="")
        self.status.config(text=done(result) if callable(done) else done)
        self.refresh()
        return True

    def create_apply(self):
        white, soft, neutral = self.settings()
        self.run(lambda: sb.create_profile(self.display, self.cal, white, soft, neutral, True),
                 lambda result: f"Applied {result[0].removesuffix('.icc')}")

    def back_to_normal(self):
        if self.cal:
            base = os.path.basename(self.cal.path)
            self.run(lambda: sb.link(self.display, base, True), f"Back to normal: {base.removesuffix('.icc')}")

    def apply_selected(self):
        selected = self.tree.selection()
        if selected and self.run(lambda: sb.link(self.display, selected[0], True),
                                 f"Applied {selected[0].removesuffix('.icc')}"):
            self.sync_sliders(selected[0])
            self.update_preview()

    def remove_selected(self):
        selected = self.tree.selection()
        if not selected:
            return
        name = selected[0]
        if "SDR Boost" not in name:
            messagebox.showinfo("SDR Boost", "Only SDR Boost profiles can be removed here.")
        elif messagebox.askyesno("SDR Boost", f"Remove {name.removesuffix('.icc')}?"):
            self.run(lambda: sb.remove_profile(self.display, name), f"Removed {name.removesuffix('.icc')}")


def main():
    enable_dpi_awareness()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
