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
DEFAULT_RESULTS_CSV_PATH = os.path.join(_APP_DIR, "leakage_results.csv")
CSV_HEADER = ["timestamp", "run_id", "wafer_id", "die_id", "nplc", "pol_voltage_V", "pol_delay_s", "pol_current_A", "pol_current_nA", "leak_voltage_V", "leak_delay_s", "leak_current_A", "leak_current_nA"]


class LeakageGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Polarization / Leakage Measurement")
        self.resizable(True, True)
        self.geometry("1150x680")
        self.instrument = None
        self.history = [] #csv data
        self._liveLeakReading = None #continuous measurement of leakage
        self._continuousThread = None #For the continuous measurement, to not freeze the tk window
        self._continuousStopEvent = None #To stop the continous thread(event here)
        self._plotBuffer = deque() #points for the live plot, only keep last PLOT_WINDOW_S seconds
        self._uiQueue = queue.Queue() #worker threads put stuff here, only the main loop reads it (tkinter is not thread safe)
        self._loopRunCounter = 0 #same id for every row written by one loop test run

        self._build_scrollable_container()

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

    def _build_scrollable_container(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        self.mainFrame = ttk.Frame(canvas)
        mainFrameWindow = canvas.create_window((0, 0), window=self.mainFrame, anchor="nw")
        self.mainFrame.grid_columnconfigure(1, weight=1)
        self.mainFrame.grid_rowconfigure(3, weight=1)

        self.mainFrame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(mainFrameWindow, width=e.width, height=max(e.height, self.mainFrame.winfo_reqheight())))
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

    def _build_connection_frame(self):
        frame = ttk.LabelFrame(self.mainFrame, text="Instrument connection")
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
        frame = ttk.LabelFrame(self.mainFrame, text="Sequence parameters")
        frame.grid(row=1, column=0, padx=10, pady=8, sticky="ew")

        self.polVoltageVar = tk.DoubleVar(value=20.0)
        self.polDelayVar = tk.DoubleVar(value=0.1)
        self.leakVoltageVar = tk.DoubleVar(value=10.0)
        self.leakDelayVar = tk.DoubleVar(value=0.4)
        self.limitEnabledVar = tk.BooleanVar(value=True)
        self.currentLimitMaVar = tk.DoubleVar(value=0.001)
        self.nplcVar = tk.DoubleVar(value=1.0)
        self.rangeEnabledVar = tk.BooleanVar(value=True)
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
        self.currentLimitEntry = ttk.Entry(frame, textvariable=self.currentLimitMaVar, width=10, state="normal" if self.limitEnabledVar.get() else "disabled")
        self.currentLimitEntry.grid(row=4, column=1, padx=5, pady=4, sticky="e")
        ttk.Label(frame, text="mA (limit)").grid(row=4, column=2, padx=5, pady=4, sticky="w")

        #nplc and range only apply after you hit connect again
        row(5, "NPLC", self.nplcVar, "cycles (setup)")

        ttk.Checkbutton(frame, text="Fixed current range", variable=self.rangeEnabledVar, command=self._toggle_current_range).grid(row=6, column=0, columnspan=2, padx=5, pady=4, sticky="w")
        self.currentRangeEntry = ttk.Entry(frame, textvariable=self.currentRangeAVar, width=10, state="normal" if self.rangeEnabledVar.get() else "disabled")
        self.currentRangeEntry.grid(row=6, column=1, padx=5, pady=4, sticky="e")
        ttk.Label(frame, text="A (range, setup)").grid(row=6, column=2, padx=5, pady=4, sticky="w")

    def _toggle_current_limit(self):
        self.currentLimitEntry.configure(state="normal" if self.limitEnabledVar.get() else "disabled")

    def _toggle_current_range(self):
        self.currentRangeEntry.configure(state="normal" if self.rangeEnabledVar.get() else "disabled")

    def _toggle_pre_pause(self):
        self.prePauseEntry.configure(state="normal" if self.prePauseEnabledVar.get() else "disabled")

    def _get_pre_pause(self):
        return self.prePauseSecVar.get() if self.prePauseEnabledVar.get() else 0.0

    def _build_csv_frame(self):
        frame = ttk.LabelFrame(self.mainFrame, text="CSV logging")
        frame.grid(row=2, column=0, padx=10, pady=8, sticky="ew")

        ttk.Label(frame, text="Loop samples").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.csvPathVar = tk.StringVar(value=DEFAULT_CSV_PATH)
        ttk.Entry(frame, textvariable=self.csvPathVar, width=45).grid(row=0, column=1, padx=5, pady=5)
        ttk.Button(frame, text="Browse...", command=self._browse_csv).grid(row=0, column=2, padx=5, pady=5)
        ttk.Button(frame, text="Clear", command=self._on_clear_csv).grid(row=0, column=3, padx=5, pady=5)

        ttk.Label(frame, text="Results").grid(row=1, column=0, padx=5, pady=5, sticky="w")
        self.resultsCsvPathVar = tk.StringVar(value=DEFAULT_RESULTS_CSV_PATH)
        ttk.Entry(frame, textvariable=self.resultsCsvPathVar, width=45).grid(row=1, column=1, padx=5, pady=5)
        ttk.Button(frame, text="Browse...", command=self._browse_results_csv).grid(row=1, column=2, padx=5, pady=5)
        ttk.Button(frame, text="Clear", command=self._on_clear_results_csv).grid(row=1, column=3, padx=5, pady=5)

    def _build_action_frame(self):
        container = ttk.Frame(self.mainFrame)
        container.grid(row=3, column=0, padx=10, pady=8, sticky="ew")

        startFrame = ttk.LabelFrame(container, text="Single measurement")
        startFrame.grid(row=0, column=0, sticky="ew")

        self.startButton = ttk.Button(startFrame, text="Start", command=self._on_start, state="disabled")
        self.startButton.grid(row=0, column=0, padx=5, pady=5)

        self.continuousButton = ttk.Button(startFrame, text="Continuous", command=self._on_toggle_continuous, state="disabled")
        self.continuousButton.grid(row=0, column=1, padx=5, pady=5)

        self.storeButton = ttk.Button(startFrame, text="Store reading", command=self._on_store, state="disabled")
        self.storeButton.grid(row=0, column=2, padx=5, pady=5)

        self.statusVar = tk.StringVar(value="Ready")
        ttk.Label(startFrame, textvariable=self.statusVar).grid(row=0, column=3, padx=10, pady=5, sticky="w")

        self.startWaferIdVar = tk.StringVar(value="")
        ttk.Label(startFrame, text="Wafer ID").grid(row=1, column=0, padx=5, pady=(0, 5), sticky="w")
        ttk.Entry(startFrame, textvariable=self.startWaferIdVar, width=10).grid(row=1, column=1, padx=5, pady=(0, 5), sticky="w")

        self.startDieIdVar = tk.StringVar(value="")
        ttk.Label(startFrame, text="Die ID").grid(row=1, column=2, padx=5, pady=(0, 5), sticky="w")
        ttk.Entry(startFrame, textvariable=self.startDieIdVar, width=10).grid(row=1, column=3, padx=5, pady=(0, 5), sticky="w")

        loopFrame = ttk.LabelFrame(container, text="Loop test")
        loopFrame.grid(row=1, column=0, sticky="ew", pady=(14, 0))

        self.loopDurationVar = tk.DoubleVar(value=5.0)
        ttk.Label(loopFrame, text="Duration").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        ttk.Entry(loopFrame, textvariable=self.loopDurationVar, width=10).grid(row=0, column=1, padx=5, pady=5, sticky="w")
        ttk.Label(loopFrame, text="s").grid(row=0, column=2, padx=5, pady=5, sticky="w")

        self.loopRepeatVar = tk.IntVar(value=10)
        ttk.Label(loopFrame, text="Repeats").grid(row=0, column=4, padx=5, pady=5, sticky="w")
        ttk.Entry(loopFrame, textvariable=self.loopRepeatVar, width=6).grid(row=0, column=5, padx=5, pady=5, sticky="w")

        self.loopTestButton = ttk.Button(loopFrame, text="Loop test", command=self._on_loop_test, state="disabled")
        self.loopTestButton.grid(row=0, column=6, padx=5, pady=5, sticky="w")

        self.loopWaferIdVar = tk.StringVar(value="")
        ttk.Label(loopFrame, text="Wafer ID").grid(row=1, column=0, padx=5, pady=(0, 5), sticky="w")
        ttk.Entry(loopFrame, textvariable=self.loopWaferIdVar, width=10).grid(row=1, column=1, padx=5, pady=(0, 5), sticky="w")

        self.loopDieIdVar = tk.StringVar(value="")
        ttk.Label(loopFrame, text="Die ID").grid(row=1, column=2, padx=5, pady=(0, 5), sticky="w")
        ttk.Entry(loopFrame, textvariable=self.loopDieIdVar, width=10).grid(row=1, column=3, padx=5, pady=(0, 5), sticky="w")

        self.prePauseEnabledVar = tk.BooleanVar(value=True)
        self.prePauseSecVar = tk.DoubleVar(value=3.0)
        ttk.Checkbutton(loopFrame, text="Pause before polarization", variable=self.prePauseEnabledVar, command=self._toggle_pre_pause).grid(row=2, column=0, columnspan=3, padx=5, pady=(0, 5), sticky="w")
        self.prePauseEntry = ttk.Entry(loopFrame, textvariable=self.prePauseSecVar, width=10, state="normal" if self.prePauseEnabledVar.get() else "disabled")
        self.prePauseEntry.grid(row=3, column=1, padx=5, pady=(0, 5), sticky="w")
        ttk.Label(loopFrame, text="s (before each polarization)").grid(row=3, column=2, columnspan=3, padx=5, pady=(0, 5), sticky="w")

    def _build_results_frame(self):
        self.rightFrame = ttk.Frame(self.mainFrame)
        self.rightFrame.grid(row=0, column=1, rowspan=4, padx=10, pady=8, sticky="nsew")
        self.rightFrame.grid_columnconfigure(0, weight=1)
        self.rightFrame.grid_rowconfigure(1, weight=1)

        frame = ttk.LabelFrame(self.rightFrame, text="Results")
        frame.grid(row=0, column=0, sticky="ew")
        frame.grid_columnconfigure(0, weight=1)

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
        self.tree.grid(row=3, column=0, padx=5, pady=8, sticky="ew")
        self._update_tree_headings()

    def _build_plot_frame(self):
        frame = ttk.LabelFrame(self.rightFrame, text="Live plot (continuous mode, last 60 s)")
        frame.grid(row=1, column=0, pady=(8, 0), sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)

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
        self._plotCanvas.get_tk_widget().grid(row=0, column=0, padx=5, pady=5, sticky="nsew")

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
        elif kind == "leak_sample":
            self._on_leak_sample(*message[1:])
        elif kind == "leak_result":
            self._on_leak_result(*message[1:])
        elif kind == "loop_test_done":
            self._on_loop_test_done()
        elif kind == "loop_test_error":
            self._on_loop_test_error(message[1])

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
        self.loopTestButton.configure(state="normal")

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
        self.loopTestButton.configure(state="disabled")

    def _on_close(self):
        self._on_disconnect()
        self.destroy()

    #CSV #######################

    def _browse_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=os.path.basename(self.csvPathVar.get()), filetypes=[("CSV", "*.csv")])
        if path:
            self.csvPathVar.set(path)

    def _browse_results_csv(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=os.path.basename(self.resultsCsvPathVar.get()), filetypes=[("CSV", "*.csv")])
        if path:
            self.resultsCsvPathVar.set(path)

    def _on_clear_csv(self):
        self._clear_csv(self.csvPathVar.get(), "Loop samples")

    def _on_clear_results_csv(self):
        self._clear_csv(self.resultsCsvPathVar.get(), "Results")

    def _clear_csv(self, path, label):
        if not path:
            return
        if not messagebox.askyesno("Clear CSV", f"Delete all rows in the {label} CSV?\n{path}"):
            return
        try:
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(CSV_HEADER)
        except OSError as exc:
            messagebox.showerror("Clear failed", str(exc))

    def _append_csv(self, path, row):
        writeHeader = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            writer = csv.writer(f)
            if writeHeader:
                writer.writerow(CSV_HEADER)
            writer.writerow(["" if v is None else v for v in row])

    def _to_na(self, amps):
        return amps * 1e9 if amps is not None else None

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
        self.loopTestButton.configure(state="disabled")
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

        timestamp, polVoltage, polDelay, polCurrent, leakVoltage, leakDelay, leakCurrent = row
        csvRow = [timestamp, None, self.startWaferIdVar.get(), self.startDieIdVar.get(), self.nplcVar.get(), polVoltage, polDelay, polCurrent, self._to_na(polCurrent), leakVoltage, leakDelay, leakCurrent, self._to_na(leakCurrent)]
        try:
            self._append_csv(self.resultsCsvPathVar.get(), csvRow)
        except OSError as exc:
            messagebox.showerror("CSV write failed", str(exc))

        self.statusVar.set("Done.")
        self.startButton.configure(state="normal")
        self.continuousButton.configure(state="normal")
        self.loopTestButton.configure(state="normal")

    def _on_leak_sample(self, timestamp, runId, waferId, dieId, nplc, leakVoltage, leakDelay, current):
        row = [datetime.fromtimestamp(timestamp).isoformat(timespec="milliseconds"), runId, waferId, dieId, nplc, None, None, None, None, leakVoltage, leakDelay, current, self._to_na(current)]
        try:
            self._append_csv(self.csvPathVar.get(), row)
        except OSError as exc:
            messagebox.showerror("CSV write failed", str(exc))

    def _on_leak_result(self, timestamp, runId, waferId, dieId, nplc, polVoltage, polDelay, polCurrent, leakVoltage, leakDelay, current):
        row = [datetime.fromtimestamp(timestamp).isoformat(timespec="milliseconds"), runId, waferId, dieId, nplc, polVoltage, polDelay, polCurrent, self._to_na(polCurrent), leakVoltage, leakDelay, current, self._to_na(current)]
        try:
            self._append_csv(self.resultsCsvPathVar.get(), row)
        except OSError as exc:
            messagebox.showerror("CSV write failed", str(exc))

    def _on_sequence_error(self, exc):
        self.statusVar.set("Error.")
        messagebox.showerror("Measurement error", str(exc))
        self.startButton.configure(state="normal")
        self.continuousButton.configure(state="normal")
        self.loopTestButton.configure(state="normal")

    #loop test #######################

    def _on_loop_test(self):
        if not self.instrument:
            return
        try:
            polVoltage = self.polVoltageVar.get()
            polDelay = self.polDelayVar.get()
            leakVoltage = self.leakVoltageVar.get()
            leakDelay = self.leakDelayVar.get()
            currentLimit = self.currentLimitMaVar.get() / 1000.0 if self.limitEnabledVar.get() else None
            duration = self.loopDurationVar.get()
            repeats = self.loopRepeatVar.get()
            nplc = self.nplcVar.get()
            prePause = self._get_pre_pause()
        except tk.TclError:
            messagebox.showerror("Bad value", "Check the numbers")
            return
        if repeats < 1:
            messagebox.showerror("Bad value", "Repeats must be at least 1")
            return

        waferId = self.loopWaferIdVar.get()
        dieId = self.loopDieIdVar.get()

        self.startButton.configure(state="disabled")
        self.continuousButton.configure(state="disabled")
        self.loopTestButton.configure(state="disabled")
        self.statusVar.set("Loop test running...")

        thread = threading.Thread(target=self._run_loop_test_repeats, args=(polVoltage, polDelay, leakVoltage, leakDelay, currentLimit, duration, repeats, waferId, dieId, nplc, prePause), daemon=True)
        thread.start()

    def _run_loop_test_repeats(self, polVoltage, polDelay, leakVoltage, leakDelay, currentLimit, duration, repeats, waferId, dieId, nplc, prePause):
        def progress(msg):
            self._uiQueue.put(("status", msg))

        for i in range(repeats):
            progress(f"Loop test running... ({i + 1}/{repeats})")
            self._loopRunCounter += 1
            runId = self._loopRunCounter
            try:
                self._run_loop_test(polVoltage, polDelay, leakVoltage, leakDelay, currentLimit, duration, runId, waferId, dieId, nplc, prePause, progress)
            except Exception as exc:
                self._uiQueue.put(("loop_test_error", exc))
                return

        self._uiQueue.put(("loop_test_done", None))

    def _run_loop_test(self, polVoltage, polDelay, leakVoltage, leakDelay, currentLimit, duration, runId, waferId, dieId, nplc, prePause, progress):
        loopStart = time.time()
        polCurrent, samples, bestSample, endSample = self.instrument.run_loop_test(
            polVoltage, polDelay, leakVoltage, leakDelay, duration,
            currentLimit=currentLimit, prePause=prePause, progress=progress,
        )

        for elapsed, current in samples:
            self._uiQueue.put(("leak_sample", loopStart + elapsed, runId, waferId, dieId, nplc, leakVoltage, elapsed, current))

        if bestSample is not None:
            elapsed, current = bestSample
            self._uiQueue.put(("leak_result", loopStart + elapsed, runId, waferId, dieId, nplc, polVoltage, polDelay, polCurrent, leakVoltage, leakDelay, current))
        if endSample is not None:
            elapsed, current = endSample
            self._uiQueue.put(("leak_result", loopStart + elapsed, runId, waferId, dieId, nplc, polVoltage, polDelay, polCurrent, leakVoltage, duration, current))

    def _on_loop_test_done(self):
        self.statusVar.set("Loop test done.")
        self.startButton.configure(state="normal")
        self.continuousButton.configure(state="normal")
        self.loopTestButton.configure(state="normal")

    def _on_loop_test_error(self, exc):
        self.statusVar.set("Error.")
        messagebox.showerror("Loop test error", str(exc))
        self.startButton.configure(state="normal")
        self.continuousButton.configure(state="normal")
        self.loopTestButton.configure(state="normal")

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

        csvRow = [timestamp, None, None, None, self.nplcVar.get(), None, None, None, None, leakVoltage, self.leakDelayVar.get(), leakCurrent, self._to_na(leakCurrent)]
        try:
            self._append_csv(self.csvPathVar.get(), csvRow)
        except OSError as exc:
            messagebox.showerror("CSV write failed", str(exc))

        self.statusVar.set("Reading stored.")


if __name__ == "__main__":
    app = LeakageGUI()
    app.mainloop()
