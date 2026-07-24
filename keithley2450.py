import time
import pyvisa


class LeakageMachineError(RuntimeError):
    pass


class LeakageMachine:
    #Class function
    def __init__(self, resourceName, timeoutMs=5000):
        self.resourceName = resourceName
        self._resourceManager = pyvisa.ResourceManager()
        self._visaResource = self._resourceManager.open_resource(resourceName) # open connection with the machine
        self._visaResource.timeout = timeoutMs #Waiting time before it raises an error
        self._visaResource.read_termination = "\n" #When it reads the machine answer this jump means the message is done
        self._visaResource.write_termination = "\n" # When it reads this symbol the machine will know it's the end of your answer
        try:
            self._visaResource.clear() #flush any stale reply left in the instrument's buffer before we start talking to it
        except Exception:
            pass

    #Updated write function to take into account possible failures
    def _send(self, command):
        self._visaResource.write(command)
        self._check_error(command)

    #To handle error in query, TODO check CLS it seems to be another way to handle that
    def _check_error(self, command):
        response = self._visaResource.query("SYST:ERR?").strip()
        try:
            code, message = response.split(",", 1)
            code = int(code)
        except ValueError:
            raise LeakageMachineError(f"Nop '{command}': unexpected error-queue reply {response!r}")
        if code != 0:
            raise LeakageMachineError(f"Nop '{command}': {message.strip()}")


    #UI things #######################


    #To list all of the devices plugged to the laptop
    @staticmethod
    def find_devices():
        resourceManager = pyvisa.ResourceManager()
        try:
            return list(resourceManager.list_resources())
        finally:
            resourceManager.close()

    #To ask for the instrument identity
    def get_id(self):
        return self._visaResource.query("*IDN?").strip()

    #One-time setup, done before the timed polarization/leakage sequence
    #Source -> SOUR
    #Sense -> SENS
    def setup(self, nplc=1.0, autozero=False, currentRange=None, sourceDelay=0.0001):
        self._send("*RST") #default settings
        self._send("SOUR:FUNC VOLT") #We want to send a tension so tension mode
        self._send("SOUR:VOLT:DEL:AUTO OFF") #use our own fixed source delay instead of the instrument's computed one
        self._send(f"SOUR:VOLT:DEL {sourceDelay}")
        self._send("SOUR:VOLT:READ:BACK OFF") #don't re-measure the sourced voltage before each reading
        self._send('SENS:FUNC "CURR"') #What we measure: the current
        self._send("ROUT:TERM FRONT") #front-panel terminals
        self._send("SENS:CURR:RSEN OFF") #2-wire sense
        if currentRange is None:
            self._send("SENS:CURR:RANG:AUTO ON")
        else:
            self._send("SENS:CURR:RANG:AUTO OFF")
            self._send(f"SENS:CURR:RANG {currentRange}")#We could define that.
        self._send(f"SENS:CURR:NPLC {nplc}") #How long the measurement will take, and how many values it will take into account
        self._send(f"SENS:CURR:AZER {'ON' if autozero else 'OFF'}") #do we want to take a break once in a while for calibration
        self._send("DISP:SCR GRAPH") #show the graph screen on the instrument's front panel

    #To stop the machine sending a signal
    #If for some reason it's not possible by security we close the connection
    def close(self):
        try:
            self.turn_off()
        except Exception:
            pass
        finally:
            self._visaResource.close()
            self._resourceManager.close()

    #We send a signal to start sending tension
    def turn_on(self):
        self._send("OUTP ON")

    #We send a signal to stop sending the tension
    def turn_off(self):
        self._send("OUTP OFF")

    def set_voltage(self, volts, currentLimit=None):
        """currentLimit: float amps, or None for no compliance limit (instrument max)."""
        self._send(f"SOUR:VOLT:RANG {abs(volts) * 1.5}") #50% margin so the range never clips the actual voltage
        self._send(f"SOUR:VOLT:ILIM {'MAX' if currentLimit is None else currentLimit}") #Limit of maximum current, it's for component security
        self._send(f"SOUR:VOLT {volts}") #real voltage that will be sent

    #Change NPLC on its own, without a full *RST like setup() does
    def set_nplc(self, nplc):
        self._send(f"SENS:CURR:NPLC {nplc}")

    #READ? ask a new measure and give back current,timestamp,status separated by comma
    #we only keep the current so we take the first part
    def get_current(self):
        return float(self._visaResource.query("READ?").strip().split(",")[0])


    #Returns a list of (relative_timestamp_s, current_A) tuples.
    #Used by the look function below
    def run_duration_loop(self, duration):
        self._send('TRACe:CLEar "defbuffer1"') #wipe old readings so they don't mix with this run
        self._send(f'TRIGger:LOAD "DurationLoop", {duration}') #load the instrument's built-in timed-measurement program
        self._send("INITiate") #and actually start it
        while True:
            state = self._visaResource.query("TRIGger:STATe?").strip().split(";")[0] #ask if it's still running
            if "RUNNING" not in state.upper():
                break #done, instrument finished the duration on its own
            time.sleep(0.1)
        count = int(self._visaResource.query('TRACe:ACTual? "defbuffer1"').strip()) #how many points it actually took
        if count == 0:
            return []
        raw = self._visaResource.query(f'TRACe:DATA? 1, {count}, "defbuffer1", REL, READ').strip() #pull everything in one go: timestamp+current per point
        values = [float(v) for v in raw.split(",")] #flat list: rel1,read1,rel2,read2,...
        return list(zip(values[0::2], values[1::2])) #un-flatten back into (rel, read) pairs


    #Runs the full polarization + leakage measurement
    #Returns (polCurrent, leakCurrent).
    #It's basically for only measurement
    def run_test(self, polVoltage, polDelay,leakVoltage,leakDelay,currentLimit=None,prePause=0.0,progress=lambda message: None):
        try:
            #Polarization part asked by Laura
            progress("Polarizing")
            self.set_voltage(polVoltage, currentLimit=currentLimit)
            self.turn_on()
            time.sleep(polDelay)
            polCurrent = self.get_current()


            #Sleep time in the same way Bianca did it, not really necessary about what I understood
            if prePause > 0:
                progress("Waiting before measurement")
                time.sleep(prePause)

            #Real leakage measurement
            progress("Leakage")
            self.set_voltage(leakVoltage, currentLimit=currentLimit)
            time.sleep(leakDelay)
            leakCurrent = self.get_current()

            #Return both
            progress("Done.")
            return polCurrent, leakCurrent
        finally:
            #By security, we stop the signal at the end
            self.turn_off()

    #Runs the polarization + timed leakage buffer capture used by the GUI's loop test.
    def run_loop_test(self, polVoltage, polDelay, leakVoltage, leakDelay, duration, currentLimit=None, prePause=0.0, progress=lambda message: None):
        try:
            if prePause > 0:
                progress("Waiting before polarization")
                time.sleep(prePause)

            progress("Polarizing")
            self.set_voltage(polVoltage, currentLimit=currentLimit)
            self.turn_on()
            time.sleep(polDelay)
            polCurrent = self.get_current()

            self.turn_off() #so leakage starts from 0V (charging up) instead of stepping down from polVoltage
            time.sleep(3.0) #same 3s off-time as Bianca's script between polarization and leakage

            progress("Loop test running...")
            self.set_voltage(leakVoltage, currentLimit=currentLimit)
            self.turn_on()
            samples = self.run_duration_loop(duration)

            bestSample = min(samples, key=lambda sample: abs(sample[0] - leakDelay), default=None)
            endSample = max(samples, key=lambda sample: sample[0], default=None)

            progress("Done.")
            return polCurrent, samples, bestSample, endSample
        finally:
            self.turn_off()
