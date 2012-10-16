#!/usr/bin/env python

# built in imports
import os
import datetime
from math import pi
from warnings import warn
from ConfigParser import SafeConfigParser

import pdb
# debugging
#try:
    #from IPython.core.debugger import Tracer
#except ImportError:
    #pass
#else:
    #set_trace = Tracer()

# dependencies
import numpy as np
from scipy import io
from scipy.integrate import cumtrapz
from scipy.optimize import curve_fit
import matplotlib.pyplot as plt
from tables import NoSuchNodeError

import dtk.process as process

from dtk.bicycle import front_contact, benchmark_to_moore 
from dtk.bicycle import front_wheel_yaw_angle, front_wheel_rate
from dtk.bicycle import steer_torque_slip, contact_forces_slip, contact_forces_nonslip
from dtk.bicycle import contact_points_acceleration

import bicycleparameters as bp


# local dependencies
from database import get_row_num, get_cell, pad_with_zeros, run_id_string
import signalprocessing as sigpro
from bdpexceptions import TimeShiftError

config = SafeConfigParser()
config.read(os.path.join(os.path.dirname(__file__), '..', 'defaults.cfg'))

class Signal(np.ndarray):
    """
    A subclass of ndarray for collecting the data for a single signal in a run.

    Attributes
    ----------
    conversions : dictionary
        A mapping for unit conversions.
    name : str
        The name of the signal. Should be CamelCase.
    runid : str
        A five digit identification number associated with the
        trial this signal was collected from (e.g. '00104').
    sampleRate : float
        The sample rate in hertz of the signal.
    source : str
        The source of the data. This should be 'NI' for the
        National Instruments USB-6218 and 'VN' for the VN-100 IMU.
    units : str
        The physcial units of the signal. These should be specified
        as lowercase complete words using only multiplication and
        division symbols (e.g. 'meter/second/second').
        Signal.conversions will show the avialable options.

    Methods
    -------
    plot()
        Plot's the signal versus time and returns the line.
    frequency()
        Returns the frequency spectrum of the signal.
    time_derivative()
        Returns the time derivative of the signal.
    filter(frequency)
        Returns the low passed filter of the signal.
    truncate(tau)
        Interpolates and truncates the signal the based on the time shift,
        `tau`, and the signal source.
    as_dictionary
        Returns a dictionary of the metadata of the signal.
    convert_units(units)
        Returns a signal with different units. `conversions` specifies the
        available options.

    """

    # define some basic unit converions
    conversions = {'degree->radian': pi / 180.,
                   'degree/second->radian/second': pi / 180.,
                   'degree/second/second->radian/second/second': pi / 180.,
                   'inch*pound->newton*meter': 25.4 / 1000. * 4.44822162,
                   'pound->newton': 4.44822162,
                   'feet/second->meter/second': 12. * 2.54 / 100.,
                   'mile/hour->meter/second': 0.00254 * 12. / 5280. / 3600.}

    def __new__(cls, inputArray, metadata):
        """
        Returns an instance of the Signal class with the additional signal
        data.

        Parameters
        ----------
        inputArray : ndarray, shape(n,)
            A one dimension array representing a single variable's time
            history.
        metadata : dictionary
            This dictionary contains the metadata for the signal.
                name : str
                    The name of the signal. Should be CamelCase.
                runid : str
                    A five digit identification number associated with the
                    trial this experiment was collected at (e.g. '00104').
                sampleRate : float
                    The sample rate in hertz of the signal.
                source : str
                    The source of the data. This should be 'NI' for the
                    National Instruments USB-6218 and 'VN' for the VN-100 IMU.
                units : str
                    The physcial units of the signal. These should be specified
                    as lowercase complete words using only multiplication and
                    division symbols (e.g. 'meter/second/second').
                    Signal.conversions will show the avialable options.

        Raises
        ------
        ValueError
            If `inputArray` is not a vector.

        """
        if len(inputArray.shape) > 1:
            raise ValueError('Signals must be arrays of one dimension.')
        # cast the input array into the Signal class
        obj = np.asarray(inputArray).view(cls)
        # add the metadata to the object
        obj.name = metadata['name']
        obj.runid = metadata['runid']
        obj.sampleRate = metadata['sampleRate']
        obj.source = metadata['source']
        obj.units = metadata['units']
        return obj

    def __array_finalize__(self, obj):
        if obj is None: return
        self.name = getattr(obj, 'name', None)
        self.runid = getattr(obj, 'runid', None)
        self.sampleRate = getattr(obj, 'sampleRate', None)
        self.source = getattr(obj, 'source', None)
        self.units = getattr(obj, 'units', None)

    def __array_wrap__(self, outputArray, context=None):
        # doesn't support these things in basic ufunc calls...maybe one day
        # That means anytime you add, subtract, multiply, divide, etc, the
        # following are not retained.
        outputArray.name = None
        outputArray.source = None
        outputArray.units = None
        return np.ndarray.__array_wrap__(self, outputArray, context)

    def as_dictionary(self):
        '''Returns the signal metadata as a dictionary.'''
        data = {'runid': self.runid,
                'name': self.name,
                'units': self.units,
                'source': self.source,
                'sampleRate': self.sampleRate}
        return data

    def convert_units(self, units):
        """
        Returns a signal with the specified units.

        Parameters
        ----------
        units : str
            The units to convert the signal to. The mapping must be in the
            attribute `conversions`.

        Returns
        -------
        newSig : Signal
            The signal with the desired units.

        """
        if units == self.units:
            return self
        else:
            try:
                conversion = self.units + '->' + units
                newSig = self * self.conversions[conversion]
            except KeyError:
                try:
                    conversion = units + '->' + self.units
                    newSig = self / self.conversions[conversion]
                except KeyError:
                    raise KeyError(('Conversion from {0} to {1} is not ' +
                        'possible or not defined.').format(self.units, units))
            # make the new signal
            newSig = Signal(newSig, self.as_dictionary())
            newSig.units = units

            return newSig

    def filter(self, frequency):
        """Returns the signal filtered by a low pass Butterworth at the given
        frequency."""
        filteredArray = process.butterworth(self.spline(), frequency, self.sampleRate)
        return Signal(filteredArray, self.as_dictionary())

    def frequency(self):
        """Returns the frequency content of the signal."""
        return process.freq_spectrum(self.spline(), self.sampleRate)

    def integrate(self, initialCondition=0., detrend=False):
        """Integrates the signal using the trapezoidal rule."""
        time = self.time()
        # integrate using trapz and adjust with the initial condition
        grated = np.hstack((0., cumtrapz(self, x=time))) + initialCondition
        # this tries to characterize the drift in the integrated signal. It
        # works well for signals from straight line tracking but not
        # necessarily for lange change.
        if detrend is True:
            def line(x, a, b, c):
                return a * x**2 + b * x + c
            popt, pcov = curve_fit(line, time, grated)
            grated = grated - line(time, popt[0], popt[1], popt[2])
        grated = Signal(grated, self.as_dictionary())
        grated.units = self.units + '*second'
        grated.name = self.name + 'Int'
        return grated

    def plot(self, show=True):
        """Plots and returns the signal versus time."""
        time = self.time()
        line = plt.plot(time, self)
        if show:
            plt.xlabel('Time [second]')
            plt.ylabel('{0} [{1}]'.format(self.name, self.units))
            plt.title('Signal plot during run {0}'.format(self.runid))
            plt.show()
        return line

    def spline(self):
        """Returns the signal with nans replaced by the results of a cubic
        spline."""
        splined = process.spline_over_nan(self.time(), self)
        return Signal(splined, self.as_dictionary())

    def subtract_mean(self):
        """Returns the mean subtracted data."""
        return Signal(process.subtract_mean(self), self.as_dictionary())

    def time(self):
        """Returns the time vector of the signal."""
        return sigpro.time_vector(len(self), self.sampleRate)

    def time_derivative(self):
        """Returns the time derivative of the signal."""
        # caluculate the numerical time derivative
        dsdt = process.derivative(self.time(), self, method='combination')
        # map the metadata from self onto the derivative
        dsdt = Signal(dsdt, self.as_dictionary())
        dsdt.name = dsdt.name + 'Dot'
        dsdt.units = dsdt.units + '/second'
        return dsdt

    def truncate(self, tau):
        '''Returns the shifted and truncated signal based on the provided
        timeshift, tau.'''
        # this is now an ndarray instead of a Signal
        return Signal(sigpro.truncate_data(self, tau), self.as_dictionary())

class RawSignal(Signal):
    """
    A subclass of Signal for collecting the data for a single raw signal in
    a run.

    Attributes
    ----------
    sensor : Sensor
        Each raw signal has a sensor associated with it. Most sensors contain
        calibration data for that sensor/signal.
    calibrationType :

    Notes
    -----
    This is a class for the signals that are the raw measurement outputs
    collected by the BicycleDAQ software and are already stored in the pytables
    database file.

    """

    def __new__(cls, runid, signalName, database):
        """
        Returns an instance of the RawSignal class with the additional signal
        metadata.

        Parameters
        ----------
        runid : str
            A five digit
        signalName : str
            A CamelCase signal name that corresponds to the raw signals output
            by BicycleDAQ_.
        database : pytables object
            The hdf5 database for the instrumented bicycle.

        .. _BicycleDAQ: https://github.com/moorepants/BicycleDAQ

        """

        # get the tables
        rTab = database.root.runTable
        sTab = database.root.signalTable
        cTab = database.root.calibrationTable

        # get the row number for this particular run id
        rownum = get_row_num(runid, rTab)
        signal = database.getNode('/rawData/' + runid, name=signalName).read()

        # cast the input array into my subclass of ndarray
        obj = np.asarray(signal).view(cls)

        obj.runid = runid
        obj.timeStamp = matlab_date_to_object(get_cell(rTab, 'DateTime',
            rownum))
        obj.calibrationType, obj.units, obj.source = [(row['calibration'],
            row['units'], row['source'])
            for row in sTab.where('signal == signalName')][0]
        obj.name = signalName

        try:
            obj.sensor = Sensor(obj.name, cTab)
        except KeyError:
            pass
            # This just means that there was no sensor associated with that
            # signal for calibration purposes.
            #print "There is no sensor named {0}.".format(signalName)

        # this assumes that the supply voltage for this signal is the same for
        # all sensor calibrations
        try:
            supplySource = [row['runSupplyVoltageSource']
                           for row in cTab.where('name == signalName')][0]
            if supplySource == 'na':
                obj.supply = [row['runSupplyVoltage']
                               for row in cTab.where('name == signalName')][0]
            else:
                obj.supply = database.getNode('/rawData/' + runid,
                        name=supplySource).read()
        except IndexError:
            pass
            #print "{0} does not have a supply voltage.".format(signalName)
            #print "-" * 79

        # get the appropriate sample rate
        if obj.source == 'NI':
            sampRateCol = 'NISampleRate'
        elif obj.source == 'VN':
            sampRateCol = 'VNavSampleRate'
        else:
            raise ValueError('{0} is not a valid source.'.format(obj.source))

        obj.sampleRate = rTab[rownum][rTab.colnames.index(sampRateCol)]

        return obj

    def __array_finalize__(self, obj):
        if obj is None: return
        self.calibrationType = getattr(obj, 'calibrationType', None)
        self.name = getattr(obj, 'name', None)
        self.runid = getattr(obj, 'runid', None)
        self.sampleRate = getattr(obj, 'sampleRate', None)
        self.sensor = getattr(obj, 'sensor', None)
        self.source = getattr(obj, 'source', None)
        self.units = getattr(obj, 'units', None)
        self.timeStamp = getattr(obj, 'timeStamp', None)

    def __array_wrap__(self, outputArray, context=None):
        # doesn't support these things in basic ufunc calls...maybe one day
        outputArray.calibrationType = None
        outputArray.name = None
        outputArray.sensor = None
        outputArray.source = None
        outputArray.units = None
        return np.ndarray.__array_wrap__(self, outputArray, context)

    def scale(self):
        """
        Returns the scaled signal based on the calibration data for the
        supplied date.

        Returns
        -------
        : ndarray (n,)
            Scaled signal.

        """
        try:
            self.calibrationType
        except AttributeError:
            raise AttributeError("Can't scale without the calibration type")

        # these will need to be changed once we start measuring them
        doNotScale = ['LeanPotentiometer',
                      'HipPotentiometer',
                      'TwistPotentiometer']
        if self.calibrationType in ['none', 'matrix'] or self.name in doNotScale:
            #print "Not scaling {0}".format(self.name)
            return self
        else:
            pass
            #print "Scaling {0}".format(self.name)

            # pick the largest calibration date without surpassing the run date
            calibData = self.sensor.get_data_for_date(self.timeStamp)

            slope = calibData['slope']
            bias = calibData['bias']
            intercept = calibData['offset']
            calibrationSupplyVoltage = calibData['calibrationSupplyVoltage']

            #print "slope {0}, bias {1}, intercept {2}".format(slope, bias,
                    #intercept)

            if self.calibrationType == 'interceptStar':
                # this is for potentiometers, where the slope is ratiometric
                # and zero degrees is always zero volts
                calibratedSignal = (calibrationSupplyVoltage / self.supply *
                                    slope * self + intercept)
            elif self.calibrationType == 'intercept':
                # this is the typical calibration that I use for all the
                # sensors that I calibrate myself
                calibratedSignal = (calibrationSupplyVoltage / self.supply *
                                    (slope * self + intercept))
            elif self.calibrationType == 'bias':
                # this is for the accelerometers and rate gyros that are
                # "ratiometric", but I'm still not sure this is correct
                calibratedSignal = (slope * (self - self.supply /
                                    calibrationSupplyVoltage * bias))
            else:
                raise StandardError("None of the calibration equations worked.")
            calibratedSignal.name = calibData['signal']
            calibratedSignal.units = calibData['units']
            calibratedSignal.source = self.source

            return calibratedSignal.view(Signal)

    def plot_scaled(self, show=True):
        '''Plots and returns the scaled signal versus time.'''
        time = self.time()
        scaled = self.scale()
        line = plt.plot(time, scaled[1])
        plt.xlabel('Time [s]')
        plt.ylabel(scaled[2])
        plt.title('{0} signal during run {1}'.format(scaled[0],
                  str(self.runid)))
        if show:
            plt.show()
        return line

class Sensor():
    """This class is a container for calibration data for a sensor."""

    def __init__(self, name, calibrationTable):
        """
        Initializes this sensor class.

        Parameters
        ----------
        name : string
            The CamelCase name of the sensor (e.g. SteerTorqueSensor).
        calibrationTable : pyTables table object
            This is the calibration data table that contains all the data taken
            during calibrations.

        """
        self.name = name
        self._store_calibration_data(calibrationTable)

    def _store_calibration_data(self, calibrationTable):
        """
        Stores a dictionary of calibration data for the sensor for all
        calibration dates in the object.

        Parameters
        ----------
        calibrationTable : pyTables table object
            This is the calibration data table that contains all the data taken
            during calibrations.

        """
        self.data = {}

        for row in calibrationTable.iterrows():
            if self.name == row['name']:
                self.data[row['calibrationID']] = {}
                for col in calibrationTable.colnames:
                    self.data[row['calibrationID']][col] = row[col]

        if self.data == {}:
            raise KeyError(('{0} is not a valid sensor ' +
                           'name').format(self.name))

    def get_data_for_date(self, runDate):
        """
        Returns the calibration data for the sensor for the most recent
        calibration relative to `runDate`.

        Parameters
        ----------
        runDate : datetime object
            This is the date of the run that the calibration data is needed
            for.

        Returns
        -------
        calibData : dictionary
            A dictionary containing the sensor calibration data for the
            calibration closest to but not past `runDate`.

        Notes
        -----
        This method will select the calibration data for the date closest to
        but not past `runDate`. **All calibrations must be taken before the
        runs.**

        """
        # make a list of calibration ids and time stamps
        dateIdPairs = [(k, matlab_date_to_object(v['timeStamp']))
                       for k, v in self.data.iteritems()]
        # sort the pairs with the most recent date first
        dateIdPairs.sort(key=lambda x: x[1], reverse=True)
        # go through the list and return the index at which the calibration
        # date is larger than the run date
        for i, pair in enumerate(dateIdPairs):
            if runDate >= pair[1]:
                break
        return self.data[dateIdPairs[i][0]]

class Run():
    """The fluppin fundamental class for a run."""

    def __init__(self, runid, dataset, pathToParameterData=None,
            forceRecalc=False, filterFreq=None, store=True):
        """Loads the raw and processed data for a run if available otherwise it
        generates the processed data from the raw data.

        Parameters
        ----------
        runid : int or str
            The run id should be an integer, e.g. 5, or a five digit string with
            leading zeros, e.g. '00005'.
        dataset : DataSet
            A DataSet object with at least some raw data.
        pathToParameterData : string, {'<path>', None},  optional
            The path to a data directory for the BicycleParameters package. It
            should contain the bicycles and riders used in the experiments.
        forceRecalc : boolean, optional, default = False
            If true then it will force a recalculation of all the processed
            data.
        filterSigs : float, optional, default = None
            If true all of the processed signals will be low pass filtered with
            a second order Butterworth filter at the given filter frequency.
        store : boolean, optional, default = True
            If true the resulting task signals will be stored in the database.

        """

        if pathToParameterData is None:
            pathToParameterData = config.get('data', 'pathToParameters')

        print "Initializing the run object."

        self.filterFreq = filterFreq

        dataset.open()
        dataTable = dataset.database.root.runTable
        signalTable = dataset.database.root.signalTable
        taskTable = dataset.database.root.taskTable

        runid = run_id_string(runid)

        # get the row number for this particular run id
        rownum = get_row_num(runid, dataTable)

        # make some dictionaries to store all the data
        self.metadata = {}
        self.rawSignals = {}

        # make lists of the input and output signals
        rawDataCols = [x['signal'] for x in
                       signalTable.where("isRaw == True")]
        computedCols = [x['signal'] for x in
                        signalTable.where("isRaw == False")]

        # store the metadata for this run
        print "Loading metadata from the database."
        for col in dataTable.colnames:
            if col not in (rawDataCols + computedCols):
                self.metadata[col] = get_cell(dataTable, col, rownum)

        print "Loading the raw signals from the database."
        for col in rawDataCols:
            # rawDataCols includes all possible raw signals, but every run
            # doesn't have all the signals, so skip the ones that aren't there
            try:
                self.rawSignals[col] = RawSignal(runid, col, dataset.database)
            except NoSuchNodeError:
                pass

        if self.metadata['Rider'] != 'None':
            self.load_rider(pathToParameterData)

        self.bumpLength = 1.0 # 1 meter

        # Try to load the task signals if they've already been computed. If
        # they aren't in the database, the filter frequencies don't match or
        # forceRecalc is true the then compute them. This may save some time
        # when repeatedly loading runs for analysis.
        self.taskFromDatabase = False
        try:
            runGroup = dataset.database.root.taskData._f_getChild(runid)
        except NoSuchNodeError:
            forceRecalc = True
        else:
            # The filter frequency stored in the task table is either a nan
            # value or a valid float. If the stored filter frequency is not the
            # same as the the one passed to Run, then a recalculation should be
            # forced.
            taskRowNum = get_row_num(runid, taskTable)
            storedFreq = taskTable.cols.FilterFrequency[taskRowNum]
            self.taskSignals = {}
            if filterFreq is None:
                newFilterFreq = np.nan
            else:
                newFilterFreq = filterFreq

            if np.isnan(newFilterFreq) and np.isnan(storedFreq):
                for node in runGroup._f_walkNodes():
                    meta = {k : node._f_getAttr(k) for k in ['units', 'name',
                        'runid', 'sampleRate', 'source']}
                    self.taskSignals[node.name] = Signal(node[:], meta)
                self.taskFromDatabase = True
            elif np.isnan(newFilterFreq) or np.isnan(storedFreq):
                forceRecalc = True
            else:
                if abs(storedFreq - filterFreq) < 1e-10:
                    for node in runGroup._f_walkNodes():
                        meta = {k : node._f_getAttr(k) for k in ['units', 'name',
                        'runid', 'sampleRate', 'source']}
                        self.taskSignals[node.name] = Signal(node[:], meta)
                    self.taskFromDatabase = True
                else:
                    forceRecalc = True

        dataset.close()

        if forceRecalc == True:
            try:
                del self.taskSignals
            except AttributeError:
                pass
            self.process_raw_signals()

        # store the task signals in the database if they are newly computed
        if (store == True and self.taskFromDatabase == False
                and self.topSig == 'task'):
            taskMeta = {
                        'Duration' :
                        self.taskSignals['ForwardSpeed'].time()[-1],
                        'FilterFrequency' : self.filterFreq,
                        'MeanSpeed' : self.taskSignals['ForwardSpeed'].mean(),
                        'RunID' : self.metadata['RunID'],
                        'StdSpeed' : self.taskSignals['ForwardSpeed'].std(),
                        'Tau' : self.tau,
                        }
            dataset.add_task_signals(self.taskSignals, taskMeta)

        # tell the user about the run
        print self

    def process_raw_signals(self):
        """Processes the raw signals as far as possible and filters the
        result if a cutoff frequency was specified."""

        print "Computing signals from raw data."
        self.calibrate_signals()

        # the following maneuvers should never be calculated beyond the
        # calibrated signals
        maneuver = self.metadata['Maneuver']
        con1 = maneuver != 'Steer Dynamics Test'
        con2 = maneuver != 'System Test'
        con3 = maneuver != 'Static Calibration'
        if con1 and con2 and con3:
            self.compute_time_shift()
            self.check_time_shift(0.15)
            self.truncate_signals()
            self.compute_signals()
            self.task_signals()

        if self.filterFreq is not None:
            self.filter_top_signals(self.filterFreq)

    def filter_top_signals(self, filterFreq):
        """Filters the top most signals with a low pass filter."""

        if self.topSig == 'task':
            print('Filtering the task signals.')
            for k, v in self.taskSignals.items():
                self.taskSignals[k] = v.filter(filterFreq)
        elif self.topSig == 'computed':
            print('Filtering the computed signals.')
            for k, v in self.computedSignals.items():
                self.computedSignals[k] = v.filter(filterFreq)
        elif self.topSig == 'calibrated':
            print('Filtering the calibrated signals.')
            for k, v in self.calibratedSignals.items():
                self.calibratedSignals[k] = v.filter(filterFreq)

    def calibrate_signals(self):
        """Calibrates the raw signals."""

        # calibrate the signals for the run
        self.calibratedSignals = {}
        for sig in self.rawSignals.values():
            calibSig = sig.scale()
            self.calibratedSignals[calibSig.name] = calibSig

        self.topSig = 'calibrated'

    def task_signals(self):
        """Computes the task signals."""
        print('Extracting the task portion from the data.')
        self.extract_task()

        # compute task specific variables*
        self.compute_yaw_angle()
        self.compute_rear_wheel_contact_rates()
        self.compute_rear_wheel_contact_points()
        self.compute_front_wheel_contact_points()
        self.compute_front_wheel_yaw_angle()
        self.compute_front_wheel_rate()
        self.compute_contact_points_acceleration()
        self.compute_steer_torque_slip()
        self.compute_contact_forces_slip()
        self.compute_contact_forces_nonslip()

        self.topSig = 'task'

    def compute_signals(self):
        """Computes the task independent quantities."""

        self.computedSignals ={}
        # transfer some of the signals to computed
        noChange = ['FiveVolts',
                    'PushButton',
                    'RearWheelRate',
                    'RollAngle',
                    'SteerAngle',
                    'ThreeVolts']
        for sig in noChange:
            if sig in ['RollAngle', 'SteerAngle']:
                self.computedSignals[sig] =\
                self.truncatedSignals[sig].convert_units('radian')
            else:
                self.computedSignals[sig] = self.truncatedSignals[sig]

        # compute the quantities that aren't task specific
        self.compute_pull_force()
        self.compute_frame_acceleration()
        self.compute_forward_speed()
        self.compute_steer_rate()
        self.compute_yaw_roll_pitch_rates()
        self.compute_steer_torque()


    def truncate_signals(self):
        """Truncates the calibrated signals based on the time shift."""

        self.truncatedSignals = {}
        for name, sig in self.calibratedSignals.items():
            self.truncatedSignals[name] = sig.truncate(self.tau).spline()
        self.topSig = 'truncated'

    def compute_time_shift(self):
        """Computes the time shift based on the vertical accelerometer
        signals."""

        self.tau = sigpro.find_timeshift(
            self.calibratedSignals['AccelerometerAccelerationY'],
            self.calibratedSignals['AccelerationZ'],
            self.metadata['NISampleRate'],
            self.metadata['Speed'], plotError=False)

    def check_time_shift(self, maxNRMS):
        """Raises an error if the normalized root mean square of the shifted
        accelerometer signals is high."""

        # Check to make sure the signals were actually good fits by
        # calculating the normalized root mean square. If it isn't very
        # low, raise an error.
        niAcc = self.calibratedSignals['AccelerometerAccelerationY']
        vnAcc = self.calibratedSignals['AccelerationZ']
        vnAcc = vnAcc.truncate(self.tau).spline()
        niAcc = niAcc.truncate(self.tau).spline()
        # todo: this should probably check the rms of the mean subtracted data
        # because both accelerometers don't always give the same value, this
        # may work better with a filtered signal too
        # todo: this should probably be moved into the time shift code in the
        # signalprocessing model
        nrms = np.sqrt(np.mean((vnAcc + niAcc)**2)) / (niAcc.max() - niAcc.min())
        if nrms > maxNRMS:
            raise TimeShiftError(('The normalized root mean square for this ' +
                'time shift is {}, which is greater '.format(str(nrms)) +
                'than the maximum allowed: {}'.format(str(maxNRMS))))

    def compute_rear_wheel_contact_points(self):
        """Computes the location of the wheel contact points in the ground
        plane."""

        # get the rates
        try:
            latRate = self.taskSignals['LateralRearContactRate']
            lonRate = self.taskSignals['LongitudinalRearContactRate']
        except AttributeError:
            print('At least one of the rates are not available. ' +
                  'The YawAngle was not computed.')
        else:
            # convert to meters per second
            latRate = latRate.convert_units('meter/second')
            lonRate = lonRate.convert_units('meter/second')
            # integrate and try to account for the drift
            lat = latRate.integrate(detrend=True)
            lon = lonRate.integrate()
            # set the new name and units
            lat.name = 'LateralRearContact'
            lat.units = 'meter'
            lon.name = 'LongitudinalRearContact'
            lon.units = 'meter'
            # store in task signals
            self.taskSignals[lat.name] = lat
            self.taskSignals[lon.name] = lon

    def compute_front_wheel_contact_points(self):
        """Caluculates the front wheel contact points in the ground plane."""

        q1 = self.taskSignals['LongitudinalRearContact']
        q2 = self.taskSignals['LateralRearContact']
        q3 = self.taskSignals['YawAngle']
        q4 = self.taskSignals['RollAngle']
        q7 = self.taskSignals['SteerAngle']

        p = benchmark_to_moore(self.bicycleRiderParameters)

        f = np.vectorize(front_contact)
        q9, q10 = f(q1, q2, q3, q4, q7, p['d1'], p['d2'], p['d3'], p['rr'],
            p['rf'])

        q9.name = 'LongitudinalFrontContact'
        q9.units = 'meter'
        q10.name = 'LateralFrontContact'
        q10.units = 'meter'

        self.taskSignals['LongitudinalFrontContact'] = q9
        self.taskSignals['LateralFrontContact'] = q10

    def compute_front_wheel_yaw_angle(self):
        """Calculates the yaw angle of the front wheel."""

        bp = self.bicycleRiderParameters
        mp = benchmark_to_moore(bp)

        q1 = self.taskSignals['YawAngle']
        q2 = self.taskSignals['RollAngle']
        q4 = self.taskSignals['SteerAngle']

        f = np.vectorize(front_wheel_yaw_angle)
        q1_front_wheel = f(q1, q2, q4, mp['d1'], mp['d2'], mp['d3'], 
            bp['lam'], mp['rr'], mp['rf'])

        q1_front_wheel.name = 'FrontWheelYawAngle'
        q1_front_wheel.units = 'radian'
        self.taskSignals['FrontWheelYawAngle'] = q1_front_wheel

    def compute_front_wheel_rate(self):
        """Calculates the front wheel rate, based on the Jason's data 
        of front_wheel_contact_point. Alternatively, you can use the 
        sympy to get the front_wheel_contact_rate directly first."""

        q1 = self.taskSignals['YawAngle']
        q2 = self.taskSignals['RollAngle']
        q4 = self.taskSignals['SteerAngle']
        u9 = self.taskSignals['LongitudinalFrontContact'].time_derivative()
        u10 = self.taskSignals['LateralFrontContact'].time_derivative()

        bp = self.bicycleRiderParameters
        mp = benchmark_to_moore(bp)

        f = np.vectorize(front_wheel_rate)

        u6 = f(q1, q2, q4, u9, u10, mp['d1'], mp['d2'], mp['d3'], 
            bp['lam'], mp['rr'], mp['rf'])

        u6.name = 'FrontWheelRate'
        u6.units = 'radian/second'
        self.taskSignals['FrontWheelRate'] = u6

    def compute_steer_torque_slip(self):
        """Calculate the steer torque on the front frame and back on the rear
        frame, in E['3'] direction, under the slip condition"""

        #parameters
        bp = self.bicycleRiderParameters
        mp = benchmark_to_moore(self.bicycleRiderParameters)

        l1 = mp['l1']
        l2 = mp['l2']
        mc = mp['mc']
        ic11 = mp['ic11']
        ic33 = mp['ic33']
        ic31 = mp['ic31']

        V = self.taskSignals['ForwardSpeed'].mean()

        q2 = self.taskSignals['RollAngle']
        q4 = self.taskSignals['SteerAngle']

        u1 = self.taskSignals['YawRate']
        u2 = self.taskSignals['RollRate']
        u4 = self.taskSignals['SteerRate']
        u8 = self.taskSignals['LateralRearContactRate']
        u9 = self.taskSignals['LongitudinalFrontContact'].time_derivative()
        u10 = self.taskSignals['LateralFrontContact'].time_derivative()

        u1d = u1.time_derivative()
        u2d = u2.time_derivative()
        u4d = u4.time_derivative()
        u8d = self.taskSignals['LateralRearContactAcceleration']
        u10d = self.taskSignals['LateralFrontContactAcceleration']

        f = np.vectorize(steer_torque_slip)

        #calculation
        #steer torque slip
        steerTorque_slip = f(V, l1, l2, mc, ic11, ic33, ic31, q2, q4, 
                    u1, u2, u4, u8, u9, u10, u1d, u2d, u4d, u8d, u10d)

        steerTorque_slip.name = 'SteerTorque_Slip'
        steerTorque_slip.units = 'newton*meter'
        self.taskSignals[steerTorque_slip.name] = steerTorque_slip

    def compute_contact_forces_slip(self):
        """Calculate the contact forces of rear and front wheels with respect 
        to inertial frame under the slip condition."""

        #parameters
        bp = self.bicycleRiderParameters
        mp = benchmark_to_moore(self.bicycleRiderParameters)

        l1 = mp['l1']
        l2 = mp['l2']
        mc = mp['mc']
        ic11 = mp['ic11']
        ic22 = mp['ic22']
        ic33 = mp['ic33']
        ic31 = mp['ic31']

        V = self.taskSignals['ForwardSpeed'].mean()

        q1 = self.taskSignals['YawAngle']
        q2 = self.taskSignals['RollAngle']
        q4 = self.taskSignals['SteerAngle']

        u1 = self.taskSignals['YawRate']
        u2 = self.taskSignals['RollRate']
        u3 = self.taskSignals['PitchRate']
        u4 = self.taskSignals['SteerRate']
        u5 = self.taskSignals['RearWheelRate']
        u6 = self.taskSignals['FrontWheelRate']
        u7 = self.taskSignals['LongitudinalRearContactRate']
        u8 = self.taskSignals['LateralRearContactRate']
        u9 = self.taskSignals['LongitudinalFrontContact'].time_derivative()
        u10 = self.taskSignals['LateralFrontContact'].time_derivative()

        u1d = u1.time_derivative()
        u2d = u2.time_derivative()
        u3d = u3.time_derivative()
        u4d = u4.time_derivative()
        u5d = u5.time_derivative()
        u6d = u6.time_derivative()
        u7d = self.taskSignals['LongitudinalRearContactAcceleration']
        u8d = self.taskSignals['LateralRearContactAcceleration']
        u9d = self.taskSignals['LongitudinalFrontContactAcceleration']
        u10d = self.taskSignals['LateralFrontContactAcceleration']

        f = np.vectorize(contact_forces_slip)

        Fx_r_s, Fy_r_s, Fx_f_s, Fy_f_s = f(V, l1, l2, mc, 
                    ic11, ic22, ic33, ic31, q1, q2, q4, 
                    u1, u2, u3, u4, u5, u6, u7, u8, u9, u10, 
                    u1d, u2d, u3d, u4d, u5d, u6d, u7d, u8d, u9d, u10d)

        Fx_r_s.name = 'LongitudinalRearContactForce_Slip'
        Fx_r_s.units = 'newton'
        self.taskSignals[Fx_r_s.name] = Fx_r_s

        Fy_r_s.name = 'LateralRearContactForce_Slip'
        Fy_r_s.units = 'newton'
        self.taskSignals[Fy_r_s.name] = Fy_r_s

        Fx_f_s.name = 'LongitudinalFrontContactForce_Slip'
        Fx_f_s.units = 'newton'
        self.taskSignals[Fx_f_s.name] = Fx_f_s

        Fy_f_s.name = 'LateralFrontContactForce_Slip'
        Fy_f_s.units = 'newton'
        self.taskSignals[Fy_f_s.name] = Fy_f_s

    def compute_contact_forces_nonslip(self):
        """Calculate the contact forces of rear and front wheels with respect 
        to inertial frame under the nonlip condition."""

        #parameters
        bp = self.bicycleRiderParameters
        mp = benchmark_to_moore(self.bicycleRiderParameters)

        l1 = mp['l1']
        l2 = mp['l2']
        mc = mp['mc']

        q1 = self.taskSignals['YawAngle']
        q2 = self.taskSignals['RollAngle']
        q4 = self.taskSignals['SteerAngle']

        u1 = self.taskSignals['YawRate']
        u2 = self.taskSignals['RollRate']
        u3 = self.taskSignals['PitchRate']
        u4 = self.taskSignals['SteerRate']
        u5 = self.taskSignals['RearWheelRate']
        u6 = self.taskSignals['FrontWheelRate']

        u1d = u1.time_derivative()
        u2d = u2.time_derivative()
        u3d = u3.time_derivative()
        u4d = u4.time_derivative()
        u5d = u5.time_derivative()
        u6d = u6.time_derivative()

        f = np.vectorize(contact_forces_nonslip)

        Fx_r_ns, Fy_r_ns, Fx_f_ns, Fy_f_ns = f(l1, l2, mc, q1, q2, q4, 
                    u1, u2, u3, u4, u5, u6, u1d, u2d, u3d, u4d, u5d, u6d)

        Fx_r_ns.name = 'LongitudinalRearContactForce_Nonslip'
        Fx_r_ns.units = 'newton'
        self.taskSignals[Fx_r_ns.name] = Fx_r_ns

        Fy_r_ns.name = 'LateralRearContactForce_Nonslip'
        Fy_r_ns.units = 'newton'
        self.taskSignals[Fy_r_ns.name] = Fy_r_ns

        Fx_f_ns.name = 'LongitudinalFrontContactForce_Nonslip'
        Fx_f_ns.units = 'newton'
        self.taskSignals[Fx_f_ns.name] = Fx_f_ns

        Fy_f_ns.name = 'LateralFrontContactForce_Nonslip'
        Fy_f_ns.units = 'newton'
        self.taskSignals[Fy_f_ns.name] = Fy_f_ns

    def compute_contact_points_acceleration(self):
        """Calculates the acceleration of the contact points of front and rear
        wheels."""

        AccX= self.taskSignals['FrameAccelerationX']
        AccY = self.taskSignals['FrameAccelerationY']
        AccZ = self.taskSignals['FrameAccelerationZ']

        q1 = self.taskSignals['YawAngle']
        q2 = self.taskSignals['RollAngle']
        q4 = self.taskSignals['SteerAngle']

        u1 = self.taskSignals['YawRate']
        u2 = self.taskSignals['RollRate']
        u3 = self.taskSignals['PitchRate']
        u4 = self.taskSignals['SteerRate']
        u5 = self.taskSignals['RearWheelRate']
        u6 = self.taskSignals['FrontWheelRate']

        u1d = u1.time_derivative()
        u2d = u2.time_derivative()
        u3d = u3.time_derivative()
        u4d = u4.time_derivative()
        u5d = u5.time_derivative()
        u6d = u6.time_derivative()

        bp = self.bicycleRiderParameters
        mp = benchmark_to_moore(self.bicycleRiderParameters)

        d1 = mp['d1']
        d2 = mp['d2']
        d3 = mp['d3']
        rr = mp['rr']
        rf = mp['rf']
        ds1 = self.bicycle.parameters['Measured']['ds1']
        ds3 = self.bicycle.parameters['Measured']['ds3']
        s3 = ds3
        s1 = ds1 + mp['l4']

        f = np.vectorize(contact_points_acceleration)

        u7d, u8d, u11d, u9d, u10d, u12d = f(AccX, AccY, AccZ, 
                        q1, q2, q4, 
                        u1, u2, u3, u4, u5, u6, u1d, u2d, u3d, u4d, u5d, u6d, 
                        d1, d2, d3, rr, rf, s1, s3)

        u7d.name = 'LongitudinalRearContactAcceleration'
        u7d.units = 'meter/second/second'
        u8d.name = 'LateralRearContactAcceleration'
        u8d.units = 'meter/second/second'
        u11d.name = 'DownwardRearContactAcceleration'
        u11d.units = 'meter/second/second'
        u9d.name = 'LongitudinalFrontContactAcceleration'
        u9d.units = 'meter/second/second'
        u10d.name = 'LateralFrontContactAcceleration'
        u10d.units = 'meter/second/second'
        u12d.name = 'DownwardFrontContactAcceleration'
        u12d.units = 'meter/second/second'

        self.taskSignals[u7d.name] = u7d
        self.taskSignals[u8d.name] = u8d
        self.taskSignals[u11d.name] = u11d
        self.taskSignals[u9d.name] = u9d
        self.taskSignals[u10d.name] = u10d
        self.taskSignals[u12d.name] = u12d

    def compute_rear_wheel_contact_rates(self):
        """Calculates the rates of the wheel contact points in the ground
        plane."""

        try:
            yawAngle = self.taskSignals['YawAngle']
            rearWheelRate = self.taskSignals['RearWheelRate']
            rR = self.bicycleRiderParameters['rR'] # this should be in meters
        except AttributeError:
            print('Either the yaw angle, rear wheel rate or ' +
                  'front wheel radius is not available. The ' +
                  'contact rates were not computed.')
        else:
            yawAngle = yawAngle.convert_units('radian')
            rearWheelRate = rearWheelRate.convert_units('radian/second')

            lon, lat = sigpro.rear_wheel_contact_rate(rR, rearWheelRate, yawAngle)

            lon.name = 'LongitudinalRearContactRate'
            lon.units = 'meter/second'
            self.taskSignals[lon.name] = lon

            lat.name = 'LateralRearContactRate'
            lat.units = 'meter/second'
            self.taskSignals[lat.name] = lat

    def compute_yaw_angle(self):
        """Computes the yaw angle by integrating the yaw rate."""

        # get the yaw rate
        try:
            yawRate = self.taskSignals['YawRate']
        except AttributeError:
            print('YawRate is not available. The YawAngle was not computed.')
        else:
            # convert to radians per second
            yawRate = yawRate.convert_units('radian/second')
            # integrate and try to account for the drift
            yawAngle = yawRate.integrate(detrend=True)
            # set the new name and units
            yawAngle.name = 'YawAngle'
            yawAngle.units = 'radian'
            # store in computed signals
            self.taskSignals['YawAngle'] = yawAngle

    def compute_steer_torque(self, plot=False):
        """Computes the rider applied steer torque.

        Parameters
        ----------
        plot : boolean, optional
            Default is False, but if True a plot is generated.

        """
        # steer torque
        frameAngRate = np.vstack((
            self.truncatedSignals['AngularRateX'],
            self.truncatedSignals['AngularRateY'],
            self.truncatedSignals['AngularRateZ']))
        frameAngAccel = np.vstack((
            self.truncatedSignals['AngularRateX'].time_derivative(),
            self.truncatedSignals['AngularRateY'].time_derivative(),
            self.truncatedSignals['AngularRateZ'].time_derivative()))
        frameAccel = np.vstack((
            self.truncatedSignals['AccelerationX'],
            self.truncatedSignals['AccelerationY'],
            self.truncatedSignals['AccelerationZ']))
        handlebarAngRate = self.truncatedSignals['ForkRate']
        handlebarAngAccel = self.truncatedSignals['ForkRate'].time_derivative()
        steerAngle = self.truncatedSignals['SteerAngle']
        steerColumnTorque =\
            self.truncatedSignals['SteerTubeTorque'].convert_units('newton*meter')
        handlebarMass = self.bicycleRiderParameters['mG']
        handlebarInertia =\
            self.bicycle.steer_assembly_moment_of_inertia(fork=False,
                wheel=False, nominal=True)
        # this is the distance from the handlebar center of mass to the
        # steer axis
        w = self.bicycleRiderParameters['w']
        c = self.bicycleRiderParameters['c']
        lam = self.bicycleRiderParameters['lam']
        xG = self.bicycleRiderParameters['xG']
        zG = self.bicycleRiderParameters['zG']
        handlebarCoM = np.array([xG, 0., zG])
        d = bp.geometry.distance_to_steer_axis(w, c, lam, handlebarCoM)
        # these are the distances from the point on the steer axis which is
        # aligned with the handlebar center of mass to the accelerometer on
        # the frame
        ds1 = self.bicycle.parameters['Measured']['ds1']
        ds3 = self.bicycle.parameters['Measured']['ds3']
        ds = np.array([ds1, 0., ds3]) # i measured these
        # damping and friction values come from Peter's work, I need to verify
        # them still
        damping = 0.3475
        friction = 0.0861

        components = sigpro.steer_torque_components(
            frameAngRate, frameAngAccel, frameAccel, handlebarAngRate,
            handlebarAngAccel, steerAngle, steerColumnTorque,
            handlebarMass, handlebarInertia, damping, friction, d, ds)
        steerTorque = sigpro.steer_torque(components)

        stDict = {'units':'newton*meter',
                  'name':'SteerTorque',
                  'runid':self.metadata['RunID'],
                  'sampleRate':steerAngle.sampleRate,
                  'source':'NA'}
        self.computedSignals['SteerTorque'] = Signal(steerTorque, stDict)

        if plot is True:

            time = steerAngle.time()

            hdot = (components['Hdot1'] + components['Hdot2'] +
                components['Hdot3'] + components['Hdot4'])
            cross = (components['cross1'] + components['cross2'] +
                components['cross3'])

            fig = plt.figure()

            frictionAx = fig.add_subplot(4, 1, 1)
            frictionAx.plot(time, components['viscous'],
                            time, components['coulomb'],
                            time, components['viscous'] + components['coulomb'])
            frictionAx.set_ylabel('Torque [N-m]')
            frictionAx.legend(('Viscous Friction', 'Coulomb Friction',
                'Total Friction'))

            dynamicAx = fig.add_subplot(4, 1, 2)
            dynamicAx.plot(time, hdot, time, cross, time, hdot + cross)
            dynamicAx.set_ylabel('Torque [N-m]')
            dynamicAx.legend((r'Torque due to $\dot{H}$',
                              r'Torque due to $r \times m a$',
                              r'Total Dynamic Torque'))

            additionalAx = fig.add_subplot(4, 1, 3)
            additionalAx.plot(time, hdot + cross + components['viscous'] +
                    components['coulomb'],
                    label='Total Frictional and Dynamic Torque')
            additionalAx.set_ylabel('Torque [N-m]')
            additionalAx.legend()

            torqueAx = fig.add_subplot(4, 1, 4)
            torqueAx.plot(time, components['steerColumn'],
                time, hdot + cross + components['viscous'] + components['coulomb'],
                time, steerTorque)
            torqueAx.set_xlabel('Time [s]')
            torqueAx.set_ylabel('Torque [N-m]')
            torqueAx.legend(('Measured Torque', 'Frictional and Dynamic Torque',
                'Rider Applied Torque'))

            plt.show()

            return fig

    def compute_yaw_roll_pitch_rates(self):
        """Computes the yaw, roll and pitch rates of the bicycle frame."""

        try:
            omegaX = self.truncatedSignals['AngularRateX']
            omegaY = self.truncatedSignals['AngularRateY']
            omegaZ = self.truncatedSignals['AngularRateZ']
            rollAngle = self.truncatedSignals['RollAngle']
            lam = self.bicycleRiderParameters['lam']
        except AttributeError:
            print('All needed signals are not available. ' +
                  'Yaw, roll and pitch rates were not computed.')
        else:
            omegaX = omegaX.convert_units('radian/second')
            omegaY = omegaY.convert_units('radian/second')
            omegaZ = omegaZ.convert_units('radian/second')
            rollAngle = rollAngle.convert_units('radian')

            yr, rr, pr = sigpro.yaw_roll_pitch_rate(omegaX, omegaY, omegaZ, lam,
                                            rollAngle=rollAngle)
            yr.units = 'radian/second'
            yr.name = 'YawRate'
            rr.units = 'radian/second'
            rr.name = 'RollRate'
            pr.units = 'radian/second'
            pr.name = 'PitchRate'

            self.computedSignals['YawRate'] = yr
            self.computedSignals['RollRate'] = rr
            self.computedSignals['PitchRate'] = pr

    def compute_steer_rate(self):
        """Calculate the steer rate from the frame and fork rates."""
        try:
            forkRate = self.truncatedSignals['ForkRate']
            omegaZ = self.truncatedSignals['AngularRateZ']
        except AttributeError:
            print('ForkRate or AngularRateZ is not available. ' +
                  'SteerRate was not computed.')
        else:
            forkRate = forkRate.convert_units('radian/second')
            omegaZ = omegaZ.convert_units('radian/second')

            steerRate = sigpro.steer_rate(forkRate, omegaZ)
            steerRate.units = 'radian/second'
            steerRate.name = 'SteerRate'
            self.computedSignals['SteerRate'] = steerRate

    def compute_forward_speed(self):
        """Calculates the magnitude of the main component of velocity of the
        center of the rear wheel."""

        try:
            rR = self.bicycleRiderParameters['rR']
            rearWheelRate = self.truncatedSignals['RearWheelRate']
        except AttributeError:
            print('rR or RearWheelRate is not availabe. ' +
                  'ForwardSpeed was not computed.')
        else:
            rearWheelRate = rearWheelRate.convert_units('radian/second')

            self.computedSignals['ForwardSpeed'] = -rR * rearWheelRate
            self.computedSignals['ForwardSpeed'].units = 'meter/second'
            self.computedSignals['ForwardSpeed'].name = 'ForwardSpeed'

    def compute_pull_force(self):
        """
        Computes the pull force from the truncated pull force signal.

        """
        try:
            pullForce = self.truncatedSignals['PullForce']
        except AttributeError:
            print 'PullForce was not available. PullForce was not computed.'
        else:
            pullForce = pullForce.convert_units('newton')
            pullForce.name = 'PullForce'
            pullForce.units = 'newton'
            self.computedSignals[pullForce.name] = pullForce

    def compute_frame_acceleration(self):
        """
        Computes the frame acceleration in inertial frame about body-fixed
        coordinates, taken from the NV-100signal.

        """
        try:
            AccX = self.truncatedSignals['AccelerationX']
            AccY = self.truncatedSignals['AccelerationY']
            AccZ = self.truncatedSignals['AccelerationZ']

        except AttributeError:
            print 'Accelerations from truncatedSignals were not available,'\
                ' Accelerations for computedSignals are not computed'
        else:
            AccX = AccX.convert_units('meter/second/second')
            AccX.name = 'FrameAccelerationX'
            AccX.units = 'meter/second/second'
            self.computedSignals[AccX.name] = AccX

            AccY = AccY.convert_units('meter/second/second')
            AccY.name = 'FrameAccelerationY'
            AccY.units = 'meter/second/second'
            self.computedSignals[AccY.name] = AccY

            AccZ = AccZ.convert_units('meter/second/second')
            AccZ.name = 'FrameAccelerationZ'
            AccZ.units = 'meter/second/second'
            self.computedSignals[AccZ.name] = AccZ

    def __str__(self):
        '''Prints basic run information to the screen.'''

        line = "=" * 79
        info = 'Run # {0}\nEnvironment: {1}\nRider: {2}\nBicycle: {3}\nSpeed:'\
            '{4}\nManeuver: {5}\nNotes: {6}'.format(
            self.metadata['RunID'],
            self.metadata['Environment'],
            self.metadata['Rider'],
            self.metadata['Bicycle'],
            self.metadata['Speed'],
            self.metadata['Maneuver'],
            self.metadata['Notes'])

        return line + '\n' + info + '\n' + line

    def export(self, filetype, directory='exports'):
        """
        Exports the computed signals to a file.

        Parameters
        ----------
        filetype : str
            The type of file to export the data to. Options are 'mat', 'csv',
            and 'pickle'.

        """

        if filetype == 'mat':
            if not os.path.exists(directory):
                print "Creating {0}".format(directory)
                os.makedirs(directory)
            exportData = {}
            exportData.update(self.metadata)
            try:
                exportData.update(self.taskSignals)
            except AttributeError:
                try:
                    exportData.update(self.truncatedSignals)
                except AttributeError:
                    exportData.update(self.calibratedSignals)
                    print('Exported calibratedSignals to {}'.format(directory))
                else:
                    print('Exported truncatedSignals to {}'.format(directory))
            else:
                print('Exported taskSignals to {}'.format(directory))

            filename = pad_with_zeros(str(self.metadata['RunID']), 5) + '.mat'
            io.savemat(os.path.join(directory, filename), exportData)
        else:
            raise NotImplementedError(('{0} method is not available' +
                                      ' yet.').format(filetype))

    def extract_task(self):
        """Slices the computed signals such that data before the end of the
        bump is removed and unusable trailng data is removed.

        """
        # get the z acceleration from the VN-100
        acc = -self.truncatedSignals['AccelerometerAccelerationY'].filter(30.)
        # find the mean speed during the task (look at one second in the middle
        # of the data)
        speed = self.computedSignals['ForwardSpeed']
        meanSpeed = speed[len(speed) / 2 - 100:len(speed) / 2 + 100].mean()
        wheelbase = self.bicycleRiderParameters['w']
        # find the bump
        indices = sigpro.find_bump(acc, acc.sampleRate, meanSpeed, wheelbase,
                self.bumpLength)


        # if it is a pavilion run, then clip the end too
        # these are the runs that the length of track method of clipping
        # applies to
        straight = ['Track Straight Line With Disturbance',
                    'Balance With Disturbance',
                    'Balance',
                    'Track Straight Line']
        if (self.metadata['Environment'] == 'Pavillion Floor' and
            self.metadata['Maneuver'] in straight):

            # this is based on the length of the track in the pavilion that we
            # measured on September 21st, 2011
            trackLength = 32. - wheelbase - self.bumpLength
            end = trackLength / meanSpeed * acc.sampleRate

            # i may need to clip the end based on the forward speed dropping
            # below certain threshold around the mean
        else:
            # if it isn't a pavilion run, don't clip the end
            end = -1

        self.taskSignals = {}
        for name, sig in self.computedSignals.items():
            self.taskSignals[name] = sig[indices[2]:end]

    def load_rider(self, pathToParameterData):
        """Creates a bicycle/rider attribute which contains the physical
        parameters for the bicycle and rider for this run."""

        print("Loading the bicycle and rider data for " +
              "{} on {}".format(self.metadata['Rider'],
              self.metadata['Bicycle']))

        # currently this isn't very generic, it only assumes that there was
        # Luke, Jason, and Charlie riding on the instrumented bicycle.
        rider = self.metadata['Rider']
        if rider == 'Charlie' or rider == 'Luke':
            # Charlie and Luke rode the bike in the same configuration
            bicycle = 'Rigidcl'
        elif rider == 'Jason' :
            bicycle = 'Rigid'
        else:
            raise StandardError('There are no bicycle parameters ' +
                    'for {}'.format(rider))

        # force a recalculation (but not the period calcs, they take too long)
        self.bicycle = bp.Bicycle(bicycle, pathToData=pathToParameterData)
        try:
            self.bicycle.extras
        except AttributeError:
            pass
        else:
            self.bicycle.save_parameters()
        # force a recalculation of the human parameters
        self.bicycle.add_rider(rider)
        if self.bicycle.human is not None:
            self.bicycle.save_parameters()

        self.bicycleRiderParameters =\
            bp.io.remove_uncertainties(self.bicycle.parameters['Benchmark'])

    def plot(self, *args, **kwargs):
        '''
        Returns a plot of the time series of various signals.

        Parameters
        ----------
        signalName : string
            These should be strings that correspond to the signals available in
            the computed data. If the first character of the string is `-` then
            the negative signal will be plotted. You can also scale the values
            so by adding a value and an ``*`` such as: ``'-10*RollRate'. The
            negative sign always has to come first.
        signalType : string, optional
            This allows you to plot from the other signal types. Options are
            'task', 'computed', 'truncated', 'calibrated', 'raw'. The default
            is 'task'.

        '''
        if not kwargs:
            kwargs = {'signalType': 'task'}

        mapping = {}
        for x in ['computed', 'truncated', 'calibrated', 'raw', 'task']:
            try:
                mapping[x] = getattr(self, x + 'Signals')
            except AttributeError:
                pass

        fig = plt.figure()
        ax = fig.add_axes([0.125, 0.125, 0.8, 0.7])

        leg = []
        for i, arg in enumerate(args):
            legName = arg
            sign = 1.
            # if a negative sign is present
            if '-' in arg and arg[0] != '-':
                raise ValueError('{} is incorrectly typed'.format(arg))
            elif '-' in arg and arg[0] == '-':
                arg = arg[1:]
                sign = -1.
            # if a multiplication factor is present
            if '*' in arg:
                mul, arg = arg.split('*')
            else:
                mul = 1.
            signal = sign * float(mul) * mapping[kwargs['signalType']][arg]
            ax.plot(signal.time(), signal)
            leg.append(legName + ' [' + mapping[kwargs['signalType']][arg].units + ']')

        ax.legend(leg)
        runid = pad_with_zeros(str(self.metadata['RunID']), 5)
        ax.set_title('Run: ' + runid + ', Rider: ' + self.metadata['Rider'] +
                  ', Speed: ' + str(self.metadata['Speed']) + 'm/s' + '\n' +
                  'Maneuver: ' + self.metadata['Maneuver'] +
                  ', Environment: ' + self.metadata['Environment'] + '\n' +
                  'Notes: ' + self.metadata['Notes'])

        ax.set_xlabel('Time [second]')

        ax.grid()

        return fig

    def plot_wheel_contact(self, show=False):
        """Returns a plot of the wheel contact traces.

        Parameters
        ----------
        show : boolean
            If true the plot will be displayed.

        Returns
        -------
        fig : matplotlib.Figure

        """

        q1 = self.taskSignals['LongitudinalRearContact']
        q2 = self.taskSignals['LateralRearContact']
        q9 = self.taskSignals['LongitudinalFrontContact']
        q10 = self.taskSignals['LateralFrontContact']

        fig = plt.figure()
        ax = fig.add_subplot(1, 1, 1)
        ax.plot(q1, q2, q9, q10)
        ax.set_xlabel('Distance [' + q1.units + ']')
        ax.set_ylabel('Distance [' + q2.units + ']')
        ax.set_ylim((-0.5, 0.5))
        rider = self.metadata['Rider']
        where = self.metadata['Environment']
        speed = '%1.2f' % self.taskSignals['ForwardSpeed'].mean()
        maneuver = self.metadata['Maneuver']
        ax.set_title(rider + ', ' + where + ', ' + maneuver + ' @ ' + speed + ' m/s')

        if show is True:
            fig.show()

        return fig

    def verify_time_sync(self, show=True, saveDir=None):
        """Shows a plot of the acceleration signals that were used to
        synchronize the NI and VN data. If it doesn't show a good fit, then
        something is wrong.

        Parameters
        ----------
        show : boolean
            If true, the figure will be displayed.
        saveDir : str
            The path to a directory in which to save the figure.

        """

        if self.topSig == 'calibrated':
            sigType = 'calibrated'
        else:
            sigType = 'truncated'

        fig = self.plot('-AccelerometerAccelerationY', 'AccelerationZ',
                signalType=sigType)
        ax = fig.axes[0]
        ax.set_xlim((0, 10))
        title = ax.get_title()
        ax.set_title(title + '\nSignal Type: ' + sigType)
        if saveDir is not None:
            if not os.path.exists(saveDir):
                print "Creating {0}".format(saveDir)
                os.makedirs(saveDir)
            runid = run_id_string(self.metadata['RunID'])
            fig.savefig(os.path.join(saveDir, runid + '.png'))
        if show is True:
            fig.show()

        return fig

    def video(self):
        '''
        Plays the video of the run.

        '''
        # get the 5 digit string version of the run id
        runid = pad_with_zeros(str(self.metadata['RunID']), 5)
        viddir = os.path.join('..', 'Video')
        abspath = os.path.abspath(viddir)
        # check to see if there is a video for this run
        if (runid + '.mp4') in os.listdir(viddir):
            path = os.path.join(abspath, runid + '.mp4')
            os.system('vlc "' + path + '"')
        else:
            print "No video for this run"

def matlab_date_to_object(matDate):
    '''Returns a date time object based on a Matlab `datestr()` output.

    Parameters
    ----------
    matDate : string
        String in the form '21-Mar-2011 14:45:54'.

    Returns
    -------
    python datetime object

    '''
    return datetime.datetime.strptime(matDate, '%d-%b-%Y %H:%M:%S')
