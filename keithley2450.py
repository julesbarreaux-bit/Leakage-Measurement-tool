import time
import pyvisa


SOURCE_VOLTAGE_RANGE = 20 # it should probably be dynamic, for now with laura's test it's fine


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

    #Updated write function to take into account possible failures
    def _send(self, command):
        self._visaResource.write(command)
        self._check_error(command)

    #To handle error in query, TODO check CLS it seems to be another way to handle that
    def _check_error(self, command):
        code, message = self._visaResource.query("SYST:ERR?").strip().split(",", 1)
        if int(code) != 0:
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
    def setup(self, nplc=0.1, autozero=False, currentRange=None):
        self._send("*RST") #default settings
        self._send("SOUR:FUNC VOLT") #We want to send a tension so tension mode
        self._send(f"SOUR:VOLT:RANG {SOURCE_VOLTAGE_RANGE}") # the scale/range
        self._send('SENS:FUNC "CURR"') #What we measure: the current
        if currentRange is None:
            self._send("SENS:CURR:RANG:AUTO ON")
        else:
            self._send("SENS:CURR:RANG:AUTO OFF")
            self._send(f"SENS:CURR:RANG {currentRange}")#We could define that
        self._send(f"SENS:CURR:NPLC {nplc}") #How long the measurement will take, and how many values it will take into account
        self._send(f"SENS:CURR:AZER {'ON' if autozero else 'OFF'}") #do we want to take a break once in a while for calibration

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
        self._send(f"SOUR:VOLT:ILIM {'MAX' if currentLimit is None else currentLimit}") #Limit of maximum current, it's for component security
        self._send(f"SOUR:VOLT {volts}") #real voltage that will be sent

    #READ? ask a new measure and give back current,timestamp,status separated by comma
    #we only keep the current so we take the first part
    def get_current(self):
        return float(self._visaResource.query("READ?").strip().split(",")[0])


    #Runs the full polarization + leakage measurement
    #Returns (polCurrent, leakCurrent).
    def run_test(self, polVoltage, polDelay,leakVoltage,leakDelay,currentLimit=None,progress=lambda message: None):
        try:
            #Polarization part asked by Laura
            progress("Polarizing")
            self.set_voltage(polVoltage, currentLimit=currentLimit)
            self.turn_on()
            time.sleep(polDelay)
            polCurrent = self.get_current()

            #Real leakage measurement
            progress("Leakage")
            self.set_voltage(leakVoltage, currentLimit=currentLimit)
            time.sleep(leakDelay)
            leakCurrent = self.get_current()

            #Return both
            progress("Done.")
            return polCurrent, leakCurrent
        finally:
            #By security we stop the signal at the end
            self.turn_off()
