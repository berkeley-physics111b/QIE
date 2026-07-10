"""
Coincidence Counting GUI for the Altera DE2-115 FPGA counter board.

Requires:
    - fpga_interface.py  (the FPGAInterface class - renamed from
      "FPGA_DE2-115_interface.py" because Python module names cannot
      contain dashes or dots)
    - pyvisa, numpy

Run with:
    python counter_gui.py
"""

import csv
import os
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk, filedialog, messagebox

from altera_interface import FPGAInterface

# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def calculate_accidental_coinc_rate(rate_a, rate_b, resolution_ns):
    """
    Compute the accidental coincidence rate of the detectors based on Poisson statistics.
    """
    return rate_a * rate_b * resolution_ns

def calculate_accidental_counts(rate_a, rate_b, resolution_ns, update_period):
    """
    Compute the number of accidental coincidence counts
    expected over `update_period` seconds using calculate_accidental_coinc_rate.
    """
    return calculate_accidental_coinc_rate(rate_a, rate_b, resolution_ns) * update_period

# ----------------------------------------------------------------------
# Thermometer-style indicator widget
# ----------------------------------------------------------------------
class Thermometer(tk.Canvas):
    def __init__(self, parent, title, max_value=1_000_000, increment=50_000,
                 width=150, height=380, **kwargs):
        # NOTE: height/width are fixed and identical across every
        # Thermometer instance in the app (see
        # CounterApp._build_main_display). Rescaling (see set_scale below)
        # only redraws the tick marks and fill bar inside this fixed canvas
        # -- it never changes the widget's footprint -- which is what keeps
        # all thermometers lined up at the same height no matter what scale
        # each one is set to.
        super().__init__(parent, width=width, height=height, bg='white',
                          highlightthickness=1, highlightbackground='gray', **kwargs)
        self.title_text = title
        self.max_value = max_value
        self.increment = increment
        self.canvas_width = width
        self.canvas_height = height

        self.bar_top = 45
        self.bar_bottom = height - 20
        self.bar_left = width // 2 - 18
        self.bar_right = width // 2 + 18

        self._fill_rect = None
        self._current_value = 0.0
        self._redraw()

    def _value_to_y(self, value):
        frac = max(0.0, min(1.0, value / self.max_value))
        return self.bar_bottom - frac * (self.bar_bottom - self.bar_top)

    def _redraw(self):
        """Fully redraw the static elements (title, ticks, outline, fill)
        using the current max_value/increment, then restore the last value."""
        self.delete('all')
        self._draw_static()
        self.set_value(self._current_value)

    def _draw_static(self):
        self.create_text(self.canvas_width // 2, 18, text=self.title_text,
                          font=('Helvetica', 18, 'bold'))

        n_ticks = max(1, int(self.max_value / self.increment))
        for i in range(n_ticks + 1):
            val = i * self.increment
            y = self._value_to_y(val)
            self.create_line(self.bar_left - 6, y, self.bar_left, y)
            if i % 2 == 0:
                label = f'{val / 1000:g}k' if val >= 1000 else f'{val:g}'
                self.create_text(self.bar_left - 10, y, text=label,
                                  anchor='e', font=('Helvetica', 8))

        self.create_rectangle(self.bar_left, self.bar_top, self.bar_right,
                               self.bar_bottom, outline='black', width=2)

        self._fill_rect = self.create_rectangle(
            self.bar_left, self.bar_bottom, self.bar_right, self.bar_bottom,
            fill='#d9342b', outline='')

    def set_value(self, value):
        self._current_value = value
        value = max(0.0, min(value, self.max_value))
        y = self._value_to_y(value)
        self.coords(self._fill_rect, self.bar_left, y, self.bar_right, self.bar_bottom)

        # color creeps toward orange/red as it climbs
        frac = value / self.max_value
        if frac < 0.5:
            color = '#3a8f3a'
        elif frac < 0.8:
            color = '#e0a52c'
        else:
            color = '#d9342b'
        self.itemconfig(self._fill_rect, fill=color)

    def set_scale(self, max_value, increment=None):
        """Adjust the full-scale value (and optionally the tick spacing),
        then redraw. Canvas size never changes, so rescaling one
        thermometer cannot disturb the alignment of the others."""
        max_value = float(max_value)
        if max_value <= 0:
            raise ValueError("max_value must be positive")
        self.max_value = max_value
        self.increment = float(increment) if increment else max(1.0, max_value / 10)
        self._redraw()


# ----------------------------------------------------------------------
# Main application
# ----------------------------------------------------------------------
class CounterApp(tk.Tk):

    COUNTER_A_IDX = 0
    COUNTER_B_IDX = 1
    COUNTER_AB_IDX = 5

    STATUS_COLORS = {
        'Initializing': '#555555',
        'Reading Counters': '#1a5fb4',
        'Updated Counts': '#26a269',
        'Program Terminated': '#555555',
    }

    def __init__(self):
        super().__init__()
        self.title("DE2-115 Coincidence Counter")
        self.geometry("1150x780")

        self.fpga = FPGAInterface()
        self.connected = False
        self.running = False          # acquisition loop active
        self.snapshot_in_progress = False
        self._acq_thread = None
        self._snapshot_thread = None
        self._resume_after_snapshot = False

        self.last_counts = [0] * 8
        self.last_period = 1.0

        self._build_layout()

        # Any edit to these fires _on_param_changed, which timestamps the
        # "Params" indicator in the Connection panel so the user can see
        # their edit was actually picked up (these vars are re-read fresh
        # from their .get() on every acquisition/snapshot pass, so there is
        # no separate "push" step -- the confirmation just makes that
        # visible).
        for var in (self.update_period_var, self.resolution_var,
                    self.subtract_accidentals_var, self.round_display_var):
            var.trace_add('write', self._on_param_changed)

        self._refresh_ports()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _on_param_changed(self, *_args):
        ts = datetime.now().strftime('%H:%M:%S')
        self.params_synced_var.set(f"\u2713 Applied at {ts}")
        self.params_synced_label.configure(foreground='#26a269')

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _build_layout(self):
        outer = ttk.Frame(self)
        outer.pack(fill='both', expand=True, padx=8, pady=8)

        left = ttk.Frame(outer, width=300)
        left.pack(side='left', fill='y', padx=(0, 10))
        left.pack_propagate(False)

        right = ttk.Frame(outer)
        right.pack(side='left', fill='both', expand=True)

        self._build_connection_panel(left)
        self._build_settings_panel(left)
        self._build_snapshot_panel(left)
        self._build_counter_readout_panel(left)

        self._build_main_display(right)

    # ---- Connection panel ---------------------------------------------------
    def _build_connection_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Connection")
        frame.pack(fill='x', pady=(0, 8))

        ttk.Label(frame, text="COM Port:").grid(row=0, column=0, sticky='w', padx=4, pady=4)
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(frame, textvariable=self.port_var, state='readonly', width=18)
        self.port_combo.grid(row=0, column=1, padx=4, pady=4)
        self.port_combo.bind('<Button-1>', lambda e: self._refresh_ports())

        self.connect_btn = ttk.Button(frame, text="Connect", command=self._toggle_connect)
        self.connect_btn.grid(row=1, column=0, columnspan=2, sticky='we', padx=4, pady=(0, 4))

        ttk.Label(frame, text="Status:").grid(row=2, column=0, sticky='w', padx=4, pady=(4, 4))
        self.status_var = tk.StringVar(value="Not connected")
        self.status_label = ttk.Label(frame, textvariable=self.status_var, foreground='#555555',
                                       font=('Helvetica', 10, 'bold'), wraplength=180)
        self.status_label.grid(row=2, column=1, sticky='w', padx=4, pady=(4, 4))

        # Confirms that edits to live acquisition parameters (update period,
        # resolution, subtract-accidentals, round-display) have actually
        # been picked up by the running system. These values are re-read
        # from their tk variables on every acquisition pass, so the "push"
        # happens automatically -- this label just gives the user visible
        # proof it happened, via trace callbacks set up in __init__.
        ttk.Label(frame, text="Params:").grid(row=3, column=0, sticky='w', padx=4, pady=(0, 4))
        self.params_synced_var = tk.StringVar(value="No changes yet")
        self.params_synced_label = ttk.Label(frame, textvariable=self.params_synced_var,
                                              foreground='#888888',
                                              font=('Helvetica', 8, 'italic'), wraplength=180)
        self.params_synced_label.grid(row=3, column=1, sticky='w', padx=4, pady=(0, 4))

        frame.columnconfigure(1, weight=1)

    # ---- Settings panel -------------------------------------------------
    def _build_settings_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Acquisition Settings")
        frame.pack(fill='x', pady=(0, 8))

        ttk.Label(frame, text="Update period (s):").grid(row=0, column=0, sticky='w', padx=4, pady=(4, 0))
        self.update_period_var = tk.StringVar(value="1.0")
        ttk.Entry(frame, textvariable=self.update_period_var, width=10).grid(
            row=0, column=1, sticky='w', padx=4, pady=(4, 0))
        ttk.Label(frame, text="Must be a multiple of 0.1 s", foreground='gray',
                  font=('Helvetica', 8, 'italic')).grid(row=1, column=0, columnspan=2, sticky='w', padx=4)

        self.subtract_accidentals_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Subtract accidental counts from AB",
                         variable=self.subtract_accidentals_var).grid(
            row=2, column=0, columnspan=2, sticky='w', padx=4, pady=(6, 0))

        self.round_display_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(frame, text="Round counts in display",
                         variable=self.round_display_var).grid(
            row=3, column=0, columnspan=2, sticky='w', padx=4, pady=(2, 4))

    # ---- Snapshot panel ---------------------------------------------------
    def _build_snapshot_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Snapshot")
        frame.pack(fill='x', pady=(0, 8))

        ttk.Label(frame, text="Save file:").grid(row=0, column=0, sticky='w', padx=4, pady=2)
        self.snapshot_path_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.snapshot_path_var, width=16).grid(
            row=0, column=1, sticky='we', padx=2, pady=2)
        ttk.Button(frame, text="Browse...", command=self._browse_snapshot_path).grid(
            row=0, column=2, padx=2, pady=2)

        ttk.Label(frame, text="Snapshot time (s):").grid(row=1, column=0, sticky='w', padx=4, pady=2)
        self.snapshot_time_var = tk.StringVar(value="1.0")
        ttk.Entry(frame, textvariable=self.snapshot_time_var, width=10).grid(
            row=1, column=1, columnspan=2, sticky='w', padx=2, pady=2)

        ttk.Label(frame, text="Alpha (deg):").grid(row=2, column=0, sticky='w', padx=4, pady=2)
        self.alpha_var = tk.StringVar(value="0.0")
        ttk.Entry(frame, textvariable=self.alpha_var, width=10).grid(
            row=2, column=1, columnspan=2, sticky='w', padx=2, pady=2)

        ttk.Label(frame, text="Beta (deg):").grid(row=3, column=0, sticky='w', padx=4, pady=2)
        self.beta_var = tk.StringVar(value="0.0")
        ttk.Entry(frame, textvariable=self.beta_var, width=10).grid(
            row=3, column=1, columnspan=2, sticky='w', padx=2, pady=2)

        ttk.Label(frame, text="Comment:").grid(row=4, column=0, sticky='w', padx=4, pady=2)
        self.comment_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.comment_var, width=16).grid(
            row=4, column=1, columnspan=2, sticky='we', padx=2, pady=2)

        self.snapshot_btn = ttk.Button(frame, text="Take Snapshot", command=self._take_snapshot)
        self.snapshot_btn.grid(row=5, column=0, columnspan=3, sticky='we', padx=4, pady=(6, 4))

        frame.columnconfigure(1, weight=1)

    # ---- Counter readout panel -------------------------------------------
    def _build_counter_readout_panel(self, parent):
        frame = ttk.LabelFrame(parent, text="Counter Rates (counts/s)")
        frame.pack(fill='x', pady=(0, 8))

        self.counter0_var = tk.StringVar(value="--")
        self.counter1_var = tk.StringVar(value="--")
        self.counter5_var = tk.StringVar(value="--")

        for i, (label, var) in enumerate([
            ("Counter 0 (A):", self.counter0_var),
            ("Counter 1 (B):", self.counter1_var),
            ("Counter 5 (AB):", self.counter5_var),
        ]):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky='w', padx=4, pady=2)
            ttk.Label(frame, textvariable=var, font=('Consolas', 10, 'bold')).grid(
                row=i, column=1, sticky='e', padx=4, pady=2)
        frame.columnconfigure(1, weight=1)

    # ---- Main display -------------------------------------------------
    def _build_main_display(self, parent):
        therm_frame = ttk.Frame(parent)
        therm_frame.pack(fill='x', pady=(0, 10))

        col_a = ttk.Frame(therm_frame)
        col_a.grid(row=0, column=0, padx=15)
        col_b = ttk.Frame(therm_frame)
        col_b.grid(row=0, column=1, padx=15)
        col_ab = ttk.Frame(therm_frame)
        col_ab.grid(row=0, column=2, padx=15)
        therm_frame.columnconfigure((0, 1, 2), weight=1)

        # All three Thermometer canvases are created with identical
        # width/height (see Thermometer.__init__) and are the first widget
        # packed into their column frame, so their tops and bottoms line up
        # exactly across A / B / AB regardless of what's added underneath
        # each one (rate label, scale control, or -- for AB only -- the
        # coincidence panel). Only the scale (full-scale value / tick
        # spacing) changes per-thermometer; the canvas size itself never
        # does, so alignment can't drift.
        self.therm_a = Thermometer(col_a, "A")
        self.therm_a.pack()
        self.rate_a_var = tk.StringVar(value="0")
        ttk.Label(col_a, textvariable=self.rate_a_var, font=('Consolas', 16, 'bold')).pack(pady=(6, 0))
        ttk.Label(col_a, text="counts/s").pack()
        self._build_scale_control(col_a, self.therm_a, default_max=1_000_000)

        self.therm_b = Thermometer(col_b, "B")
        self.therm_b.pack()
        self.rate_b_var = tk.StringVar(value="0")
        ttk.Label(col_b, textvariable=self.rate_b_var, font=('Consolas', 16, 'bold')).pack(pady=(6, 0))
        ttk.Label(col_b, text="counts/s").pack()
        self._build_scale_control(col_b, self.therm_b, default_max=1_000_000)

        self.therm_ab = Thermometer(col_ab, "AB")
        self.therm_ab.pack()
        self.rate_ab_var = tk.StringVar(value="0")
        ttk.Label(col_ab, textvariable=self.rate_ab_var, font=('Consolas', 16, 'bold')).pack(pady=(6, 0))
        ttk.Label(col_ab, text="counts/s").pack()
        # AB (coincidence) rates are typically much lower than singles A/B,
        # so give it a smaller default full-scale.
        self._build_scale_control(col_ab, self.therm_ab, default_max=10_000)

        # sub-panel under the AB thermometer -- stays below the AB counter,
        # underneath its scale control
        coinc_frame = ttk.LabelFrame(col_ab, text="Coincidence / Accidentals")
        coinc_frame.pack(fill='x', pady=(10, 0))

        ttk.Label(coinc_frame, text="Resolution (ns):").grid(row=0, column=0, sticky='w', padx=4, pady=4)
        self.resolution_var = tk.StringVar(value="20")
        ttk.Entry(coinc_frame, textvariable=self.resolution_var, width=10).grid(
            row=0, column=1, sticky='w', padx=4, pady=4)

        ttk.Label(coinc_frame, text="Accidental rate (/s):").grid(row=1, column=0, sticky='w', padx=4, pady=4)
        self.accidental_rate_var = tk.StringVar(value="0")
        ttk.Label(coinc_frame, textvariable=self.accidental_rate_var, font=('Consolas', 10, 'bold')).grid(
            row=1, column=1, sticky='w', padx=4, pady=4)

        ttk.Label(coinc_frame, text="Accidental counts:").grid(row=2, column=0, sticky='w', padx=4, pady=4)
        self.accidentals_var = tk.StringVar(value="0")
        ttk.Label(coinc_frame, textvariable=self.accidentals_var, font=('Consolas', 10, 'bold')).grid(
            row=2, column=1, sticky='w', padx=4, pady=4)

        # start / terminate program button, bottom of main area, left-aligned
        bottom = ttk.Frame(parent)
        bottom.pack(fill='x', pady=(20, 0))
        self.program_btn = ttk.Button(bottom, text="Start Program", command=self._toggle_program)
        self.program_btn.pack(ipadx=20, ipady=8, side='left', anchor='w')
        self.program_btn.state(['disabled'])  # enabled once connected

    def _build_scale_control(self, parent, thermometer, default_max):
        """Small 'Full scale' entry that lets the user rescale a
        Thermometer live. Rescaling never resizes the canvas, so it can't
        knock the thermometers out of alignment with each other."""
        ctrl = ttk.Frame(parent)
        ctrl.pack(pady=(6, 0))
        ttk.Label(ctrl, text="Full scale:", font=('Helvetica', 8)).pack(side='left')
        scale_var = tk.StringVar(value=str(default_max))

        def apply_scale(event=None):
            try:
                new_max = float(scale_var.get())
                if new_max <= 0:
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "Invalid scale",
                    f"Full scale for thermometer '{thermometer.title_text}' "
                    "must be a positive number.")
                scale_var.set(str(int(thermometer.max_value)))
                return
            thermometer.set_scale(new_max)

        entry = ttk.Entry(ctrl, textvariable=scale_var, width=8)
        entry.pack(side='left', padx=(4, 4))
        entry.bind('<Return>', apply_scale)
        entry.bind('<FocusOut>', apply_scale)
        ttk.Button(ctrl, text="Set", width=4, command=apply_scale).pack(side='left')
        return scale_var

    # ------------------------------------------------------------------
    # Status helper (thread-safe)
    # ------------------------------------------------------------------
    def _set_status(self, text, is_error=False):
        def _update():
            self.status_var.set(text)
            if is_error:
                self.status_label.configure(foreground='#c01c28')
            else:
                self.status_label.configure(
                    foreground=self.STATUS_COLORS.get(text, '#000000'))
        self.after(0, _update)

    # ------------------------------------------------------------------
    # Connection handling
    # ------------------------------------------------------------------
    def _refresh_ports(self):
        try:
            # Re-query the VISA resource manager rather than reusing the
            # stale snapshot FPGAInterface took at construction time, so
            # newly-plugged-in devices actually show up.
            ports = list(self.fpga._rm.list_resources())
            self.fpga.connected_devices = ports
        except Exception as e:
            ports = []
            messagebox.showerror(
                "Device detection error",
                f"Could not query connected devices.\n\n{e}")
        self.port_combo['values'] = ports
        if ports:
            if self.port_var.get() not in ports:
                self.port_var.set(ports[0])
        else:
            self.port_var.set('')

    def _toggle_connect(self):
        if not self.connected:
            self._connect()
        else:
            self._disconnect()

    def _connect(self):
        port = self.port_var.get()
        if not port:
            messagebox.showwarning("No port selected", "Please select a COM port.")
            return

        self.connect_btn.state(['disabled'])
        self._set_status("Initializing")

        def worker():
            # FPGAInterface.open() never returns False -- on failure (e.g.
            # no device present / cable unplugged) it *raises* FPGAError.
            # That exception was previously unhandled here, which silently
            # killed the worker thread and left the Connect button disabled
            # forever with no feedback. Now we catch it and surface a popup.
            ok = False
            error = None
            try:
                ok = self.fpga.open(port)
            except Exception as e:
                error = e

            def finish():
                if ok and error is None:
                    self.connected = True
                    self.connect_btn.configure(text="Disconnect")
                    self.program_btn.state(['!disabled'])
                    self._set_status("Updated Counts")
                else:
                    self._set_status(f"Error: {error}", is_error=True)
                    messagebox.showerror(
                        "Connection failed",
                        f"Could not connect to a device on {port}.\n\n{error}")
                self.connect_btn.state(['!disabled'])
            self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def _disconnect(self):
        if self.running:
            self._stop_program()
        self.fpga.close()
        self.connected = False
        self.connect_btn.configure(text="Connect")
        self.program_btn.state(['disabled'])
        self._set_status("Program Terminated")

    # ------------------------------------------------------------------
    # Start / Terminate program (acquisition loop)
    # ------------------------------------------------------------------
    def _toggle_program(self):
        if not self.running:
            self._start_program()
        else:
            self._stop_program()

    def _start_program(self):
        if not self.connected:
            messagebox.showwarning("Not connected", "Connect to the FPGA first.")
            return
        try:
            self._get_update_period()
        except ValueError as e:
            messagebox.showerror("Invalid update period", str(e))
            return

        self.running = True
        self.program_btn.configure(text="Terminate Program")
        self._acq_thread = threading.Thread(target=self._acquisition_loop, daemon=True)
        self._acq_thread.start()

    def _stop_program(self):
        self.running = False
        if self._acq_thread is not None:
            self._acq_thread.join(timeout=2)
        self.program_btn.configure(text="Start Program")
        self._set_status("Program Terminated")

    def _get_update_period(self):
        try:
            period = float(self.update_period_var.get())
        except ValueError:
            raise ValueError("Update period must be a number.")
        n = round(period / self.fpga.SAMPLE_PERIOD_S)
        if period <= 0 or abs(n * self.fpga.SAMPLE_PERIOD_S - period) > 1e-9:
            raise ValueError("Update period must be a positive multiple of 0.1 s.")
        return period

    def _acquisition_loop(self):
        # error_occurred tracks whether we're exiting because something
        # went wrong (vs. a normal user-requested stop), so the tail below
        # knows whether it's safe to overwrite the status text with
        # "Program Terminated" or whether it should leave the error message
        # visible.
        error_occurred = False
        while self.running:
            try:
                period = self._get_update_period()
            except ValueError as e:
                self._set_status(f"Error: {e}", is_error=True)
                error_occurred = True
                break

            # don't fight with a snapshot acquisition
            while self.snapshot_in_progress and self.running:
                time.sleep(0.05)
            if not self.running:
                break

            self._set_status("Reading Counters")
            try:
                counts = self.fpga.acquire_counts(period)
            except Exception as e:
                self._set_status(f"Error: {e}", is_error=True)
                error_occurred = True
                break

            self.last_counts = counts
            self.last_period = period
            self._set_status("Updated Counts")
            self.after(0, self._refresh_display, counts, period)

        # IMPORTANT: this loop can end two ways -- (1) _stop_program() was
        # called from the main thread, which already flips the button text
        # itself, or (2) the loop broke out on its own here due to an error.
        # Previously, case (2) never touched self.program_btn at all, so the
        # button was left reading "Terminate Program" even though
        # self.running had gone False -- the next click would silently call
        # _start_program() again instead of doing what the stale label said.
        # Scheduling this on the main thread via .after() fixes that for
        # both cases (harmless if _stop_program already did it).
        self.running = False
        self.after(0, self._on_acquisition_loop_ended, error_occurred)

    def _on_acquisition_loop_ended(self, error_occurred):
        self.program_btn.configure(text="Start Program")
        if not error_occurred:
            self._set_status("Program Terminated")
        # if an error occurred, the error message set inside the loop stays
        # on screen instead of being overwritten by "Program Terminated"

    # ------------------------------------------------------------------
    # Display refresh
    # ------------------------------------------------------------------
    def _refresh_display(self, counts, period):
        rate_a = counts[self.COUNTER_A_IDX] / period
        rate_b = counts[self.COUNTER_B_IDX] / period
        rate_ab_raw = counts[self.COUNTER_AB_IDX] / period

        try:
            resolution_ns = float(self.resolution_var.get())
        except ValueError:
            resolution_ns = 0.0

        # accidental_rate: expected accidental coincidence RATE (counts/s),
        # from Poisson statistics: rate_a * rate_b * resolution_window.
        # accidentals: expected accidental coincidence COUNTS over this
        # particular `period` (accidental_rate * period).
        #
        # BUG FIX: this used to read
        #   accidental_rate = calculate_accidental_coinc_rate if period else 0.0
        # which assigned the *function object itself* (missing the call),
        # so as soon as "Subtract accidental counts from AB" was ticked,
        # `rate_ab_raw - accidental_rate` raised a TypeError (float - function)
        # and silently killed the acquisition loop.
        accidental_rate = calculate_accidental_coinc_rate(rate_a, rate_b, resolution_ns) if period else 0.0
        accidentals = accidental_rate * period if period else 0.0

        rate_ab_display = rate_ab_raw
        if self.subtract_accidentals_var.get():
            rate_ab_display = rate_ab_raw - accidental_rate

        self.therm_a.set_value(rate_a)
        self.therm_b.set_value(rate_b)
        self.therm_ab.set_value(rate_ab_display)

        self.rate_a_var.set(self._fmt(rate_a))
        self.rate_b_var.set(self._fmt(rate_b))
        self.rate_ab_var.set(self._fmt(rate_ab_display))

        self.counter0_var.set(self._fmt(rate_a))
        self.counter1_var.set(self._fmt(rate_b))
        self.counter5_var.set(self._fmt(rate_ab_raw))

        self.accidental_rate_var.set(self._fmt(accidental_rate))
        self.accidentals_var.set(self._fmt(accidentals))

    def _fmt(self, value):
        if self.round_display_var.get():
            return f"{round(value):,}"
        return f"{value:,.2f}"

    # ------------------------------------------------------------------
    # Snapshot handling
    # ------------------------------------------------------------------
    def _browse_snapshot_path(self):
        path = filedialog.asksaveasfilename(
            title="Select snapshot CSV file",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if path:
            self.snapshot_path_var.set(path)

    def _take_snapshot(self):
        if not self.connected:
            messagebox.showwarning("Not connected", "Connect to the FPGA first.")
            return
        if self.snapshot_in_progress:
            return

        path = self.snapshot_path_var.get().strip()
        if not path:
            messagebox.showwarning("No file selected", "Choose a snapshot save file first.")
            return

        try:
            snap_time = float(self.snapshot_time_var.get())
            n = round(snap_time / self.fpga.SAMPLE_PERIOD_S)
            if snap_time <= 0 or abs(n * self.fpga.SAMPLE_PERIOD_S - snap_time) > 1e-9:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid snapshot time",
                                  "Snapshot time must be a positive multiple of 0.1 s.")
            return

        try:
            alpha = float(self.alpha_var.get())
            beta = float(self.beta_var.get())
        except ValueError:
            messagebox.showerror("Invalid angle", "Alpha and Beta must be numbers.")
            return

        comment = self.comment_var.get()

        # pause live acquisition, if running
        self._resume_after_snapshot = self.running
        self.snapshot_in_progress = True
        self.snapshot_btn.state(['disabled'])
        self.program_btn.state(['disabled'])
        self._set_status("Reading Counters")

        def worker():
            try:
                counts = self.fpga.acquire_counts(snap_time)
                self.after(0, self._finish_snapshot, counts, snap_time, path, alpha, beta, comment)
            except Exception as e:
                self._set_status(f"Error: {e}", is_error=True)
                self.after(0, self._snapshot_cleanup)

        self._snapshot_thread = threading.Thread(target=worker, daemon=True)
        self._snapshot_thread.start()

    def _finish_snapshot(self, counts, snap_time, path, alpha, beta, comment):
        rate_a = counts[self.COUNTER_A_IDX] / snap_time
        rate_b = counts[self.COUNTER_B_IDX] / snap_time
        rate_ab = counts[self.COUNTER_AB_IDX] / snap_time

        # show the snapshot data on the main display
        self._refresh_display(counts, snap_time)

        write_header = not os.path.exists(path)
        try:
            with open(path, 'a', newline='') as f:
                writer = csv.writer(f)
                if write_header:
                    writer.writerow([
                        'timestamp', 'snapshot_time_s', 'alpha_deg', 'beta_deg', 'comment',
                        *[f'counts_{i}' for i in range(8)],
                        'rate_A', 'rate_B', 'rate_AB'])
                writer.writerow([
                    datetime.now().isoformat(timespec='seconds'),
                    snap_time, alpha, beta, comment,
                    *list(counts),
                    rate_a, rate_b, rate_ab])
            self._set_status("Updated Counts")
        except Exception as e:
            self._set_status(f"Error saving snapshot: {e}", is_error=True)
            messagebox.showerror("Save failed", str(e))

        self._snapshot_cleanup()

    def _snapshot_cleanup(self):
        self.snapshot_in_progress = False
        self.snapshot_btn.state(['!disabled'])
        if self.connected:
            self.program_btn.state(['!disabled'])
        # live loop resumes on its own since it was only waiting on the flag

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------
    def _on_close(self):
        if self.running:
            self._stop_program()
        if self.connected:
            self.fpga.close()
        self.destroy()


if __name__ == '__main__':
    app = CounterApp()
    app.mainloop()