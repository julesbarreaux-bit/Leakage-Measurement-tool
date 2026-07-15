"""GUI for the leakage current test. Run with: python leakage_gui.py"""
import csv
import os
import queue
import sys
import threading
import time
import tkinter as tk
from collections import deque
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from keithley2450 import LeakageMachine, LeakageMachineError

PLOT_WINDOW_S = 60.0

#when built as exe the temp folder gets wiped on exit so we use the exe folder instead
if getattr(sys, "frozen", False):
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CSV_PATH = os.path.join(_APP_DIR, "leakage_measurements.csv")
CSV_HEADER = ["timestamp", "pol_voltage_V", "pol_delay_s", "pol_current_A", "leak_voltage_V", "leak_delay_s", "leak_current_A"]


class LeakageGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Polarization / Leakage Measurement")
        self.resizable(True, True)
        self.instrument = None
        self.history = [] #csv data
        self._liveLeakReading = None #continuous measurement of leakage
        self._continuousThread = None #For the continuous measurement, to not freeze the tk window
        self._continuousStopEvent = None #To stop the continous thread(event here)
        self._plotBuffer = deque() #points for the live plot, only keep last PLOT_WINDOW_S seconds
        self._uiQueue = queue.Queue() #worker threads put stuff here, only the main loop reads it (tkinter is not thread safe)


        self._build_connection_frame()
        self._build_params_frame() #set default values
        self._build_csv_frame()
        self._build_action_frame()
        self._build_results_frame()
        self._build_plot_frame()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_resources()
        self.after(100, self._poll_ui_queue)

    #UI construction #######################

    def _build_connection_frame(self):
        frame = ttk.LabelFrame(self, text="Instrument connection")
        frame.grid(row=0, column=0, padx=10, pady=8, sticky="ew")

        ttk.Label(frame, text="VISA resource:").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.resourceVar = tk.StringVar()
        self.resourceCombo = ttk.Combobox(frame, textvariable=self.resourceVar, width=38)
        self.resourceCombo.grid(row=0, column=1, padx=5, pady=5)

        ttk.Button(frame, text="Scan", command=self._refresh_resources).grid(row=0, column=2, padx=5)
        self.connectButton = ttk.Button(frame, text="Connect", command=self._on_connect)
        self.connectButton.grid(row=0, column=3, padx=5)
        self.disconnectButton = ttk.Button(frame, text="Disconnect", command=self._on_disconnect, state="disabled")
        self.disconnectButton.grid(row=0, column=4, padx=5)

        self.connStatusVar = tk.StringVar(value="Not connected")
        ttk.Label(frame, textvariable=self.connStatusVar, foreground="grey").grid(row=1, column=0, columnspan=5, padx=5, pady=(0, 5), sticky="w")

    def _build_params_frame(self):
        frame = ttk.LabelFrame(self, text="Sequence parameters")
        frame.grid(row=1, column=0, padx=10, pady=8, sticky="ew")

        self.polVoltageVar = tk.DoubleVar(value=20.0)
        self.polDelayVar = tk.DoubleVar(value=0.1)
        self.leakVoltageVar = tk.DoubleVar(value=10.0)
        self.leakDelayVar = tk.DoubleVar(value=0.4)
        self.limitEnabledVar = tk.BooleanVar(value=False)
        self.currentLimitMaVar = tk.DoubleVar(value=10.0)
        self.nplcVar = tk.DoubleVar(value=0.1)
        self.rangeEnabledVar = tk.BooleanVar(value=False)
        self.currentRangeAVar = tk.DoubleVar(value=1e-6)

        def row(r, label, var, unit):
            ttk.Label(frame, text=label).grid(row=r, column=0, padx=5, pady=4, sticky="w")
            ttk.Entry(frame, textvariable=var, width=10).grid(row=r, column=1, padx=5, pady=4)
            ttk.Label(frame, text=unit).grid(row=r, column=2, padx=5, pady=4, sticky="w")

        row(0, "Polarization voltage", self.polVoltageVar, "V")
        row(1, "Polarization delay", self.polDelayVar, "s")
        row(2, "Leakage voltage", self.leakVoltageVar, "V")
        row(3, "Leakage measurement delay", self.leakDelayVar, "s")

        ttk.Checkbutton(frame, text="Limit", variable=self.limitEnabledVar, command=self._toggle_current_limit).grid(row=4, column=0, columnspan=2, padx=5, pady=4, sticky="w")
        self.currentLimitEntry = ttk.Entry(frame, textvariable=self.currentLimitMaVar, width=10, state="disabled")
        self.currentLimitEntry.grid(row=4, column=1, padx=5, pady=4, sticky="e")
        ttk.Label(frame, text="mA (limit)").grid(row=4, column=2, padx=5, pady=4, sticky="w")

        #nplc and range only apply after you hit connect again
        row(5, "NPLC", self.nplcVar, "cycles (setup)")

        ttk.Checkbutton(frame, text="Fixed current range", variable=self.rangeEnabledVar, command=self._toggle_current_range).grid(row=6, column=0, columnspan=2, padx=5, pady=4, sticky="w")
        self.currentRangeEntry = ttk.Entry(frame, textvariable=self.currentRangeAVar, width=10, state="disabled")
        self.currentRangeEntry.grid(row=6, column=1, padx=5, pady=4, sticky="e")
        ttk.Label(frame, text="A (range, setup)").grid(row=6, column=2, padx=5, pady=4, sticky="w")

    def _toggle_current_limit(self):
        self.currentLimitEntry.configure(state="normal" if self.limitEnabledVar.get() else "disabled")

    def _toggle_current_range(self):
        self.currentRangeEntry.configure(state="normal" if self.rangeEnabledVar.get() else "disabled")

    def _build_csv_frame(self):
        frame = ttk.LabelFrame(self, text="CSV logging")
        frame.grid(row=2, column=0, padx=10, pady=8, sticky="ew")

        self.csvPathVar = tk.StringVar(value=DEFAULT_CSV_PATH)
        ttk.Entry(frame, textvariable=self.csvPathVar, width=45).grid(row=0, column=0, padx=5, pady=5)
        ttk.Button(frame, text="Browse...", command=self._browse_csv).grid(row=0, column=1, padx=5, pady=5)

    def _build_action_frame(self):
        frame = ttk.Frame(self)
        frame.grid(row=3, column=0, padx=10, pady=8, sticky="ew")

        self.startButton = ttk.Button(frame, text="Start", command=self._on_start, state="disabled")
        self.startButton.grid(row=0, column=0, padx=5)

        self.continuousButton = ttk.Button(frame, text="Continuous", command=self._on_toggle_continuous, state="disabled")
        self.continuousButton.grid(row=0, column=1, padx=5)

        self.storeButton = ttk.Button(frame, text="Store reading", command=self._on_store, state="disabled")
        self.storeButton.grid(row=0, column=2, padx=5)

        self.statusVar = tk.StringVar(value="Ready")
        ttk.Label(frame, textvariable=self.statusVar).grid(row=0, column=3, padx=10, sticky="w")

    def _build_results_frame(self):
        frame = ttk.LabelFrame(self, text="Results")
        frame.grid(row=4, column=0, padx=10, pady=8, sticky="ew")

        self.showNaVar = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Show in nA", variable=self.showNaVar, command=self._refresh_display).grid(row=0, column=0, padx=5, pady=4, sticky="w")

        self.polResultVar = tk.StringVar(value="Polarization current: --")
        self.leakResultVar = tk.StringVar(value="Leakage current: --")
        ttk.Label(frame, textvariable=self.polResultVar, font=("Segoe UI", 10, "bold")).grid(row=1, column=0, padx=5, pady=4, sticky="w")
        ttk.Label(frame, textvariable=self.leakResultVar, font=("Segoe UI", 10, "bold")).grid(row=2, column=0, padx=5, pady=4, sticky="w")

        columns = ("timestamp", "pol_v", "pol_i", "leak_v", "leak_i")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=6)
        for col in columns:
            self.tree.column(col, width=110, anchor="center")
        self.tree.grid(row=3, column=0, padx=5, pady=8)
        self._update_tree_headings()

    def _build_plot_frame(self):
        frame = ttk.LabelFrame(self, text="Live plot (continuous mode, last 60 s)")
        frame.grid(row=5, column=0, padx=10, pady=8, sticky="ew")

        self._fig = Figure(figsize=(5.4, 2.8), dpi=80)
        self._axVoltage = self._fig.add_subplot(111)
        self._axCurrent = self._axVoltage.twinx()

        self._axVoltage.set_xlabel("seconds ago")
        self._axVoltage.set_ylabel("Voltage (V)", color="blue")
        self._axVoltage.tick_params(axis="y", labelcolor="blue")
        self._axCurrent.set_ylabel("Current (A)", color="red")
        self._axCurrent.tick_params(axis="y", labelcolor="red")

        (self._voltageLine,) = self._axVoltage.plot([], [], color="blue")
        (self._currentLine,) = self._axCurrent.plot([], [], color="red")
        self._fig.tight_layout()

        self._plotCanvas = FigureCanvasTkAgg(self._fig, master=frame)
        self._plotCanvas.get_tk_widget().grid(row=0, column=0, padx=5, pady=5)

    def _record_plot_point(self, voltage, current):
        self._plotBuffer.append((time.time(), voltage, current))
        self._refresh_plot()

    def _refresh_plot(self):
        now = time.time()
        while self._plotBuffer and self._plotBuffer[0][0] < now - PLOT_WINDOW_S:
            self._plotBuffer.popleft()

        xs = [t - now for t, _, _ in self._plotBuffer]
        voltages = [v for _, v, _ in self._plotBuffer]
        currents = [i for _, _, i in self._plotBuffer]
        self._voltageLine.set_data(xs, voltages)
        self._currentLine.set_data(xs, currents)

        self._axVoltage.set_xlim(-PLOT_WINDOW_S, 0)
        self._axVoltage.relim()
        self._axVoltage.autoscale_view(scalex=False, scaley=True)
        self._axCurrent.relim()
        self._axCurrent.autoscale_view(scalex=False, scaley=True)
        self._plotCanvas.draw_idle()

    def _update_tree_headings(self):
        currentUnit = "nA" if self.showNaVar.get() else "A"
        headings = {"timestamp": "Timestamp", "pol_v": "Pol. (V)", "pol_i": f"I pol. ({currentUnit})", "leak_v": "Leak (V)", "leak_i": f"I leak ({currentUnit})"}
        for col, text in headings.items():
            self.tree.heading(col, text=text)

    def _format_current(self, amps):
        if self.showNaVar.get():
            return f"{amps * 1e9:.3f} nA"
        return f"{amps:.6e} A"

    def _format_voltage(self, volts):
        return f"{volts:g}" if volts is not None else "--"

    def _format_current_or_dash(self, amps):
        return self._format_current(amps) if amps is not None else "--"

    def _refresh_display(self):
        self._update_tree_headings()
        if self.history:
            latest = self.history[-1]
            self.polResultVar.set(f"Polarization current: {self._format_current_or_dash(latest[3])}")
            self.leakResultVar.set(f"Leakage current: {self._format_current_or_dash(latest[6])}")

        self.tree.delete(*self.tree.get_children())
        for timestamp, polVoltage, polDelay, polCurrent, leakVoltage, leakDelay, leakCurrent in self.history:
            values = (timestamp, self._format_voltage(polVoltage), self._format_current_or_dash(polCurrent), self._format_voltage(leakVoltage), self._format_current_or_dash(leakCurrent))
            self.tree.insert("", 0, values=values)

    #cross-thread UI updates #######################

    def _poll_ui_queue(self):
        try:
            while True:
                message = self._uiQueue.get_nowait()
                self._handle_ui_message(message)
        except queue.Empty:
            pass
        self._refresh_plot() #keep refreshing even with no new point so the plot keeps sliding
        self.after(100, self._poll_ui_queue)

    def _handle_ui_message(self, message):
        kind = message[0]
        if kind == "status":
            self.statusVar.set(message[1])
        elif kind == "sequence_done":
            self._on_sequence_done(message[1])
        elif kind == "sequence_error":
            self._on_sequence_error(message[1])
        elif kind == "continuous_reading":
            self._update_continuous_reading(message[1], message[2])
        elif kind == "continuous_error":
            self._on_continuous_error(message[1])

    #connection handling #######################


    #refresh ressources
    def _refresh_resources(self):
        try:
            resources = LeakageMachine.find_devices()
        except Exception as exc:
            resources = []
            self.connStatusVar.set(f"Scan failed: {exc}")
        self.resourceCombo["values"] = resources
        if resources and not self.resourceVar.get():
            self.resourceVar.set(resources[0])


    #It will set up value in the machine class, create an instance of instrument in slef.instrument with its settings
    def _on_connect(self):
        resource = self.resourceVar.get().strip()
        if not resource:
            messagebox.showwarning("No resource", "Pick or type a VISA resource")
            return
        try:
            nplc = self.nplcVar.get()
            currentRange = self.currentRangeAVar.get() if self.rangeEnabledVar.get() else None
        except tk.TclError:
            messagebox.showerror("Bad value", "Check nplc / range")
            return
        try:
            self.instrument = LeakageMachine(resource)
            deviceId = self.instrument.get_id()
            self.instrument.setup(nplc=nplc, currentRange=currentRange)
        except Exception as exc:
            self.instrument = None
            messagebox.showerror("Connection failed", str(exc))
            self.connStatusVar.set("Not connected")
            return

        self.connStatusVar.set(f"Connected: {deviceId}")
        self.connectButton.configure(state="disabled")
        self.disconnectButton.configure(state="normal")
        self.startButton.configure(state="normal")
        self.continuousButton.configure(state="normal")

    def _on_disconnect(self):
        self._stop_continuous()
        if self.instrument:
            self.instrument.close()
            self.instrument = None
        self.connStatusVar.set("Not connected")
        self.connectButton.configure(state="normal")
        self.disconnectButton.configure(state="disabled")
        self.startButton.configure(state="disabled")
        self.continuousButton.configure(state="disabled")
        self.storeButton.configure(state="disabled")

    def _on_close(self):
        self._on_disconnect()
        self.destroy()

    #CSV #######################

    def _browse_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=os.path.basename(self.csvPathVar.get()), filetypes=[("CSV", "*.csv")])
        if path:
            self.csvPathVar.set(path)

    def _append_csv(self, row):
        path = self.csvPathVar.get()
        writeHeader = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            writer = csv.writer(f)
            if writeHeader:
                writer.writerow(CSV_HEADER)
            writer.writerow(["" if v is None else v for v in row])

    #measurement #######################

    def _on_start(self):
        if not self.instrument:
            return
        try:
            polVoltage = self.polVoltageVar.get()
            polDelay = self.polDelayVar.get()
            leakVoltage = self.leakVoltageVar.get()
            leakDelay = self.leakDelayVar.get()
            currentLimit = self.currentLimitMaVar.get() / 1000.0 if self.limitEnabledVar.get() else None
        except tk.TclError:
            messagebox.showerror("Bad value", "Check the numbers")
            return

        self.startButton.configure(state="disabled")
        self.continuousButton.configure(state="disabled")
        self.statusVar.set("Running...")

        thread = threading.Thread(target=self._run_sequence, args=(polVoltage, polDelay, leakVoltage, leakDelay, currentLimit), daemon=True)
        thread.start()

    def _run_sequence(self, polVoltage, polDelay, leakVoltage, leakDelay, currentLimit):
        def progress(msg):
            self._uiQueue.put(("status", msg))

        try:
            polCurrent, leakCurrent = self.instrument.run_test(polVoltage, polDelay, leakVoltage, leakDelay, currentLimit=currentLimit, progress=progress)
        except (LeakageMachineError, Exception) as exc:
            self._uiQueue.put(("sequence_error", exc))
            return

        timestamp = datetime.now().isoformat(timespec="seconds")
        row = [timestamp, polVoltage, polDelay, polCurrent, leakVoltage, leakDelay, leakCurrent]
        self._uiQueue.put(("sequence_done", row))

    def _on_sequence_done(self, row):
        self.history.append(row)
        self._refresh_display()

        try:
            self._append_csv(row)
        except OSError as exc:
            messagebox.showerror("CSV write failed", str(exc))

        self.statusVar.set("Done.")
        self.startButton.configure(state="normal")
        self.continuousButton.configure(state="normal")

    def _on_sequence_error(self, exc):
        self.statusVar.set("Error.")
        messagebox.showerror("Measurement error", str(exc))
        self.startButton.configure(state="normal")
        self.continuousButton.configure(state="normal")

    #continuous mode #######################

    def _on_toggle_continuous(self):
        if self._continuousThread is None:
            self._start_continuous()
        else:
            self._stop_continuous()

    def _start_continuous(self):
        try:
            leakVoltage = self.leakVoltageVar.get()
            leakDelay = self.leakDelayVar.get()
            currentLimit = self.currentLimitMaVar.get() / 1000.0 if self.limitEnabledVar.get() else None
        except tk.TclError:
            messagebox.showerror("Bad value", "Check the numbers")
            return

        self._liveLeakReading = None
        self.startButton.configure(state="disabled")
        self.continuousButton.configure(text="Stop")
        self.storeButton.configure(state="normal")
        self.statusVar.set("Continuous running...")

        self._continuousStopEvent = threading.Event()
        self._continuousThread = threading.Thread(target=self._continuous_loop, args=(leakVoltage, leakDelay, currentLimit, self._continuousStopEvent), daemon=True)
        self._continuousThread.start()

    def _continuous_loop(self, leakVoltage, leakDelay, currentLimit, stopEvent):
        try:
            self.instrument.set_voltage(leakVoltage, currentLimit=currentLimit)
            self.instrument.turn_on()
            while not stopEvent.is_set():
                try:
                    current = self.instrument.get_current()
                except Exception as exc:
                    self._uiQueue.put(("continuous_error", exc))
                    return
                self._uiQueue.put(("continuous_reading", leakVoltage, current))
                stopEvent.wait(leakDelay)
        finally:
            try:
                self.instrument.turn_off()
            except Exception:
                pass

    def _update_continuous_reading(self, leakVoltage, current):
        self._liveLeakReading = (leakVoltage, current)
        self.leakResultVar.set(f"Leakage current: {self._format_current(current)}")
        self._record_plot_point(leakVoltage, current)

    def _on_continuous_error(self, exc):
        self.statusVar.set("Error.")
        messagebox.showerror("Continuous error", str(exc))
        self._reset_continuous_ui()

    def _stop_continuous(self):
        if self._continuousStopEvent is not None:
            self._continuousStopEvent.set()
        self._continuousThread = None
        self._continuousStopEvent = None
        self._reset_continuous_ui()

    def _reset_continuous_ui(self):
        self.continuousButton.configure(text="Continuous")
        self.storeButton.configure(state="disabled")
        self.startButton.configure(state="normal" if self.instrument else "disabled")
        if self.statusVar.get() == "Continuous running...":
            self.statusVar.set("Ready")

    def _on_store(self):
        if self._liveLeakReading is None:
            return
        leakVoltage, leakCurrent = self._liveLeakReading
        timestamp = datetime.now().isoformat(timespec="seconds")
        row = [timestamp, None, None, None, leakVoltage, self.leakDelayVar.get(), leakCurrent]
        self.history.append(row)
        self._refresh_display()

        try:
            self._append_csv(row)
        except OSError as exc:
            messagebox.showerror("CSV write failed", str(exc))

        self.statusVar.set("Reading stored.")


if __name__ == "__main__":
    app = LeakageGUI()
    app.mainloop()
