from __future__ import absolute_import, division, print_function
import numpy as np
from radiotools import helper as hp
from radiotools import coordinatesystems as cstrans
from NuRadioMC.SignalGen import askaryan as signalgen
from NuRadioReco.utilities import units
from NuRadioMC.utilities import medium
from NuRadioReco.utilities import fft
from NuRadioMC.utilities.earth_attenuation import get_weight
from NuRadioMC.SignalProp import propagation
import h5py
import time
import six
import copy
from scipy import constants
# import detector simulation modules
import NuRadioReco.modules.io.eventWriter
import NuRadioReco.modules.channelSignalReconstructor
import NuRadioReco.modules.custom.deltaT.calculateAmplitudePerRaySolution
import NuRadioReco.modules.electricFieldResampler
import NuRadioReco.modules.channelGenericNoiseAdder
import NuRadioReco.modules.efieldToVoltageConverterPerEfield
import NuRadioReco.modules.efieldToVoltageConverter
import NuRadioReco.modules.channelAddCableDelay
import NuRadioReco.modules.channelResampler
import NuRadioReco.detector.detector as detector
import NuRadioReco.detector.generic_detector as gdetector
import NuRadioReco.framework.sim_station
import NuRadioReco.framework.electric_field
from NuRadioReco.utilities import geometryUtilities as geo_utl
from NuRadioReco.framework.parameters import stationParameters as stnp
from NuRadioReco.framework.parameters import channelParameters as chp
from NuRadioReco.framework.parameters import electricFieldParameters as efp
from NuRadioReco.framework.parameters import eventParameters as evp
from NuRadioReco.framework.parameters import showerParameters as shp
import datetime
import logging
from six import iteritems
import yaml
import os
import collections
# import confuse
logger = logging.getLogger("sim")


def pretty_time_delta(seconds):
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days > 0:
        return '%dd%dh%dm%ds' % (days, hours, minutes, seconds)
    elif hours > 0:
        return '%dh%dm%ds' % (hours, minutes, seconds)
    elif minutes > 0:
        return '%dm%ds' % (minutes, seconds)
    else:
        return '%ds' % (seconds,)


def merge_config(user, default):
    if isinstance(user, dict) and isinstance(default, dict):
        for k, v in iteritems(default):
            if k not in user:
                user[k] = v
            else:
                user[k] = merge_config(user[k], v)
    return user


def get_distance_cut(shower_energy, intercept, slope):
    """
    This function returns a distance cut as a function of shower energy for
    speeding up the code. The cut is a linear function of the shower energy
    logarithm:

    log10(distance_cut/m) = intercept + slope * log10(shower_energy/eV)

    Parameters
    ----------
    shower_energy: float
        Shower energy
    intercept: float
        Intercept for the linear cut
    slope: float
        Slope for the linear cut

    Returns
    -------
    distance_cut: float
        Maximum distance for ray tracing
    """

    log_distance_cut = intercept + slope * np.log10(shower_energy / units.eV)
    distance_cut = 10 ** log_distance_cut * units.m

    return distance_cut


class simulation():

#
    def __init__(self, inputfilename,
                 outputfilename,
                 detectorfile,
                 outputfilenameNuRadioReco=None,
                 debug=False,
                 write_mode='full',
                 evt_time=datetime.datetime(2018, 1, 1),
                 config_file=None,
                 log_level=logging.WARNING,
                 default_detector_station=None,
                 default_detector_channel=None,
                 file_overwrite=False,
                 write_detector=True,
                 event_list=None,
                 log_level_propagation=logging.WARNING):
        """
        initialize the NuRadioMC end-to-end simulation

        Parameters
        ----------
        inputfilename: string
            the path to the hdf5 file containing the list of neutrino events
        outputfilename: string
            specify hdf5 output filename.
        detectorfile: string
            path to the json file containing the detector description
        station_id: int
            the station id for which the simulation is performed. Must match a station
            deself._fined in the detector description
        outputfilenameNuRadioReco: string or None
            outputfilename of NuRadioReco detector sim file, this file contains all
            waveforms of the triggered events
            default: None, i.e., no output file will be written which is useful for
            effective volume calculations
        debug: bool
            True activates debug mode, default False
        write_mode: str
            Detail level of eventWriter
            specifies the output mode:
            * 'full' (default): the full event content is written to disk
            * 'mini': only station traces are written to disc
            * 'micro': no traces are written to disc
        evt_time: datetime object
            the time of the events, default 1/1/2018
        config_file: string
            path to config file
        log_level: logging.LEVEL
            the log level
        default_detector_station: int or None
            if station parameters are not defined, the parameters of the default station are used
        default_detector_channel: int or None
            if channel parameters are not defined, the parameters of the default channel are used
        file_overwrite: bool
            True allows overwriting of existing files, default False
        write_detector: bool
            If true, the detector description is written into the .nur files along with the events
            default True
        event_list: None or list of ints
            if provided, only the event listed in this list are being simulated
        log_level_propagation: logging.LEVEL
            the log level of the propagation module
        """
        logger.setLevel(log_level)
        self._log_level_ray_propagation = log_level_propagation
        config_file_default = os.path.join(os.path.dirname(__file__), 'config_default.yaml')
        logger.warning('reading default config from {}'.format(config_file_default))
        with open(config_file_default, 'r') as ymlfile:
            self._cfg = yaml.load(ymlfile, Loader=yaml.FullLoader)
        if(config_file is not None):
            logger.warning('reading local config overrides from {}'.format(config_file))
            with open(config_file, 'r') as ymlfile:
                local_config = yaml.load(ymlfile, Loader=yaml.FullLoader)
                new_cfg = merge_config(local_config, self._cfg)
                self._cfg = new_cfg

        if(self._cfg['seed'] is None):
            # the config seeting None means a random seed. To have the simulation be reproducable, we generate a new
            # random seed once and save this seed to the config setting. If the simulation is rerun, we can get
            # the same random sequence.
            self._cfg['seed'] = np.random.randint(0, 2 ** 32 - 1)

        self._inputfilename = inputfilename
        self._outputfilename = outputfilename
        if(os.path.exists(self._outputfilename)):
            msg = f"hdf5 output file {self._outputfilename} already exists"
            if file_overwrite == False:
                logger.error(msg)
                raise FileExistsError(msg)
            else:
                logger.warning(msg)
        self._detectorfile = detectorfile
        self._n_reflections = int(self._cfg['propagation']['n_reflections'])
        self._outputfilenameNuRadioReco = outputfilenameNuRadioReco
        self._debug = debug
        self._evt_time = evt_time
        self.__write_detector = write_detector
        logger.warning("setting event time to {}".format(evt_time))
        self._event_group_list = event_list

        # initialize propagation module
        self._prop = propagation.get_propagation_module(self._cfg['propagation']['module'])

        self._ice = medium.get_ice_model(self._cfg['propagation']['ice_model'])

        self._mout = collections.OrderedDict()
        self._mout_groups = collections.OrderedDict()
        self._mout_attrs = collections.OrderedDict()

        # read in detector positions
        logger.warning("Detectorfile {}".format(os.path.abspath(self._detectorfile)))
        self._det = None
        if(default_detector_station):
            logger.warning(f"Default detector station provided (station {default_detector_station}) -> Using generic detector")
            self._det = gdetector.GenericDetector(json_filename=self._detectorfile, default_station=default_detector_station,
                                                 default_channel=default_detector_channel, antenna_by_depth=False)
        else:
            self._det = detector.Detector(json_filename=self._detectorfile, antenna_by_depth=False)
        self._det.update(evt_time)

        self._station_ids = self._det.get_station_ids()
        self._event_ids_counter = {}
        for station_id in self._station_ids:
            self._event_ids_counter[station_id] = -1  # we initialize with -1 becaue we increment the counter before we use it the first time

        # print noise information
        logger.warning("running with noise {}".format(bool(self._cfg['noise'])))
        logger.warning("setting signal to zero {}".format(bool(self._cfg['signal']['zerosignal'])))
        if(bool(self._cfg['propagation']['focusing'])):
            logger.warning("simulating signal amplification due to focusing of ray paths in the firn.")

        # read sampling rate from config (this sampling rate will be used internally)
        self._dt = 1. / (self._cfg['sampling_rate'] * units.GHz)

        self._read_input_hdf5()  # we read in the full input file into memory at the beginning to limit io to the beginning and end of the run

        ################################
        # perfom a dummy detector simulation to determine how the signals are filtered
        self._bandwidth_per_channel = {}
        self._amplification_per_channel = {}
        self.__noise_adder_normalization = {}

        # first create dummy event and station with channels
        self._Vrms = 1
        for iSt, self._station_id in enumerate(self._station_ids):
            self._iSh = 0
            self._evt = NuRadioReco.framework.event.Event(0, self._iSh)
            # read all quantities from hdf5 file and store them in local variables
            self._read_input_neutrino_properties()

            self._sampling_rate_detector = self._det.get_sampling_frequency(self._station_id, 0)
#                 logger.warning('internal sampling rate is {:.3g}GHz, final detector sampling rate is {:.3g}GHz'.format(self.get_sampling_rate(), self._sampling_rate_detector))
            self._n_samples = self._det.get_number_of_samples(self._station_id, 0) / self._sampling_rate_detector / self._dt
            self._n_samples = int(np.ceil(self._n_samples / 2.) * 2)  # round to nearest even integer
            self._ff = np.fft.rfftfreq(self._n_samples, self._dt)
            self._tt = np.arange(0, self._n_samples * self._dt, self._dt)

            self._create_sim_station()
            for channel_id in range(self._det.get_number_of_channels(self._station_id)):
                electric_field = NuRadioReco.framework.electric_field.ElectricField([channel_id], self._det.get_relative_position(self._sim_station.get_id(), channel_id))
                trace = np.zeros_like(self._tt)
                trace[self._n_samples // 2] = 100 * units.V  # set a signal that will satifsy any high/low trigger
                trace[self._n_samples // 2 + 1] = -100 * units.V
                electric_field.set_trace(np.array([np.zeros_like(self._tt), trace, trace]), 1. / self._dt)
                electric_field.set_trace_start_time(0)
                electric_field[efp.azimuth] = 0
                electric_field[efp.zenith] = 100 * units.deg
                electric_field[efp.ray_path_type] = 0
                self._sim_station.add_electric_field(electric_field)

            self._station = NuRadioReco.framework.station.Station(self._station_id)
            self._station.set_sim_station(self._sim_station)
            self._station.set_station_time(self._evt_time)
            self._evt.set_station(self._station)

            self._detector_simulation_filter_amp(self._evt, self._station, self._det)
            self._bandwidth_per_channel[self._station_id] = {}
            self._amplification_per_channel[self._station_id] = {}
            self.__noise_adder_normalization[self._station_id] = {}
            for channel_id in range(self._det.get_number_of_channels(self._station_id)):
                ff = np.linspace(0, 0.5 / self._dt, 10000)
                filt = np.ones_like(ff, dtype=np.complex)
                noise_module_index = []
                for i, (name, instance, kwargs) in enumerate(self._evt.iter_modules(self._station_id)):

                    if(name in ['channelGenericNoiseAdder']):
                        noise_module_index.append(i)
                    if hasattr(instance, "get_filter"):
                        filt *= instance.get_filter(ff, self._station_id, channel_id, self._det, **kwargs)

                self._amplification_per_channel[self._station_id][channel_id] = np.abs(filt).max()
                bandwidth = np.trapz(np.abs(filt) ** 2, ff)
                self._bandwidth_per_channel[self._station_id][channel_id] = bandwidth
                logger.info(f"bandwidth of station {self._station_id} channel {channel_id} is {bandwidth/units.MHz:.1f}MHz")

                # in case noise is added, we need to determine what filters are applied after noise is added to
                # rescale the noise level accordingly
                if((not bool(self._cfg['noise'])) or len(noise_module_index) == 0):
                    logger.debug("no noise is added")
                else:
                    if(len(noise_module_index) > 1):
                        raise NotImplementedError("more than 1 noise importer module -> not supported")
                    else:
                        filt_noise = np.ones_like(ff, dtype=np.complex)
                        for i, (name, instance, kwargs) in enumerate(self._evt.iter_modules(self._station_id)):
                            if(i < noise_module_index[0]):  # skip all modules that come before the noise adder module
                                continue
                            if(hasattr(instance, "get_filter")):
                                filt_noise *= instance.get_filter(ff, self._station_id, channel_id, self._det, **kwargs)
                        norm = np.trapz(np.abs(filt_noise) ** 2, ff)
                        self.__noise_adder_normalization[self._station_id][channel_id] = norm
                        logger.info(f"noise normalization of station {self._station_id} channel {channel_id} is {norm/units.MHz:.1g}MHz")
        ################################

        # for now just assume that bandwidth is the same for all stations and channels
        self._bandwidth = next(iter(next(iter(self._bandwidth_per_channel.values())).values()))
        amplification = next(iter(next(iter(self._amplification_per_channel.values())).values()))
        Tnoise = self._cfg['trigger']['noise_temperature']
        Vrms = self._cfg['trigger']['Vrms']
        if(Tnoise is not None and Vrms is not None):
            raise AttributeError(f"Specifying noise temperature (set to {Tnoise}) and Vrms (set to {Vrms} is not allowed.")
        if(Tnoise is not None):
            self._Tnoise = float(Tnoise)
            self._Vrms = (self._Tnoise * 50 * constants.k *
                           self._bandwidth / units.Hz) ** 0.5  # from elog:1566 and https://en.wikipedia.org/wiki/Johnson%E2%80%93Nyquist_noise (last Eq. in "noise voltage and power" section
            logger.warning('noise temperature = {}, bandwidth = {:.2f} MHz -> Vrms = {:.2f} muV'.format(self._Tnoise, self._bandwidth / units.MHz, self._Vrms / units.V / units.micro))
        elif(Vrms is not None):
            self._Vrms = float(Vrms) * units.V
            self._Tnoise = None
        else:
            raise AttributeError(f"noise temperature and Vrms are both set to None")

        self._Vrms_efield = self._Vrms / amplification / units.m
        tmp_cut = float(self._cfg['speedup']['min_efield_amplitude'])
        logger.warning(f"final Vrms {self._Vrms/units.V:.2g}V corresponds to an efield of {self._Vrms_efield/units.V/units.m/units.micro:.2g} muV/m for a VEL = 1m (amplification factor of system is {amplification:.1f}).\n -> all signals with less then {tmp_cut:.1f} x Vrms_efield = {tmp_cut * self._Vrms_efield/units.m/units.V/units.micro:.2g}muV/m will be skipped")

    def run(self):
        """
        run the NuRadioMC simulation
        """
        logger.warning(f"Starting NuRadioMC simulation")

        self._channelSignalReconstructor = NuRadioReco.modules.channelSignalReconstructor.channelSignalReconstructor(log_level=logging.DEBUG)
        self._eventWriter = NuRadioReco.modules.io.eventWriter.eventWriter()
        efieldToVoltageConverterPerEfield = NuRadioReco.modules.efieldToVoltageConverterPerEfield.efieldToVoltageConverterPerEfield()
        efieldToVoltageConverter = NuRadioReco.modules.efieldToVoltageConverter.efieldToVoltageConverter()
        channelAddCableDelay = NuRadioReco.modules.channelAddCableDelay.channelAddCableDelay()
        channelGenericNoiseAdder = NuRadioReco.modules.channelGenericNoiseAdder.channelGenericNoiseAdder()
        channelResampler = NuRadioReco.modules.channelResampler.channelResampler()
        electricFieldResampler = NuRadioReco.modules.electricFieldResampler.electricFieldResampler()
        if(self._outputfilenameNuRadioReco is not None):
            self._eventWriter.begin(self._outputfilenameNuRadioReco)
        unique_event_group_ids = np.unique(self._fin['event_group_ids'])
        self._n_showers = len(self._fin['event_group_ids'])
        self._shower_ids = np.array(self._fin['shower_ids'])
        self._shower_index = {}  # this array allows to convert the shower id to an index that starts from 0 to be used to access the arrays in the hdf5 file.
        for shower_index, shower_id in enumerate(self._shower_ids):
            self._shower_index[shower_id] = shower_index

        self._create_meta_output_datastructures()

        # check if the same detector was simulated before (then we can save the ray tracing part)
        pre_simulated = self._check_if_was_pre_simulated()

        # Check if vertex_times exists:
        vertex_times_exists = self._check_vertex_times()

        input_time = 0.0
        askaryan_time = 0.
        rayTracingTime = 0.0
        detSimTime = 0.0
        outputTime = 0.0
        time_attenuation_length = 0.
        t_start = time.time()

        # loop over event groups
        for event_group_id in unique_event_group_ids:
            if(event_group_id != 122):
                continue
            logger.debug(f"simulating event group id {event_group_id}")
            if(self._event_group_list is not None and event_group_id not in self._event_group_list):
                logger.debug(f"skipping event group {event_group_id} because it is not in the event group list provided to the __init__ function")
                continue
            t1 = time.time()
            event_indices = np.atleast_1d(np.squeeze(np.argwhere(self._fin['event_group_ids'] == event_group_id)))

            # loop over all showers in event group and calculate weight
            # the weight calculation is independent of the station, so we do this calculation only once
            for self._iSh in event_indices:
                # read all quantities from hdf5 file and store them in local variables
                self._read_input_neutrino_properties()

                x1 = np.array([self._x, self._y, self._z])  # the interaction point
                # calculate weight
                # if we have a second interaction, the weight needs to be calculated from the initial neutrino
                if(self._n_interaction > 1):
                    iE_mother = np.argwhere(self._fin['event_group_ids'] == self._fin['event_group_ids'][self._iSh]).min()  # get index of mother neutrino
                    x_int_mother = np.array([self._fin['xx'][iE_mother], self._fin['yy'][iE_mother], self._fin['zz'][iE_mother]])
                    self._mout['weights'][self._iSh] = get_weight(self._fin['zeniths'][iE_mother],
                                                         self._fin['energies'][iE_mother],
                                                         self._fin['flavors'][iE_mother],
                                                         mode=self._cfg['weights']['weight_mode'],
                                                         cross_section_type=self._cfg['weights']['cross_section_type'],
                                                         vertex_position=x_int_mother,
                                                         phi_nu=self._fin['azimuths'][iE_mother])
                else:
                    self._mout['weights'][self._iSh] = get_weight(self._zenith_shower, self._energy, self._flavor,
                                                                 mode=self._cfg['weights']['weight_mode'],
                                                                 cross_section_type=self._cfg['weights']['cross_section_type'],
                                                                 vertex_position=x1,
                                                                 phi_nu=self._azimuth_shower)
            triggered_showers = {}  # this variable tracks which showers triggered a particular station
            # loop over all stations (each station is treated independently)
            for iSt, self._station_id in enumerate(self._station_ids):
                triggered_showers[self._station_id] = []
                logger.debug(f"simulating station {self._station_id}")
                candidate_station = False
                self._sampling_rate_detector = self._det.get_sampling_frequency(self._station_id, 0)
#                 logger.warning('internal sampling rate is {:.3g}GHz, final detector sampling rate is {:.3g}GHz'.format(self.get_sampling_rate(), self._sampling_rate_detector))
                self._n_samples = self._det.get_number_of_samples(self._station_id, 0) / self._sampling_rate_detector / self._dt
                self._n_samples = int(np.ceil(self._n_samples / 2.) * 2)  # round to nearest even integer
                self._ff = np.fft.rfftfreq(self._n_samples, self._dt)
                self._tt = np.arange(0, self._n_samples * self._dt, self._dt)

                sg = self._mout_groups[self._station_id]
                ray_tracing_performed = ('ray_tracing_C0' in sg) and (self._was_pre_simulated)

                self._evt_tmp = NuRadioReco.framework.event.Event(0, 0)
                self._create_sim_station()
                # loop over all showers in event group
                for self._iSh in event_indices:
                    if(self._iSh > 0 and self._iSh % max(1, int(self._n_showers / 100.)) == 0):
                        eta = pretty_time_delta((time.time() - t_start) * (self._n_showers - self._iSh) / self._iSh)
                        total_time = input_time + rayTracingTime + detSimTime + outputTime
                        if total_time > 0:
                            logger.warning("processing event {}/{} ({} triggered) = {:.1f}%, ETA {}, time consumption: ray tracing = {:.0f}% (att. length {:.0f}%), askaryan = {:.0f}%, detector simulation = {:.0f}% reading input = {:.0f}%".format(
                                self._iSh, self._n_showers, np.sum(self._mout['triggered']), 100. * self._iSh / self._n_showers,
                                eta, 100. * (rayTracingTime - askaryan_time) / total_time,
                                100. * time_attenuation_length / (rayTracingTime - askaryan_time),
                                100.* askaryan_time / total_time, 100. * detSimTime / total_time, 100.*input_time / total_time))

                    # read all quantities from hdf5 file and store them in local variables
                    self._read_input_neutrino_properties()
                    logger.debug(f"simulating shower {self._iSh}: {self._shower_type} with E = {self._shower_energy/units.eV:.2g}eV")
                    x1 = np.array([self._x, self._y, self._z])  # the interaction point

                    # skip vertices not in fiducial volume. This is required because 'mother' events are added to the event list
                    # if daugthers (e.g. tau decay) have their vertex in the fiducial volume
                    if not self._is_in_fiducial_volume():
                        logger.debug("event is not in fiducial volume, skipping simulation")
                        continue

                    # for special cases where only EM or HAD showers are simulated, skip all events that don't fulfill this criterion
                    if(self._cfg['signal']['shower_type'] == "em"):
                        if(self._shower_type != "em"):
                            continue
                    if(self._cfg['signal']['shower_type'] == "had"):
                        if(self._shower_type != "had"):
                            continue

                    # skip all events where neutrino weights is zero, i.e., do not
                    # simulate neutrino that propagate through the Earth
                    if(self._mout['weights'][self._iSh] < self._cfg['speedup']['minimum_weight_cut']):
                        logger.debug("neutrino weight is smaller than {}, skipping event".format(self._cfg['speedup']['minimum_weight_cut']))
                        continue

                    self._create_sim_shower()  # create sim shower
                    self._evt_tmp.add_sim_shower(self._sim_shower)

                    # generate unique and increasing event id per station
                    self._event_ids_counter[self._station_id] += 1
                    self._event_id = self._event_ids_counter[self._station_id]

                    # be careful, zenith/azimuth angle always refer to where the neutrino came from,
                    # i.e., opposite to the direction of propagation. We need the propagation direction here,
                    # so we multiply the shower axis with '-1'
                    self._shower_axis = -1 * hp.spherical_to_cartesian(self._zenith_shower, self._azimuth_shower)

                    # calculate correct chereknov angle for ice density at vertex position
                    n_index = self._ice.get_index_of_refraction(x1)
                    cherenkov_angle = np.arccos(1. / n_index)

                    # first step: perform raytracing to see if solution exists
                    t2 = time.time()
                    input_time += (time.time() - t1)

                    for channel_id in range(self._det.get_number_of_channels(self._station_id)):
                        logger.debug(f"simulationg channel {channel_id}")
                        x2 = self._det.get_relative_position(self._station_id, channel_id) + self._det.get_absolute_position(self._station_id)
                        r = self._prop(x1, x2, self._ice, self._cfg['propagation']['attenuation_model'], log_level=self._log_level_ray_propagation,
                                       n_frequencies_integration=int(self._cfg['propagation']['n_freq']),
                                       n_reflections=self._n_reflections)

                        if(pre_simulated and ray_tracing_performed and not self._cfg['speedup']['redo_raytracing']):  # check if raytracing was already performed
                            sg_pre = self._fin_stations["station_{:d}".format(self._station_id)]
                            temp_reflection = None
                            temp_reflection_case = None
                            if('ray_tracing_reflection' in sg_pre):  # for backward compatibility: Check if reflection layer information exists in data file
                                temp_reflection = sg_pre['ray_tracing_reflection'][self._iSh][channel_id]
                                temp_reflection_case = sg_pre['ray_tracing_reflection_case'][self._iSh][channel_id]
                            r.set_solution(sg_pre['ray_tracing_C0'][self._iSh][channel_id], sg_pre['ray_tracing_C1'][self._iSh][channel_id],
                                           sg_pre['ray_tracing_solution_type'][self._iSh][channel_id], temp_reflection, temp_reflection_case)
                        else:
                            r.find_solutions()

                        if(not r.has_solution()):
                            logger.debug("event {} and station {}, channel {} does not have any ray tracing solution ({} to {})".format(
                                self._event_group_id, self._station_id, channel_id, x1, x2))
                            continue
                        delta_Cs = []
                        viewing_angles = []
                        # loop through all ray tracing solution
                        for iS in range(r.get_number_of_solutions()):
                            sg['ray_tracing_C0'][self._iSh, channel_id, iS] = r.get_results()[iS]['C0']
                            sg['ray_tracing_C1'][self._iSh, channel_id, iS] = r.get_results()[iS]['C1']
                            sg['ray_tracing_reflection'][self._iSh, channel_id, iS] = r.get_results()[iS]['reflection']
                            sg['ray_tracing_reflection_case'][self._iSh, channel_id, iS] = r.get_results()[iS]['reflection_case']
                            sg['ray_tracing_solution_type'][self._iSh, channel_id, iS] = r.get_solution_type(iS)
                            self._launch_vector = r.get_launch_vector(iS)
                            sg['launch_vectors'][self._iSh, channel_id, iS] = self._launch_vector
                            # calculates angle between shower axis and launch vector
                            viewing_angle = hp.get_angle(self._shower_axis, self._launch_vector)
                            viewing_angles.append(viewing_angle)
                            delta_C = (viewing_angle - cherenkov_angle)
                            logger.debug('solution {} {}: viewing angle {:.1f} = delta_C = {:.1f}'.format(
                                iS, self._prop.solution_types[r.get_solution_type(iS)], viewing_angle / units.deg, (viewing_angle - cherenkov_angle) / units.deg))
                            delta_Cs.append(delta_C)

                        # discard event if delta_C (angle off cherenkov cone) is too large
                        if(min(np.abs(delta_Cs)) > self._cfg['speedup']['delta_C_cut']):
                            logger.debug('delta_C too large, event unlikely to be observed, skipping event')
                            continue

                        n = r.get_number_of_solutions()
                        for iS in range(n):  # loop through all ray tracing solution
                            # skip individual channels where the viewing angle difference is too large
                            # discard event if delta_C (angle off cherenkov cone) is too large
                            if(np.abs(delta_Cs[iS]) > self._cfg['speedup']['delta_C_cut']):
                                logger.debug('delta_C too large, ray tracing solution unlikely to be observed, skipping event')
                                continue
                            if(pre_simulated and ray_tracing_performed and not self._cfg['speedup']['redo_raytracing']):
                                sg_pre = self._fin_stations["station_{:d}".format(self._station_id)]
                                R = sg_pre['travel_distances'][self._iSh, channel_id, iS]
                                T = sg_pre['travel_times'][self._iSh, channel_id, iS]
                            else:
                                R = r.get_path_length(iS)  # calculate path length
                                T = r.get_travel_time(iS)  # calculate travel time
                                if (R == None or T == None):
                                    continue
                            sg['travel_distances'][self._iSh, channel_id, iS] = R
                            sg['travel_times'][self._iSh, channel_id, iS] = T
                            self._launch_vector = r.get_launch_vector(iS)
                            receive_vector = r.get_receive_vector(iS)
                            # save receive vector
                            sg['receive_vectors'][self._iSh, channel_id, iS] = receive_vector
                            zenith, azimuth = hp.cartesian_to_spherical(*receive_vector)

                            # get neutrino pulse from Askaryan module
                            t_ask = time.time()
                            kwargs = {}
                            # if the input file specifies a specific shower realization, use that realization
                            if(self._cfg['signal']['model'] in ["ARZ2019", "ARZ2020"] and "shower_realization_ARZ" in self._fin):
                                kwargs['iN'] = int(self._fin['shower_realization_ARZ'][self._iSh])
                                logger.debug(f"reusing shower {kwargs['iN']} ARZ shower library")
                            elif(self._cfg['signal']['model'] == "Alvarez2009" and "shower_realization_Alvarez2009" in self._fin):
                                kwargs['k_L'] = self._fin['shower_realization_Alvarez2009'][self._iSh]
                                logger.debug(f"reusing k_L parameter of Alvarez2009 model of k_L = {kwargs['k_L']:.4g}")
                            else:
                                # check if the shower was already simulated (e.g. for a different channel or ray tracing solution)
                                if(self._cfg['signal']['model'] in ["ARZ2019", "ARZ2020"]):
                                    if(self._sim_shower.has_parameter(shp.charge_excess_profile_id)):
                                        kwargs = {'iN': self._sim_shower.get_parameter(shp.charge_excess_profile_id)}
                                if(self._cfg['signal']['model'] == "Alvarez2009"):
                                    if(self._sim_shower.has_parameter(shp.k_L)):
                                        kwargs = {'k_L': self._sim_shower.get_parameter(shp.k_L)}
                                        logger.debug(f"reusing k_L parameter of Alvarez2009 model of k_L = {kwargs['k_L']:.4g}")

                            spectrum, additional_output = signalgen.get_frequency_spectrum(self._shower_energy, viewing_angles[iS],
                                            self._n_samples, self._dt, self._shower_type, n_index, R,
                                            self._cfg['signal']['model'], seed=self._cfg['seed'], full_output=True, **kwargs)
                            # save shower realization to SimShower and hdf5 file
                            if(self._cfg['signal']['model'] in ["ARZ2019", "ARZ2020"]):
                                if('shower_realization_ARZ' not in self._mout):
                                    self._mout['shower_realization_ARZ'] = np.zeros(self._n_showers, dtype=np.int)
                                if(not self._sim_shower.has_parameter(shp.charge_excess_profile_id)):
                                    self._sim_shower.set_parameter(shp.charge_excess_profile_id, additional_output['iN'])
                                    self._mout['shower_realization_ARZ'][self._iSh] = additional_output['iN']
                                    logger.debug(f"setting shower profile for ARZ shower library to i = {additional_output['iN']}")
                            if(self._cfg['signal']['model'] == "Alvarez2009"):
                                if('shower_realization_Alvarez2009' not in self._mout):
                                    self._mout['shower_realization_Alvarez2009'] = np.zeros(self._n_showers)
                                if(not self._sim_shower.has_parameter(shp.k_L)):
                                    self._sim_shower.set_parameter(shp.k_L, additional_output['k_L'])
                                    self._mout['shower_realization_Alvarez2009'][self._iSh] = additional_output['k_L']
                                    logger.debug(f"setting k_L parameter of Alvarez2009 model to k_L = {additional_output['k_L']:.4g}")
                            askaryan_time += (time.time() - t_ask)

                            # apply frequency dependent attenuation
                            t_att = time.time()
                            if self._cfg['propagation']['attenuate_ice']:
                                attn = r.get_attenuation(iS, self._ff, 0.5 * self._sampling_rate_detector)
                                spectrum *= attn
                            time_attenuation_length += (time.time() - t_att)

                            # apply the focusing effect
                            if self._cfg['propagation']['focusing']:
                                dZRec = -0.01 * units.m
                                focusing = r.get_focusing(iS, dZRec, float(self._cfg['propagation']['focusing_limit']))
                                sg['focusing_factor'][self._iSh, channel_id, iS] = focusing
                                logger.info(f"focusing: channel {channel_id:d}, solution {iS:d} -> {focusing:.1f}x")
                                # spectrum = fft.time2freq(fft.freq2time(spectrum) * focusing)
                                spectrum[1:] *= focusing

                            polarization_direction_onsky = self._calculate_polarization_vector()
                            cs_at_antenna = cstrans.cstrafo(*hp.cartesian_to_spherical(*receive_vector))
                            polarization_direction_at_antenna = cs_at_antenna.transform_from_onsky_to_ground(polarization_direction_onsky)
                            logger.debug('receive zenith {:.0f} azimuth {:.0f} polarization on sky {:.2f} {:.2f} {:.2f}, on ground @ antenna {:.2f} {:.2f} {:.2f}'.format(
                                zenith / units.deg, azimuth / units.deg, polarization_direction_onsky[0],
                                polarization_direction_onsky[1], polarization_direction_onsky[2],
                                *polarization_direction_at_antenna))
                            sg['polarization'][self._iSh, channel_id, iS] = polarization_direction_at_antenna
                            eR, eTheta, ePhi = np.outer(polarization_direction_onsky, spectrum)

                            # in case of a reflected ray we need to account for fresnel
                            # reflection at the surface
                            r_theta = None
                            r_phi = None
                            i_reflections = r.get_results()[iS]['reflection']
                            zenith_reflections = np.atleast_1d(r.get_reflection_angle(iS))  # lets handle the general case of multiple reflections off the surface (possible if also a reflective bottom layer exists)
                            n_surface_reflections = np.sum(zenith_reflections != None)
                            logger.debug(f"st {self._station_id}, ch {channel_id}, solutino {iS}: n_ref bottom = {i_reflections:d}," + \
                                         f" n_ref surface = {n_surface_reflections:d},  R = {R / units.m:.1f} m, T = {T / units.ns:.1f}ns," + \
                                         f" receive angles zen={zenith / units.deg:.0f}deg, az={azimuth / units.deg:.0f}deg")

                            if self._cfg['propagation']['attenuate_ice']:
                                tmp_output = "attenuation factor"
                                iF = len(self._ff) // 4
                                tmp_output += f" {self._ff[iF]/units.MHz:.0f} MHz: {attn[iF]:.2g}"
                                iF = len(self._ff) // 3
                                tmp_output += f" {self._ff[iF]/units.MHz:.0f} MHz: {attn[iF]:.2g}"
                                iF = len(self._ff) // 2
                                tmp_output += f" {self._ff[iF]/units.MHz:.0f} MHz: {attn[iF]:.2g}"
                                logger.debug(tmp_output)
                            for zenith_reflection in zenith_reflections:  # loop through all possible reflections
                                if(zenith_reflection is None):  # skip all ray segments where not reflection at surface happens
                                    continue
                                r_theta = geo_utl.get_fresnel_r_p(
                                    zenith_reflection, n_2=1., n_1=self._ice.get_index_of_refraction([x2[0], x2[1], -1 * units.cm]))
                                r_phi = geo_utl.get_fresnel_r_s(
                                    zenith_reflection, n_2=1., n_1=self._ice.get_index_of_refraction([x2[0], x2[1], -1 * units.cm]))

                                eTheta *= r_theta
                                ePhi *= r_phi
                                logger.debug("ray hits the surface at an angle {:.2f}deg -> reflection coefficient is r_theta = {:.2f}, r_phi = {:.2f}".format(zenith_reflection / units.deg,
                                    r_theta, r_phi))

                            if(i_reflections > 0):  # take into account possible bottom reflections
                                # each reflection lowers the amplitude by the reflection coefficient and introduces a phase shift
                                reflection_coefficient = self._ice.reflection_coefficient ** i_reflections
                                phase_shift = (i_reflections * self._ice.reflection_phase_shift) % (2 * np.pi)
                                # we assume that both efield components are equally affected
                                eTheta *= reflection_coefficient * np.exp(1j * phase_shift)
                                ePhi *= reflection_coefficient * np.exp(1j * phase_shift)
                                logger.debug(f"ray is reflecting {i_reflections:d} times at the bottom -> reducing the signal by a factor of {reflection_coefficient:.2f}")

                            if(self._debug):
                                from matplotlib import pyplot as plt
                                fig, (ax, ax2) = plt.subplots(1, 2)
                                ax.plot(self._ff, np.abs(eTheta) / units.micro / units.V * units.m)
                                ax2.plot(self._tt, fft.freq2time(eTheta, 1. / self._dt) / units.micro / units.V * units.m)
                                ax2.set_ylabel("amplitude [$\mu$V/m]")
                                fig.tight_layout()
                                fig.suptitle("$E_C$ = {:.1g}eV $\Delta \Omega$ = {:.1f}deg, R = {:.0f}m".format(
                                    self._shower_energy, viewing_angles[iS], R))
                                fig.subplots_adjust(top=0.9)
                                plt.show()

                            electric_field = NuRadioReco.framework.electric_field.ElectricField([channel_id],
                                                position=self._det.get_relative_position(self._sim_station.get_id(), channel_id),
                                                shower_id=self._shower_ids[self._iSh], ray_tracing_id=iS)
                            if(iS is None):
                                a = 1 / 0
                            electric_field.set_frequency_spectrum(np.array([eR, eTheta, ePhi]), 1. / self._dt)
                            # Trace start time is equal to the interaction time relative to the first
                            # interaction plus the wave travel time.
                            if hasattr(self, '_vertex_time'):
                                trace_start_time = self._vertex_time + T
                            else:
                                trace_start_time = T

                            # We shift the trace start time so that the trace time matches the propagation time.
                            # The centre of the trace corresponds to the instant when the signal from the shower
                            # vertex arrives at the observer. The next line makes sure that the centre time
                            # of the trace is equal to vertex_time + T (wave propagation time)
                            trace_start_time -= 0.5 * electric_field.get_number_of_samples() / electric_field.get_sampling_rate()

                            electric_field.set_trace_start_time(trace_start_time)
                            electric_field[efp.azimuth] = azimuth
                            electric_field[efp.zenith] = zenith
                            electric_field[efp.ray_path_type] = self._prop.solution_types[r.get_solution_type(iS)]
                            electric_field[efp.nu_vertex_distance] = sg['travel_distances'][self._iSh, channel_id, iS]
                            electric_field[efp.nu_viewing_angle] = viewing_angles[iS]
                            electric_field[efp.reflection_coefficient_theta] = r_theta
                            electric_field[efp.reflection_coefficient_phi] = r_phi
                            self._sim_station.add_electric_field(electric_field)

                            # apply a simple threshold cut to speed up the simulation,
                            # application of antenna response will just decrease the
                            # signal amplitude
                            if(np.max(np.abs(electric_field.get_trace())) > float(self._cfg['speedup']['min_efield_amplitude']) * self._Vrms_efield):
                                candidate_station = True
                        # end of ray tracing solutions loop
                    t3 = time.time()
                    rayTracingTime += t3 - t2
                    # end of channels loop
                # end of showers loop
                # now perform first part of detector simulation -> convert each efield to voltage
                # (i.e. apply antenna response) and apply additional simulation of signal chain (such as cable delays,
                # amp response etc.)
                if(not candidate_station):
                    logger.debug("electric field amplitude too small in all channels, skipping to next event")
                    continue
                self._station = NuRadioReco.framework.station.Station(self._station_id)
                self._station.set_sim_station(self._sim_station)

                # convert efields to voltages at digitizer
                if(hasattr(self, '_detector_simulation_part1')):
                    # we give the user the opportunity to define a custom detector simulation
                    self._detector_simulation_part1()
                else:
                    efieldToVoltageConverterPerEfield.run(self._evt, self._station, self._det)  # convolve efield with antenna pattern
                    self._detector_simulation_filter_amp(self._evt, self._station.get_sim_station(), self._det)
                    channelAddCableDelay.run(self._evt, self._sim_station, self._det)

                if(self._cfg['speedup']['amp_per_ray_solution']):
#                     self._calculate_amplitude_per_ray_tracing_solution()
                    if('max_amp_shower_and_ray' not in sg):
                        n_antennas = self._det.get_number_of_channels(self._station_id)
                        nS = 2 + 4 * self._n_reflections  # number of possible ray-tracing solutions
                        sg['max_amp_shower_and_ray'] = np.zeros((self._n_showers, n_antennas, nS))
                    if('time_shower_and_ray' not in sg):
                        n_antennas = self._det.get_number_of_channels(self._station_id)
                        nS = 2 + 4 * self._n_reflections  # number of possible ray-tracing solutions
                        sg['time_shower_and_ray'] = np.zeros((self._n_showers, n_antennas, nS))
                    self._channelSignalReconstructor.run(self._evt, self._station.get_sim_station(), self._det)
                    for channel in self._station.get_sim_station().iter_channels():
                        sg['max_amp_shower_and_ray'][self._get_shower_index(channel.get_shower_id()), channel.get_id(), channel.get_ray_tracing_solution_id()] = channel.get_parameter(chp.maximum_amplitude_envelope)
                        sg['time_shower_and_ray'][self._get_shower_index(channel.get_shower_id()), channel.get_id(), channel.get_ray_tracing_solution_id()] = channel.get_parameter(chp.signal_time)

                start_times = []
                channel_identifiers = []
                for channel in self._sim_station.iter_channels():
                    channel_identifiers.append(channel.get_unique_identifier())
                    start_times.append(channel.get_trace_start_time())
                start_times = np.array(start_times)
                start_times_sort = np.argsort(start_times)
                delta_start_times = start_times[start_times_sort][1:] - start_times[start_times_sort][:-1]  # this array is sorted in time
                split_event_time_diff = float(self._cfg['split_event_time_diff'])
                iSplit = np.atleast_1d(np.squeeze(np.argwhere(delta_start_times > split_event_time_diff)))
#                 print(f"start times {start_times}")
#                 print(f"sort array {start_times_sort}")
#                 print(f"delta times {delta_start_times}")
#                 print(f"split at indices {iSplit}")
                n_sub_events = len(iSplit) + 1
                if(n_sub_events > 1):
                    logger.warning("splitting event group id {self._event_group_id} into {n_sub_events} sub events")

                tmp_station = copy.deepcopy(self._station)
                for iEvent in range(n_sub_events):
                    iStart = 0
                    iStop = len(channel_identifiers)
                    if(n_sub_events > 1):
                        if(iEvent > 0):
                            iStart = iSplit[iEvent - 1] + 1
                    if(iEvent < n_sub_events - 1):
                        iStop = iSplit[iEvent] + 1
                    indices = start_times_sort[iStart: iStop]
                    logger.info(f"creating event {iEvent} of event group {self._event_group_id} ranging rom {iStart} to {iStop} with indices {indices}")
                    self._evt = NuRadioReco.framework.event.Event(self._event_group_id, iEvent)  # create new event
                    self._station = NuRadioReco.framework.station.Station(self._station_id)
                    sim_station = NuRadioReco.framework.sim_station.SimStation(self._station_id)
                    sim_station.set_is_neutrino()
                    tmp_sim_station = tmp_station.get_sim_station()
                    self._shower_ids_of_sub_event = []
                    for iCh in indices:
                        ch_uid = channel_identifiers[iCh]
                        shower_id = ch_uid[1]
                        if(shower_id not in self._shower_ids_of_sub_event):
                            self._shower_ids_of_sub_event.append(shower_id)
                        sim_station.add_channel(tmp_sim_station.get_channel(ch_uid))
                        efield_uid = ([ch_uid[0]], ch_uid[1], ch_uid[2])  # the efield unique identifier has as first parameter an array of the channels it is valid for
                        for efield in tmp_sim_station.get_electric_fields():
                            if(efield.get_unique_identifier() == efield_uid):
                                sim_station.add_electric_field(efield)
                    # add showers that contribute to this (sub) event to event structure
                    for shower_id in self._shower_ids_of_sub_event:
                        self._evt.add_sim_shower(self._evt_tmp.get_sim_shower(shower_id))
                    self._station.set_sim_station(sim_station)
                    self._station.set_station_time(self._evt_time)
                    self._evt.set_station(self._station)
                    if(bool(self._cfg['signal']['zerosignal'])):
                        self._increase_signal(None, 0)

                    logger.debug("performing detector simulation")
                    if(hasattr(self, '_detector_simulation_part2')):
                        # we give the user the opportunity to specify a custom detector simulation module sequence
                        # which might be needed for certain analyses
                        self._detector_simulation_part2()
                    else:
                        # start detector simulation
                        efieldToVoltageConverter.run(self._evt, self._station, self._det)  # convolve efield with antenna pattern
                        # downsample trace to internal simulation sampling rate (the efieldToVoltageConverter upsamples the trace to
                        # 20 GHz by default to achive a good time resolution when the two signals from the two signal paths are added)
                        channelResampler.run(self._evt, self._station, self._det, sampling_rate=1. / self._dt)
                        self._detector_simulation_filter_amp(self._evt, self._station, self._det)

                        if self._is_simulate_noise():
                            max_freq = 0.5 / self._dt
                            norm = self._get_noise_normalization(self._station.get_id())  # assuming the same noise level for all stations
                            Vrms = self._Vrms / (norm / (max_freq)) ** 0.5  # normalize noise level to the bandwidth its generated for
                            channelGenericNoiseAdder.run(self._evt, self._station, self._det, amplitude=Vrms, min_freq=0 * units.MHz,
                                                         max_freq=max_freq, type='rayleigh')

                        self._detector_simulation_trigger(self._evt, self._station, self._det)
                    if(not self._station.has_triggered()):
                        continue

                    triggered_showers[self._station_id].extend(self._get_shower_index(self._shower_ids_of_sub_event))
                    self._calculate_signal_properties()
                    self._save_triggers_to_hdf5()
                    t4 = time.time()
                    detSimTime += (t4 - t3)
                    if(self._outputfilenameNuRadioReco is not None and self._station.has_triggered()):
                        # downsample traces to detector sampling rate to save file size
                        channelResampler.run(self._evt, self._station, self._det, sampling_rate=self._sampling_rate_detector)
                        electricFieldResampler.run(self._evt, self._station.get_sim_station(), self._det, sampling_rate=self._sampling_rate_detector)

                        if self.__write_detector:
                            self._eventWriter.run(self._evt, self._det)
                        else:
                            self._eventWriter.run(self._evt)
                # end sub events loop

            # end station loop

        # end event group loop

        # Create trigger structures if there are no triggering events.
        # This is done to ensure that files with no triggering n_events
        # merge properly.
        self._create_empty_multiple_triggers()

        # save simulation run in hdf5 format (only triggered events)
        t5 = time.time()
        self._write_ouput_file()

        self.calculate_Veff()
        try:
            pass
        except:
            logger.error("error in calculating effective volume")

        t_total = time.time() - t_start
        outputTime = time.time() - t5

        output_NuRadioRecoTime = "Timing of NuRadioReco modules \n"
        ts = []
        for iM, (name, instance, kwargs) in enumerate(self._evt.iter_modules(self._station.get_id())):
            ts.append(instance.run.time[instance])
        ttot = np.sum(np.array(ts))
        for i, (name, instance, kwargs) in enumerate(self._evt.iter_modules(self._station.get_id())):
            t = pretty_time_delta(ts[i])
            trel = 100.*ts[i] / ttot
            output_NuRadioRecoTime += f"{name}: {t} {trel:.1f}%\n"
        logger.warning(output_NuRadioRecoTime)

        logger.warning("{:d} events processed in {} = {:.2f}ms/event ({:.1f}% input, {:.1f}% ray tracing, {:.1f}% askaryan, {:.1f}% detector simulation, {:.1f}% output)".format(self._n_showers,
                                                                                         pretty_time_delta(t_total), 1.e3 * t_total / self._n_showers,
                                                                                         100 * input_time / t_total,
                                                                                         100 * rayTracingTime / t_total,
                                                                                         100 * askaryan_time / t_total,
                                                                                         100 * detSimTime / t_total,
                                                                                         100 * outputTime / t_total))

    def _get_shower_index(self, shower_id):
        if(hasattr(shower_id, "__len__")):
            return np.array([self._shower_index[x] for x in shower_id])
        else:
            return self._shower_index[shower_id]

    def _is_simulate_noise(self):
        """
        returns True if noise should be added
        """
        return bool(self._cfg['noise'])

    def _get_noise_normalization(self, station_id, channel_id=0):
        """
        returns the normalization of the Vrms of the noise generator module.
        The normalization is
        Vrms = self._Vrms / (norm / (max_freq - min_freq))**0.5
        """
        if(station_id in self.__noise_adder_normalization and channel_id in self.__noise_adder_normalization[station_id]):
            return self.__noise_adder_normalization[station_id][channel_id]
        else:
            return 1.

    def _calculate_amplitude_per_ray_tracing_solution(self):
        if(not hasattr(self, "_calculateAmplitudePerRaySolution")):
            self._calculateAmplitudePerRaySolution = NuRadioReco.modules.custom.deltaT.calculateAmplitudePerRaySolution.calculateAmplitudePerRaySolution()
        self._calculateAmplitudePerRaySolution.run(self._evt, self._station, self._det)
        # save the amplitudes to output hdf5 file
        # save amplitudes per ray tracing solution to hdf5 data output
        sg = self._mout_groups[self._station_id]
        n_antennas = self._det.get_number_of_channels(self._station_id)
        nS = 2 + 4 * self._n_reflections  # number of possible ray-tracing solutions
        if('max_amp_ray_solution' not in sg):
            sg['max_amp_ray_solution'] = np.zeros((self._n_showers, n_antennas, nS))
        ch_counter = np.zeros(n_antennas, dtype=np.int)
        for efield in self._station.get_sim_station().get_electric_fields():
            for channel_id, maximum in iteritems(efield[efp.max_amp_antenna]):
                sg['max_amp_ray_solution'][self._iSh, channel_id, ch_counter[channel_id]] = maximum
                ch_counter[channel_id] += 1

    def _is_in_fiducial_volume(self):
        """
        checks wether a vertex is in the fiducial volume

        if the fiducial volume is not specified in the input file, True is returned (this is required for the simulation
        of pulser calibration measuremens)
        """
        tt = ['fiducial_rmin', 'fiducial_rmax', 'fiducial_zmin', 'fiducial_zmax']
        has_fiducial = True
        for t in tt:
            if(not t in self._fin_attrs):
                has_fiducial = False
        if(not has_fiducial):
            return True

        r = (self._x ** 2 + self._y ** 2) ** 0.5
        if(r >= self._fin_attrs['fiducial_rmin'] and r <= self._fin_attrs['fiducial_rmax']):
            if(self._z >= self._fin_attrs['fiducial_zmin'] and self._z <= self._fin_attrs['fiducial_zmax']):
                return True
        return False

    def _increase_signal(self, channel_id, factor):
        """
        increase the signal of a simulated station by a factor of x
        this is e.g. used to approximate a phased array concept with a single antenna

        Parameters
        ----------
        channel_id: int or None
            if None, all available channels will be modified
        """
        if(channel_id is None):
            for electric_field in self._station.get_sim_station().get_electric_fields():
                electric_field.set_trace(electric_field.get_trace() * factor, sampling_rate=electric_field.get_sampling_rate())

        else:
            sim_channels = self._station.get_sim_station().get_electric_fields_for_channels([channel_id])
            for sim_channel in sim_channels:
                sim_channel.set_trace(sim_channel.get_trace() * factor, sampling_rate=sim_channel.get_sampling_rate())

    def _read_input_hdf5(self):
        """
        reads input file into memory
        """
        fin = h5py.File(self._inputfilename, 'r')
        self._fin = {}
        self._fin_stations = {}
        self._fin_attrs = {}
        for key, value in iteritems(fin):
            if isinstance(value, h5py._hl.group.Group):
                self._fin_stations[key] = {}
                for key2, value2 in iteritems(value):
                    self._fin_stations[key][key2] = np.array(value2)
            self._fin[key] = np.array(value)
        for key, value in iteritems(fin.attrs):
            self._fin_attrs[key] = value
        fin.close()

    def _check_vertex_times(self):

        if 'vertex_times' in self._fin:
            return True
        else:
            warn_msg = 'The input file does not include vertex times. '
            warn_msg += 'Vertices from the same event will not be time-ordered.'
            logger.warning(warn_msg)
            return False

    def _calculate_signal_properties(self):
        if(self._station.has_triggered()):
            self._channelSignalReconstructor.run(self._evt, self._station, self._det)
            amplitudes = np.zeros(self._station.get_number_of_channels())
            amplitudes_envelope = np.zeros(self._station.get_number_of_channels())
            for channel in self._station.iter_channels():
                amplitudes[channel.get_id()] = channel.get_parameter(chp.maximum_amplitude)
                amplitudes_envelope[channel.get_id()] = channel.get_parameter(chp.maximum_amplitude_envelope)
            self._output_maximum_amplitudes[self._station.get_id()].append(amplitudes)
            self._output_maximum_amplitudes_envelope[self._station.get_id()].append(amplitudes_envelope)

    def _create_empty_multiple_triggers(self):
        if ('trigger_names' not in self._mout_attrs):
            self._mout_attrs['trigger_names'] = np.array([])
            self._mout['multiple_triggers'] = np.zeros((self._n_showers, 1), dtype=np.bool)
            for station_id in self._station_ids:
                sg = self._mout_groups[station_id]
                sg['multiple_triggers'] = np.zeros((self._n_showers, 1), dtype=np.bool)
                sg['triggered'] = np.zeros(self._n_showers, dtype=np.bool)

    def _create_trigger_structures(self):

        if('trigger_names' not in self._mout_attrs):
            self._mout_attrs['trigger_names'] = []
        extend_array = False
        for trigger in six.itervalues(self._station.get_triggers()):
            if(trigger.get_name() not in self._mout_attrs['trigger_names']):
                self._mout_attrs['trigger_names'].append((trigger.get_name()))
                extend_array = True
        # the 'multiple_triggers' output array is not initialized in the constructor because the number of
        # simulated triggers is unknown at the beginning. So we check if the key already exists and if not,
        # we first create this data structure
        if('multiple_triggers' not in self._mout):
            self._mout['multiple_triggers'] = np.zeros((self._n_showers, len(self._mout_attrs['trigger_names'])), dtype=np.bool)
            for station_id in self._station_ids:
                sg = self._mout_groups[station_id]
                sg['multiple_triggers'] = np.zeros((self._n_showers, len(self._mout_attrs['trigger_names'])), dtype=np.bool)
        elif(extend_array):
            tmp = np.zeros((self._n_showers, len(self._mout_attrs['trigger_names'])), dtype=np.bool)
            nx, ny = self._mout['multiple_triggers'].shape
            tmp[:, 0:ny] = self._mout['multiple_triggers']
            self._mout['multiple_triggers'] = tmp
            for station_id in self._station_ids:
                sg = self._mout_groups[station_id]
                tmp = np.zeros((self._n_showers, len(self._mout_attrs['trigger_names'])), dtype=np.bool)
                nx, ny = sg['multiple_triggers'].shape
                tmp[:, 0:ny] = sg['multiple_triggers']
                sg['multiple_triggers'] = tmp

    def _save_triggers_to_hdf5(self):
        self._create_trigger_structures()
        sg = self._mout_groups[self._station_id]
        self._output_event_group_ids[self._station_id].append(self._evt.get_run_number())
        self._output_sub_event_ids[self._station_id].append(self._evt.get_id())
        multiple_triggers = np.zeros(len(self._mout_attrs['trigger_names']), dtype=np.bool)
        for iT, trigger_name in enumerate(self._mout_attrs['trigger_names']):
            if(self._station.has_trigger(trigger_name)):
                multiple_triggers[iT] = self._station.get_trigger(trigger_name).has_triggered()
                for iSh in self._get_shower_index(self._shower_ids_of_sub_event):  # now save trigger information per shower of the current station
                    sg['multiple_triggers'][iSh][iT] = self._station.get_trigger(trigger_name).has_triggered()
                    self._mout['multiple_triggers'][iSh][iT] |= sg['multiple_triggers'][iSh][iT]
        for iSh in self._get_shower_index(self._shower_ids_of_sub_event):  # now save trigger information per shower of the current station
            sg['triggered'][iSh] = np.any(sg['multiple_triggers'][iSh])
            self._mout['triggered'][iSh] |= sg['triggered'][iSh]
        self._output_multiple_triggers_station[self._station_id].append(multiple_triggers)

        self._output_triggered_station[self._station_id].append(np.any(multiple_triggers))

    def get_Vrms(self):
        return self._Vrms

    def get_sampling_rate(self):
        return 1. / self._dt

    def get_bandwidth(self):
        return self._bandwidth

    def _check_if_was_pre_simulated(self):
        """
        checks if the same detector was simulated before (then we can save the ray tracing part)
        """
        self._was_pre_simulated = False
        if('detector' in self._fin_attrs):
            with open(self._detectorfile, 'r') as fdet:
                if(fdet.read() == self._fin_attrs['detector']):
                    self._was_pre_simulated = True
                    print("the simulation was already performed with the same detector")
        return self._was_pre_simulated

    def _create_meta_output_datastructures(self):
        """
        creates the data structures of the parameters that will be saved into the hdf5 output file
        """
        self._mout = {}
        self._mout_attributes = {}
        self._mout['weights'] = np.zeros(self._n_showers)
        self._mout['triggered'] = np.zeros(self._n_showers, dtype=np.bool)
#         self._mout['multiple_triggers'] = np.zeros((self._n_showers, self._number_of_triggers), dtype=np.bool)
        self._mout_attributes['trigger_names'] = None
        self._amplitudes = {}
        self._amplitudes_envelope = {}
        self._output_triggered_station = {}
        self._output_event_group_ids = {}
        self._output_sub_event_ids = {}
        self._output_multiple_triggers_station = {}
        self._output_maximum_amplitudes = {}
        self._output_maximum_amplitudes_envelope = {}

        for station_id in self._station_ids:
            n_antennas = self._det.get_number_of_channels(station_id)
            self._mout_groups[station_id] = {}
            sg = self._mout_groups[station_id]
            nS = 2 + 4 * self._n_reflections  # number of possible ray-tracing solutions
            sg['triggered'] = np.zeros(self._n_showers, dtype=np.bool)
            sg['launch_vectors'] = np.zeros((self._n_showers, n_antennas, nS, 3)) * np.nan
            sg['receive_vectors'] = np.zeros((self._n_showers, n_antennas, nS, 3)) * np.nan
            sg['ray_tracing_C0'] = np.zeros((self._n_showers, n_antennas, nS)) * np.nan
            sg['ray_tracing_C1'] = np.zeros((self._n_showers, n_antennas, nS)) * np.nan
            sg['ray_tracing_reflection'] = np.ones((self._n_showers, n_antennas, nS), dtype=np.int) * -1
            sg['ray_tracing_reflection_case'] = np.ones((self._n_showers, n_antennas, nS), dtype=np.int) * -1
            sg['ray_tracing_solution_type'] = np.ones((self._n_showers, n_antennas, nS), dtype=np.int) * -1
            sg['polarization'] = np.zeros((self._n_showers, n_antennas, nS, 3)) * np.nan
            sg['travel_times'] = np.zeros((self._n_showers, n_antennas, nS)) * np.nan
            sg['travel_distances'] = np.zeros((self._n_showers, n_antennas, nS)) * np.nan
            sg['focusing_factor'] = np.ones((self._n_showers, n_antennas, nS))

            self._output_event_group_ids[station_id] = []
            self._output_sub_event_ids[station_id] = []
            self._output_triggered_station[station_id] = []
            self._output_multiple_triggers_station[station_id] = []
            self._output_maximum_amplitudes[station_id] = []
            self._output_maximum_amplitudes_envelope[station_id] = []

    def _read_input_neutrino_properties(self):
        self._event_group_id = self._fin['event_group_ids'][self._iSh]
        self._flavor = self._fin['flavors'][self._iSh]
        self._energy = self._fin['energies'][self._iSh]
        self._inttype = self._fin['interaction_type'][self._iSh]
        self._x = self._fin['xx'][self._iSh]
        self._y = self._fin['yy'][self._iSh]
        self._z = self._fin['zz'][self._iSh]
        self._shower_type = self._fin['shower_type'][self._iSh]
        self._shower_energy = self._fin['shower_energies'][self._iSh]
        if 'vertex_times' in self._fin:
            self._vertex_time = self._fin['vertex_times'][self._iSh]
        self._zenith_shower = self._fin['zeniths'][self._iSh]
        self._azimuth_shower = self._fin['azimuths'][self._iSh]
        self._inelasticity = self._fin['inelasticity'][self._iSh]
        self._n_interaction = self._fin['n_interaction'][self._iSh]

    def _create_sim_station(self):
        """
        created an empyt sim_station object
        """
        # create NuRadioReco event structure
        self._sim_station = NuRadioReco.framework.sim_station.SimStation(self._station_id)
        self._sim_station.set_is_neutrino()

    def _create_sim_shower(self):
        """
        creates a sim_shower object and saves the meta arguments such as neutrino direction, self._energy and self._flavor
        """
        # create NuRadioReco event structure
        self._sim_shower = NuRadioReco.framework.radio_shower.RadioShower(self._shower_ids[self._iSh])
        # save relevant neutrino properties
        self._sim_shower[shp.zenith] = self._zenith_shower
        self._sim_shower[shp.azimuth] = self._azimuth_shower
        self._sim_shower[shp.energy] = self._shower_energy
        self._sim_shower[shp.flavor] = self._flavor
        self._sim_shower[shp.interaction_type] = self._inttype
        self._sim_shower[shp.vertex] = np.array([self._x, self._y, self._z])
        self._sim_shower[shp.vertex_time] = self._vertex_time
        self._sim_shower[shp.type] = self._shower_type

    def _write_ouput_file(self):
        folder = os.path.dirname(self._outputfilename)
        if(not os.path.exists(folder) and folder != ''):
            logger.warning(f"output folder {folder} does not exist, creating folder...")
            os.makedirs(folder)
        fout = h5py.File(self._outputfilename, 'w')

        saved = np.ones(len(self._mout['triggered']), dtype=np.bool)
        if (self._cfg['save_all'] == False):
            logger.info("saving only triggered events")
            # Careful! saved should be a copy of the triggered array, and not
            # a reference! saved indicates the interactions to be saved, while
            # triggered should indicate if an interaction has produced a trigger
            saved = np.copy(self._mout['triggered'])

            parent_indices = np.argwhere(self._fin['n_interaction'] == 1)

            for event_id in self._fin['event_group_ids']:
                event_mask = self._fin['event_group_ids'] == event_id
                event_indices = np.argwhere(self._fin['event_group_ids'] == event_id)[0]
                if (True in self._mout['triggered'][event_mask]):
                    saved[ np.intersect1d(parent_indices, event_indices)[0] ] = True
        else:
            logger.info("saving all events")

        # save data sets
        for (key, value) in iteritems(self._mout):
            fout[key] = value[saved]

        # save all data sets of the station groups
        for (key, value) in iteritems(self._mout_groups):
            sg = fout.create_group("station_{:d}".format(key))
            for (key2, value2) in iteritems(value):
                sg[key2] = value2[saved]

        # save "per event" quantities
        n_triggers = len(self._mout_attrs['trigger_names'])
        for station_id in self._mout_groups:
            n_events_for_station = len(self._output_triggered_station[station_id])
            n_channels = self._det.get_number_of_channels(station_id)
            sg = fout["station_{:d}".format(station_id)]
            sg['event_group_ids'] = np.array(self._output_event_group_ids[station_id])
            sg['event_ids'] = np.array(self._output_sub_event_ids[station_id])
            sg['maximum_amplitudes'] = np.array(self._output_maximum_amplitudes[station_id])
            sg['maximum_amplitudes_envelope'] = np.array(self._output_maximum_amplitudes_envelope[station_id])
            sg['triggered_per_event'] = np.array(self._output_triggered_station[station_id])

            # the multiple triggeres 2d array might have different number of entries per event
            # because the number of different triggers can increase dynamically
            # therefore we first create an array with the right size and then fill it
            tmp = np.zeros((n_events_for_station, n_triggers), dtype=np.bool)
            for iE, values in enumerate(self._output_multiple_triggers_station[station_id]):
                tmp[iE] = values
            sg['multiple_triggers_per_event'] = tmp

        # save meta arguments
        for (key, value) in iteritems(self._mout_attrs):
            fout.attrs[key] = value

        with open(self._detectorfile, 'r') as fdet:
            fout.attrs['detector'] = fdet.read()

        # save antenna position separately to hdf5 output
        for station_id in self._mout_groups:
            n_channels = self._det.get_number_of_channels(station_id)
            positions = np.zeros((n_channels, 3))
            for channel_id in range(n_channels):
                positions[channel_id] = self._det.get_relative_position(station_id, channel_id) + self._det.get_absolute_position(station_id)
            fout["station_{:d}".format(station_id)].attrs['antenna_positions'] = positions

        fout.attrs.create("Tnoise", self._Tnoise, dtype=np.float)
        fout.attrs.create("Vrms", self._Vrms, dtype=np.float)
        fout.attrs.create("dt", self._dt, dtype=np.float)
        fout.attrs.create("bandwidth", self._bandwidth, dtype=np.float)
        fout.attrs['n_samples'] = self._n_samples
        fout.attrs['config'] = yaml.dump(self._cfg)

        # save NuRadioMC and NuRadioReco versions
        from NuRadioMC.utilities import version
        import NuRadioMC
        fout.attrs['NuRadioMC_version'] = NuRadioMC.__version__
        fout.attrs['NuRadioReco_version'] = NuRadioReco.__version__
        fout.attrs['NuRadioMC_version_hash'] = version.get_NuRadioMC_commit_hash()
        fout.attrs['NuRadioReco_version_hash'] = version.get_NuRadioReco_commit_hash()

        # now we also save all input parameters back into the out file
        for key in self._fin.keys():
            if(key.startswith("station_")):
                continue
            if(not key in fout.keys()):  # only save data sets that havn't been recomputed and saved already
                fout[key] = np.array(self._fin[key])[saved]

        for key in self._fin_attrs.keys():
            if(not key in fout.attrs.keys()):  # only save atrributes sets that havn't been recomputed and saved already
                fout.attrs[key] = self._fin_attrs[key]
        fout.close()

    def calculate_Veff(self):
        # calculate effective
        from NuRadioMC.utilities.Veff import remove_duplicate_triggers
        triggered = remove_duplicate_triggers(self._mout['triggered'], self._fin['event_group_ids'])
        n_triggered = np.sum(triggered)
        n_triggered_weighted = np.sum(self._mout['weights'][triggered])
        n_events = self._fin_attrs['n_events']
        logger.warning(f'fraction of triggered events = {n_triggered:.0f}/{n_events:.0f} = {n_triggered / self._n_showers:.3f} (sum of weights = {n_triggered_weighted:.2f})')

        V = None
        if('xmax' in self._fin_attrs):
            dX = self._fin_attrs['xmax'] - self._fin_attrs['xmin']
            dY = self._fin_attrs['ymax'] - self._fin_attrs['ymin']
            dZ = self._fin_attrs['zmax'] - self._fin_attrs['zmin']
            V = dX * dY * dZ
        elif('rmin' in self._fin_attrs):
            rmin = self._fin_attrs['rmin']
            rmax = self._fin_attrs['rmax']
            dZ = self._fin_attrs['zmax'] - self._fin_attrs['zmin']
            V = np.pi * (rmax ** 2 - rmin ** 2) * dZ
        Veff = V * n_triggered_weighted / n_events
        logger.warning(f"Veff = {Veff / units.km ** 3:.4g} km^3, Veffsr = {Veff * 4 * np.pi/units.km**3:.4g} km^3 sr")

    def _calculate_polarization_vector(self):
        """ calculates the polarization vector in spherical coordinates (eR, eTheta, ePhi)
        """
        if(self._cfg['signal']['polarization'] == 'auto'):
            polarization_direction = np.cross(self._launch_vector, np.cross(self._shower_axis, self._launch_vector))
            polarization_direction /= np.linalg.norm(polarization_direction)
            cs = cstrans.cstrafo(*hp.cartesian_to_spherical(*self._launch_vector))
            return cs.transform_from_ground_to_onsky(polarization_direction)
        elif(self._cfg['signal']['polarization'] == 'custom'):
            ePhi = float(self._cfg['signal']['ePhi'])
            eTheta = (1 - ePhi ** 2) ** 0.5
            v = np.array([0, eTheta, ePhi])
            return v / np.linalg.norm(v)
        else:
            msg = "{} for config.signal.polarization is not a valid option".format(self._cfg['signal']['polarization'])
            logger.error(msg)
            raise ValueError(msg)
