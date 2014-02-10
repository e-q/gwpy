# coding=utf-8
# Copyright (C) Duncan Macleod (2013)
#
# This file is part of GWpy.
#
# GWpy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# GWpy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with GWpy.  If not, see <http://www.gnu.org/licenses/>.

"""Array with metadata
"""

from __future__ import (division, print_function)

import os
import numpy
import sys
import warnings
from math import (ceil, floor)
from multiprocessing import (cpu_count, Process, Queue)

from scipy import (fftpack, signal)
from matplotlib import mlab

try:
    from collections import OrderedDict
except ImportError:
    from astropy.utils import OrderedDict

from astropy import units
from astropy.io import registry as io_registry

from glue.lal import Cache

from .. import version
from ..data import (Series, Array2D)
from ..detector import Channel
from ..segments import (Segment, SegmentList)
from ..time import Time
from ..window import *

__author__ = "Duncan Macleod <duncan.macleod@ligo.org>"
__version__ = version.version

__all__ = ["TimeSeries", 'ArrayTimeSeries', 'TimeSeriesList', 'TimeSeriesDict']

_UFUNC_STRING = {'less': '<',
                 'less_equal': '<=',
                 'equal': '==',
                 'greater_equal': '>=',
                 'greater': '>',
                 }


class TimeSeries(Series):
    """A data array holding some metadata to represent a time series of
    instrumental or analysis data.

    Parameters
    ----------
    data : `numpy.ndarray`, `list`
        Data values to initialise TimeSeries
    epoch : `float` GPS time, or :class:`~gwpy.time.Time`, optional
        TimeSeries start time
    channel : :class:`~gwpy.detector.channels.Channel`, `str`, optional
        Data channel for this TimeSeries
    unit : :class:`~astropy.units.core.Unit`, optional
        The units of the data

    Returns
    -------
    TimeSeries
        a new `TimeSeries`

    Notes
    -----
    Any regular array, i.e. any iterable collection of data, can be
    easily converted into a `TimeSeries`.

    >>> data = numpy.asarray([1,2,3])
    >>> series = TimeSeries(data)

    The necessary metadata to reconstruct timing information are recorded
    in the `epoch` and `sample_rate` attributes. This time-stamps can be
    returned via the :attr:`~TimeSeries.times` property.

    All comparison operations performed on a `TimeSeries` will return a
    :class:`~gwpy.timeseries.statevector.StateTimeSeries` - a boolean array
    with metadata copied from the starting `TimeSeries`.
    """
    _metadata_slots = ['name', 'unit', 'epoch', 'channel', 'sample_rate']
    xunit = units.Unit('s')

    def __new__(cls, data, times=None, epoch=None, channel=None, unit=None,
                sample_rate=None, name=None, **kwargs):
        """Generate a new TimeSeries.
        """
        # parse Channel input
        if channel:
            channel = (isinstance(channel, Channel) and channel or
                       Channel(channel))
            name = name or channel.name
            unit = unit or channel.unit
            sample_rate = sample_rate or channel.sample_rate
        # generate TimeSeries
        new = super(TimeSeries, cls).__new__(cls, data, name=name, unit=unit,
                                             epoch=epoch, channel=channel,
                                             sample_rate=sample_rate,
                                             times=times, **kwargs)
        return new

    # -------------------------------------------
    # TimeSeries properties

    @property
    def epoch(self):
        """Starting GPS time epoch for this `TimeSeries`.

        This attribute is recorded as a `~gwpy.time.Time` object in the
        GPS format, allowing native conversion into other formats.

        See `~astropy.time` for details on the `Time` object.
        """
        try:
            return Time(self.x0, format='gps')
        except KeyError:
            raise AttributeError("No epoch has been set for this %s"
                                 % self.__class__.__name__)

    @epoch.setter
    def epoch(self, epoch):
        if isinstance(epoch, Time):
            self.x0 = epoch.gps
        elif isinstance(epoch, units.Quantity):
            self.x0 = epoch
        else:
            self.x0 = float(epoch)

    dt = Series.dx

    @property
    def sample_rate(self):
        """Data rate for this `TimeSeries` in samples per second (Hertz).
        """
        return (1 / self.dx).to('Hertz')

    @sample_rate.setter
    def sample_rate(self, val):
        self.dx = (1 / units.Quantity(val, units.Hertz)).to(self.xunit)

    @property
    def span(self):
        """Time Segment encompassed by thie `TimeSeries`.
        """
        return Segment(*Series.span.fget(self))

    @property
    def duration(self):
        """Duration of this `TimeSeries` in seconds.
        """
        return units.Quantity(self.span[1] - self.span[0], self.xunit)

    times = property(fget=Series.index.__get__,
                     fset=Series.index.__set__,
                     fdel=Series.index.__delete__,
                     doc="""Series of GPS times for each sample""")

    unit = property(fget=Series.unit.__get__,
                    fset=Series.unit.__set__,
                    fdel=Series.unit.__delete__,
                    doc="""Unit for this `TimeSeries`

                        :type: :class:`~astropy.units.core.Unit`
                        """)

    channel = property(fget=Series.channel.__get__,
                       fset=Series.channel.__set__,
                       fdel=Series.channel.__delete__,
                       doc="""Source data `Channel` for this `TimeSeries`

                           :type: :class:`~gwpy.detector.channel.Channel`
                           """)

    # -------------------------------------------
    # TimeSeries accessors

    # use input/output registry to allow multi-format reading
    read = classmethod(io_registry.read)

    @classmethod
    def fetch(cls, channel, start, end, host=None, port=None, verbose=False,
              connection=None, ndschanneltype=None):
        """Fetch data from NDS into a TimeSeries.

        Parameters
        ----------
        channel : :class:`~gwpy.detector.channel.Channel`, or `str`
            required data channel
        start : `~gwpy.time.Time`, or float
            GPS start time of data span
        end : `~gwpy.time.Time`, or float
            GPS end time of data span
        host : `str`, optional
            URL of NDS server to use, defaults to observatory site host
        port : `int`, optional
            port number for NDS server query, must be given with `host`
        verbose : `bool`, optional
            print verbose output about NDS progress
        connection : :class:`~gwpy.io.nds.NDS2Connection`
            open NDS connection to use
        ndschanneltype : `int`
            NDS2 channel type integer

        Returns
        -------
        TimeSeries
            a new `TimeSeries` containing the data read from NDS
        """
        return TimeSeriesDict.fetch(
                   [channel], start, end, host=host, port=port,
                   verbose=verbose, connection=connection,
                   ndschanneltype=ndschanneltype)[str(channel)]

    # -------------------------------------------
    # TimeSeries product methods

    def crop(self, start=None, end=None, copy=False):
        """Crop this `TimeSeries` to the given GPS ``[start, end)``
        `Segment`.

        Parameters
        ----------
        start : `Time`, `float`
            GPS start time to crop `TimeSeries` at left
        end : `Time`, `float`
            GPS end time to crop `TimeSeries` at right

        Returns
        -------
        timeseries : `TimeSeries`
            A new `TimeSeries` with the same metadata but different GPS
            span

        Notes
        -----
        If either ``start`` or ``end`` are outside of the original
        `TimeSeries` span, warnings will be printed and the limits will
        be restricted to the :attr:`TimeSeries.span`
        """
        # check type
        if isinstance(start, Time):
            start = start.gps
        if isinstance(end, Time):
            end = end.gps
        # pin early starts to time-series start
        if start == self.span[0]:
            start = None
        elif start is not None and start < self.span[0]:
            warnings.warn('TimeSeries.crop given GPS start earlier than '
                          'start time of the input TimeSeries. Crop will '
                          'begin when the TimeSeries actually starts.')
            start = None
        # pin late ends to time-series end
        if end == self.span[1]:
            end = None
        if start is not None and end > self.span[1]:
            warnings.warn('TimeSeries.crop given GPS end later than '
                          'end time of the input TimeSeries. Crop will '
                          'end when the TimeSeries actually ends.')
            end = None
        # find start index
        if start is None:
            idx0 = None
        else:
            idx0 = floor((start - self.span[0]) * self.sample_rate.value)
        # find end index
        if end is None:
            idx1 = None
        else:
            idx1 = floor((end - self.span[0]) * self.sample_rate.value)
            if idx1 >= self.size:
                idx1 = None
        # crop
        if copy:
            return self[idx0:idx1].copy()
        else:
            return self[idx0:idx1]

    def fft(self, fftlength=None):
        """Compute the one-dimensional discrete Fourier transform of
        this `TimeSeries`.

        Parameters
        ----------
        fftlength : `int`, optional
            length of the desired Fourier transform.
            Input will be cropped or padded to match the desired length.
            If fftlength is not given, the length of the `TimeSeries`
            will be used

        Returns
        -------
        out : complex `Series`
            the transformed output, with populated frequencies array
            metadata

        See Also
        --------
        :mod:`scipy.fftpack` for the definition of the DFT and conventions
        used.
        """
        from ..spectrum import Spectrum
        new = fftpack.fft(self.data, n=fftlength).view(Spectrum)
        new.frequencies = fftpack.fftfreq(new.size, d=self.dx)
        #new.x0 = new.frequencies[0]
        #if len(new.frequencies) > 1:
        #    new.dx = new.frequencies[1] - new.frequencies[0]
        return new

    def psd(self, fftlength=None, fftstride=None, method='welch', window=None,
            plan=None):
        """Calculate the power spectral density (PSD) `Spectrum` for this
        `TimeSeries`.

        The `method` argument can be one of

            - 'welch'
            - 'bartlett'
            - 'medianmean'
            - 'median'

        and any keyword arguments will be passed to the relevant method
        in `gwpy.spectrum`.

        Parameters
        ----------
        fftlength : `float`, default: :attr:`TimeSeries.duration`
            number of seconds in single FFT
        fftstride : `float`, optional, default: fftlength
            number of seconds between FFTs, default: no overlap
        method : `str`, optional, default: 'welch'
            average spectrum method
        window : `timeseries.Window`, optional
            window function to apply to timeseries prior to FFT
        plan : :lalsuite:`XLALREAL8ForwardFFTPlan`, optional
            LAL FFT plan to use when generating average spectrum,
            substitute type 'REAL8' as appropriate.

        Returns
        -------
        psd :  :class:`~gwpy.spectrum.core.Spectrum`
            a data series containing the PSD.
        """
        from ..spectrum import psd
        if fftlength is None:
            fftlength = self.duration.value
        if fftstride is None:
            fftstride = fftlength
        fftlength *= self.sample_rate.value
        fftstride *= self.sample_rate.value
        if isinstance(window, str):
            window = get_window(window, fftlength)
        try:
            psd_ = psd.lal_psd(self, method, int(fftlength), int(fftstride),
                               window=window, plan=plan)
        except ImportError:
            if window is None:
                window = 'hanning'
            psd_ = psd.scipy_psd(self, method, int(fftlength), int(fftstride),
                                 window=window)
        if psd_.unit:
            psd_.unit.__doc__ = "Power spectral density"
        return psd_

    def asd(self, fftlength=None, fftstride=None, method='welch', window=None,
            plan=None):
        """Calculate the amplitude spectral density (ASD) `Spectrum` for this
        `TimeSeries`.

        The `method` argument can be one of

            * 'welch'
            * 'bartlett'
            * 'medianmean'
            * 'median'

        and any keyword arguments will be passed to the relevant method
        in `gwpy.spectrum`.

        Parameters
        ----------
        fftlength : `float`, default: :attr:`TimeSeries.duration`
            number of seconds in single FFT
        fftstride : `float`, optional, default: fftlength
            number of seconds between FFTs, default: no overlap
        method : `str`, optional, default: 'welch'
            average spectrum method
        window : `timeseries.Window`, optional
            window function to apply to timeseries prior to FFT
        plan : :lalsuite:`XLALREAL8ForwardFFTPlan`, optional
            LAL FFT plan to use when generating average spectrum,
            substitute type 'REAL8' as appropriate.

        Returns
        -------
        psd :  :class:`~gwpy.spectrum.core.Spectrum`
            a data series containing the ASD.
        """
        asd = self.psd(fftlength, fftstride=fftstride, method=method,
                       window=window, plan=plan)
        asd **= 1/2.
        if asd.unit:
            asd.unit.__doc__ = "Amplitude spectral density"
        return asd

    def spectrogram(self, stride, fftlength=None, fftstride=None,
                    method='welch', window=None, plan=None,
                    maxprocesses=1, minprocesssize=1000):
        """Calculate the average power spectrogram of this `TimeSeries`
        using the specified average spectrum method.

        Parameters
        ----------
        stride : `float`
            number of seconds in single PSD (column of spectrogram)
        fftlength : `float`
            number of seconds in single FFT
        method : `str`, optional, default: 'welch'
            average spectrum method
        fftstride : `int`, optiona, default: fftlength
            number of seconds between FFTs
        window : `timeseries.window.Window`, optional, default: `None`
            window function to apply to timeseries prior to FFT
        plan : :lalsuite:`XLALREAL8ForwardFFTPlan`, optional
            LAL FFT plan to use when generating average spectrum,
            substitute type 'REAL8' as appropriate.
        maxprocesses : `int`, default: ``1``
            maximum number of independent frame reading processes, default
            is set to single-process file reading.
        minprocesssize : `int`, default: ``1000``
            number of individual FFTs to squeeze into a single process.
            large number by default to optimise short-duration FFT relative
            to overhead of multiprocessing.

        Returns
        -------
        spectrogram : :class:`~gwpy.spectrogram.core.Spectrogram`
            time-frequency power spectrogram as generated from the
            input time-series.
        """
        from ..spectrogram import from_timeseries
        return from_timeseries(self, stride, fftlength=fftlength,
                               fftstride=fftstride, method=method,
                               window=window, plan=plan,
                               maxprocesses=maxprocesses,
                               minprocesssize=minprocesssize)

    def fftgram(self, stride):
        """Calculate the Fourier-gram of this `TimeSeries`.

        At every ``stride``, a single, complex FFT is calculated.

        Parameters
        ----------
        stride : `float`
            number of seconds in single PSD (column of spectrogram)

        Returns
        -------
        fftgram : :class:`~gwpy.spectrogram.core.Spectrogram`
            a Fourier-gram
        """
        from ..spectrogram import Spectrogram

        fftlength = stride
        dt = stride
        df = 1/fftlength
        stride *= self.sample_rate.value
        # get size of Spectrogram
        nsteps = int(self.size // stride)
        # get number of frequencies
        nfreqs = int(fftlength*self.sample_rate.value)

        # generate output spectrogram
        dtype = numpy.complex
        out = Spectrogram(numpy.zeros((nsteps, nfreqs), dtype=dtype),
                          name=self.name, epoch=self.epoch, f0=0, df=df,
                          dt=dt, copy=True, unit=self.unit, dtype=dtype)
        # stride through TimeSeries, recording FFTs as columns of Spectrogram
        for step in range(nsteps):
            # find step TimeSeries
            idx = stride * step
            idx_end = idx + stride
            stepseries = self[idx:idx_end]
            # calculated FFT and stack
            stepfft = stepseries.fft()
            out[step] = stepfft.data
            if step == 0:
                out.frequencies = stepfft.frequencies
        return out

    # -------------------------------------------
    # TimeSeries filtering

    def highpass(self, frequency, amplitude=0.9, order=8, method='scipy'):
        """Filter this `TimeSeries` with a Butterworth high-pass filter.

        See (for example) :lalsuite:`XLALHighPassREAL8TimeSeries` for more
        information.

        Parameters
        ----------
        frequency : `float`
            minimum frequency for high-pass
        amplitude : `float`, optional
            desired amplitude response of the filter
        order : `int`, optional
            desired order of the Butterworth filter
        method : `str`, optional, default: 'scipy'
            choose method of high-passing, LAL or SciPy

        Returns
        -------
        TimeSeries

        See Also
        --------
        See :lalsuite:`XLALHighPassREAL8TimeSeries` for information on
        the LAL method, otherwise see :mod:`scipy.signal` for the SciPy
        method.
        """
        if method.lower() == 'lal':
            from lal import lal
            lalts = self.to_lal()
            highpass = getattr(lal, 'HighPass%s' % lalts.__class__.__name__)
            highpass(lalts, float(frequency), amplitude, order)
            return TimeSeries.from_lal(lalts)
        elif method.lower() == 'scipy':
            # build filter
            b, a = signal.butter(order, numpy.float64(frequency * 2.0 /
                                                      self.sample_rate),
                                 btype='highpass')
            return self.filter(b, a)
        raise NotImplementedError("Highpass filter method '%s' not "
                                  "recognised, please choose one of "
                                  "'scipy' or 'lal'")

    def lowpass(self, frequency, amplitude=0.9, order=4, method='scipy'):
        """Filter this `TimeSeries` with a Butterworth low-pass filter.

        Parameters
        ----------
        frequency : `float`
            minimum frequency for low-pass
        amplitude : `float`, optional
            desired amplitude response of the filter
        order : `int`, optional
            desired order of the Butterworth filter
        method : `str`, optional, default: 'scipy'
            choose method of high-passing, LAL or SciPy

        Returns
        -------
        TimeSeries

        See Also
        --------
        See :lalsuite:`XLALLowPassREAL8TimeSeries` for information on
        the LAL method, otherwise see :mod:`scipy.signal` for the SciPy
        method.
        """
        if method.lower() == 'lal':
            from lal import lal
            lalts = self.to_lal()
            lowpass = getattr(lal, 'LowPass%s' % lalts.__class__.__name__)
            lowpass(lalts, float(frequency), amplitude, order)
            return TimeSeries.from_lal(lalts)
        elif method.lower() == 'scipy':
            # build filter
            b, a = signal.butter(order, numpy.float64(frequency * 2.0 /
                                                      self.sample_rate),
                                 btype='lowpass')
            return self.filter(b, a)
        raise NotImplementedError("Lowpass filter method '%s' not "
                                  "recognised, please choose one of "
                                  "'scipy' or 'lal'")

    def bandpass(self, flow, fhigh, amplitude=0.9, order=6, method='scipy'):
        """Filter this `TimeSeries` by applying both low- and high-pass
        filters.

        See (for example) :lalsuite:`XLALLowPassREAL8TimeSeries` for more
        information.

        Parameters
        ----------
        flow : `float`
            minimum frequency for high-pass
        fhigh : `float`
            maximum frequency for low-pass
        amplitude : `float`, optional
            desired amplitude response of the filter
        order : `int`, optional
            desired order of the Butterworth filter

        Returns
        -------
        TimeSeries

        See Also
        --------
        See :lalsuite:`XLALLowPassREAL8TimeSeries` for information on
        the LAL method, otherwise see :mod:`scipy.signal` for the SciPy
        method.
        """
        try:
            high = self.highpass(flow, amplitude=amplitude, order=order,
                                 method=method)
        except NotImplementedError as e:
            raise NotImplementedError(str(e).replace('Lowpass', 'Bandpass'))
        else:
            return high.lowpass(fhigh, amplitude=amplitude, order=order,
                                method=method)

    def filter(self, *filt):
        """Apply the given `Filter` to this `TimeSeries`

        Parameters
        ----------
        *filt
            one of:

            - a single :class:`scipy.signal.lti` filter
            - (numerator, denominator) polynomials
            - (zeros, poles, gain)
            - (A, B, C, D) 'state-space' representation

        Returns
        -------
        ftimeseries : `TimeSeries`
            the filtered version of the input `TimeSeries`

        See also
        --------
        :mod:`scipy.signal`
            for details on filtering and representations
        """
        if len(filt) == 1 and isinstance(filt, signal.lti):
            filt = filt[0]
            a = filt.den
            b = filt.num
        elif len(filt) == 2:
            b, a = filt
        elif len(filt) == 3:
            b, a = signal.zpk2tf(*filt)
        elif len(filt) == 4:
            b, a = signal.ss2tf(*filt)
        else:
            raise ValueError("Cannot interpret filter arguments. Please give "
                             "either a signal.lti object, or a tuple in zpk "
                             "or ba format. See scipy.signal docs for "
                             "details.")
        new = signal.lfilter(b, a, self, axis=0).view(self.__class__)
        new.metadata = self.metadata.copy()
        return new

    def coherence(self, other, fftlength=None, fftstride=None,
                  window=None, **kwargs):
        """Calculate the frequency-coherence between this `TimeSeries`
        and another.

        Parameters
        ----------
        other : `TimeSeries`
            `TimeSeries` signal to calculate coherence with
        fftlength : `float`, optional, default: `TimeSeries.duration`
            number of seconds in single FFT, defaults to a single FFT
        fftstride : `int`, optiona, default: fftlength
            number of seconds between FFTs, defaults to no overlap
        window : `timeseries.window.Window`, optional, default: `HanningWindow`
            window function to apply to timeseries prior to FFT,
            default HanningWindow of the relevant size
        **kwargs
            any other keyword arguments accepted by
            :func:`matplotlib.mlab.cohere` except ``NFFT``, ``window``,
            and ``noverlap`` which are superceded by the above keyword
            arguments

        Returns
        -------
        coherence : :class:`~gwpy.spectrum.core.Spectrum`
            the coherence `Spectrum` of this `TimeSeries` with the other

        Notes
        -----
        If `self` and `other` have difference
        :attr:`TimeSeries.sample_rate` values, the higher sampled
        `TimeSeries` will be down-sampled to match the lower.

        See Also
        --------
        :func:`matplotlib.mlab.cohere`
            for details of the coherence calculator
        """
        from ..spectrum import Spectrum
        # check sampling rates
        if self.sample_rate.to('Hertz') != other.sample_rate.to('Hertz'):
            sampling = min(self.sample_rate.value, other.sample_rate.value)
            # resample higher rate series
            if self.sample_rate.value == sampling:
                other = other.resample(sampling)
                self_ = self
            else:
                self_ = self.resample(sampling)
        else:
            sampling = self.sample_rate.value
            self_ = self
        # check fft lengths
        if fftlength is None:
            fftlength = self_.duration.value
        if fftstride is None:
            fftstride = fftlength
        fftlength = int(numpy.float64(fftlength) * self_.sample_rate.value)
        fftstride = int(numpy.float64(fftstride) * self_.sample_rate.value)
        if window is None:
            window = HanningWindow(fftlength)
        coh, f = mlab.cohere(self_.data, other.data, NFFT=fftlength,
                             Fs=sampling, window=window,
                             noverlap=fftlength-fftstride, **kwargs)
        out = coh.view(Spectrum)
        out.f0 = f[0]
        out.df = (f[1] - f[0])
        out.epoch = self.epoch
        out.name = 'Coherence between %s and %s' % (self.name, other.name)
        return out

    def auto_coherence(self, dt, fftlength=None, fftstride=None,
                       window=None, **kwargs):
        """Calculate the frequency-coherence between this `TimeSeries`
        and a time-shifted copy of itself.

        The standard :meth:`TimeSeries.coherence` is calculated between
        the input `TimeSeries` and a :meth:`cropped <TimeSeries.crop>`
        copy of itself. Since the cropped version will be shorter, the
        input series will be shortened to match.

        Parameters
        ----------
        dt : `float`
            duration (in seconds) of time-shift
        fftlength : `float`, optional, default: `TimeSeries.duration`
            number of seconds in single FFT, defaults to a single FFT
        fftstride : `int`, optiona, default: fftlength
            number of seconds between FFTs, defaults to no overlap
        window : `timeseries.window.Window`, optional, default: `HanningWindow`
            window function to apply to timeseries prior to FFT,
            default HanningWindow of the relevant size
        **kwargs
            any other keyword arguments accepted by
            :func:`matplotlib.mlab.cohere` except ``NFFT``, ``window``,
            and ``noverlap`` which are superceded by the above keyword
            arguments

        Returns
        -------
        coherence : :class:`~gwpy.spectrum.core.Spectrum`
            the coherence `Spectrum` of this `TimeSeries` with the other

        Notes
        -----
        The :meth:`TimeSeries.auto_coherence` will perform best when
        ``dt`` is approximately ``fftlength / 2``.

        See Also
        --------
        :func:`matplotlib.mlab.cohere`
            for details of the coherence calculator
        """
        # shifting self backwards is the same as forwards
        dt = abs(dt)
        # crop inputs
        self_ = self.crop(self.span[0], self.span[1] - dt)
        other = self.crop(self.span[0] + dt, self.span[1])
        return self_.coherence(other, fftlength=fftlength,
                               fftstride=fftstride, window=window, **kwargs)

    def rms(self, stride=1):
        """Calculate the root-mean-square value of this `TimeSeries`
        once per stride.

        Parameters
        ----------
        stride : `float`
            stride (seconds) between RMS calculations

        Returns
        -------
        rms : `TimeSeries`
            a new `TimeSeries` containing the RMS value with dt=stride
        """
        stridesamp = stride * self.sample_rate.value
        nsteps = int(self.size // stridesamp)
        # stride through TimeSeries, recording RMS
        data = numpy.zeros(nsteps)
        for step in range(nsteps):
            # find step TimeSeries
            idx = stridesamp * step
            idx_end = idx + stridesamp
            stepseries = self[idx:idx_end]
            rms_ = numpy.sqrt(numpy.mean(numpy.abs(stepseries.data)**2))
            data[step] = rms_
        name = '%s %.2f-second RMS' % (self.name, stride)
        return self.__class__(data, channel=self.channel, epoch=self.epoch,
                              name=name, sample_rate=(1/float(stride)))

    # -------------------------------------------
    # connectors

    def is_compatible(self, other):
        """Check whether metadata attributes for self and other match.
        """
        if not self.sample_rate == other.sample_rate:
            raise ValueError("TimeSeries sampling rates do not match: "
                             "%s vs %s." % (self.sample_rate,
                                            other.sample_rate))
        if not self.unit == other.unit:
            raise ValueError("TimeSeries units do not match: %s vs %s."
                             % (str(self.unit), str(other.unit)))
        return True

    def is_contiguous(self, other):
        """Check whether other is contiguous with self.
        """
        self.is_compatible(other)
        if self.span[1] == other.span[0]:
            return 1
        elif other.span[1] == self.span[0]:
            return -1
        else:
            return 0

    def append(self, other, gap='raise', inplace=True, pad=0.0):
        """Connect another `TimeSeries` onto the end of the current one.

        Parameters
        ----------
        other : `TimeSeries`
            the second data set to connect to this one
        gap : `str`, optional, default: ``'raise'``
            action to perform if there's a gap between the other series
            and this one. One of

                - ``'raise'`` - raise an `Exception`
                - ``'ignore'`` - remove gap and join data
                - ``'pad'`` - pad gap with zeros

        inplace : `bool`, optional, default: `True`
            perform operation in-place, modifying current `TimeSeries,
            otherwise copy data and return new `TimeSeries`
        pad : `float`, optional, default: ``0.0``
            value with which to pad discontiguous `TimeSeries`

        Returns
        -------
        series : `TimeSeries`
            time-series containing joined data sets
        """
        # check metadata
        self.is_compatible(other)
        # make copy if needed
        if inplace:
            new = self
        else:
            new = self.copy()
        # fill gap
        if new.is_contiguous(other) != 1:
            if gap == 'pad':
                ngap = (other.span[0] - new.span[1]) * new.sample_rate.value
                if ngap < 1:
                    raise ValueError("Cannot append TimeSeries that starts "
                                     "before this one.")
                gapshape = list(new.shape)
                gapshape[0] = int(ngap)
                padding = numpy.ones(gapshape).view(new.__class__) * pad
                padding.epoch = new.span[1]
                padding.sample_rate = new.sample_rate
                padding.unit = new.unit
                new.append(padding, inplace=True)
            elif gap == 'ignore':
                pass
            elif new.span[0] < other.span[0] < new.span[1]:
                raise ValueError("Cannot append overlapping TimeSeries")
            else:
                raise ValueError("Cannot append discontiguous TimeSeries")
        # resize first
        s = list(new.shape)
        s[0] = new.shape[0] + other.shape[0]
        new.resize(s, refcheck=False)
        new[-other.shape[0]:] = other.data
        return new

    def prepend(self, other, gap='raise', inplace=True, pad=0.0):
        """Connect another `TimeSeries` onto the start of the current one.

        Parameters
        ----------
        other : `TimeSeries`
            the second data set to connect to this one
        gap : `str`, optional, default: ``'raise'``
            action to perform if there's a gap between the other series
            and this one. One of

                - ``'raise'`` - raise an `Exception`
                - ``'ignore'`` - remove gap and join data
                - ``'pad'`` - pad gap with zeros

        inplace : `bool`, optional, default: `True`
            perform operation in-place, modifying current `TimeSeries,
            otherwise copy data and return new `TimeSeries`
        pad : `float`, optional, default: ``0.0``
            value with which to pad discontiguous `TimeSeries`

        Returns
        -------
        series : `TimeSeries`
            time-series containing joined data sets
        """
        # check metadata
        self.is_compatible(other)
        # make copy if needed
        if inplace:
            new = self
        else:
            new = self.copy()
        # fill gap
        if new.is_contiguous(other) != -1:
            if gap == 'pad':
                ngap = int((new.span[0]-other.span[1]) * new.sample_rate.value)
                if ngap < 1:
                    raise ValueError("Cannot prepend TimeSeries that starts "
                                     "after this one.")
                gapshape = list(new.shape)
                gapshape[0] = ngap
                padding = numpy.ones(gapshape).view(new.__class__) * pad
                padding.epoch = other.span[1]
                padding.sample_rate = new.sample_rate
                padding.unit = new.unit
                new.prepend(padding, inplace=True)
            elif gap == 'ignore':
                pass
            elif other.span[0] < new.span[0] < other.span[1]:
                raise ValueError("Cannot prepend overlapping TimeSeries")
            else:
                raise ValueError("Cannot prepend discontiguous TimeSeries")
        # resize first
        N = new.shape[0]
        s = list(new.shape)
        s[0] = new.shape[0] + other.shape[0]
        new.resize(s, refcheck=False)
        new[-N:] = new.data[:N]
        new[:other.shape[0]] = other.data
        return new

    # -------------------------------------------
    # Utilities

    def plot(self, **kwargs):
        """Plot the data for this TimeSeries.
        """
        from ..plotter import TimeSeriesPlot
        return TimeSeriesPlot(self, **kwargs)

    @classmethod
    def from_nds2_buffer(cls, buffer_):
        """Construct a new `TimeSeries` from an `nds2.buffer` object
        """
        # cast as TimeSeries and return
        epoch = Time(buffer_.gps_seconds, buffer_.gps_nanoseconds,
                     format='gps')
        channel = Channel.from_nds2(buffer_.channel)
        return cls(buffer_.data, epoch=epoch, channel=channel)

    @classmethod
    def from_lal(cls, lalts):
        """Generate a new TimeSeries from a LAL TimeSeries of any type.
        """
        # write Channel
        try:
            from lal import UnitToString
        except ImportError:
            raise ImportError("No module named lal. Please see https://"
                              "www.lsc-group.phys.uwm.edu/daswg/"
                              "projects/lalsuite.html for installation "
                              "instructions")
        else:
            channel = Channel(lalts.name, 1/lalts.deltaT,
                              unit=UnitToString(lalts.sampleUnits),
                              dtype=lalts.data.data.dtype)
        return cls(lalts.data.data, channel=channel, epoch=lalts.epoch,
                   unit=UnitToString(lalts.sampleUnits), copy=True)

    def to_lal(self):
        """Convert this `TimeSeries` into a LAL TimeSeries.
        """
        try:
            import lal
        except ImportError:
            raise ImportError("No module named lal. Please see https://"
                              "www.lsc-group.phys.uwm.edu/daswg/"
                              "projects/lalsuite.html for installation "
                              "instructions")
        else:
            from lal import utils as lalutils
        laltype = lalutils.LAL_TYPE_FROM_NUMPY[self.dtype.type]
        typestr = lalutils.LAL_TYPE_STR[laltype]
        create = getattr(lal, 'Create%sTimeSeries' % typestr.upper())
        lalts = create(self.name, lal.LIGOTimeGPS(self.epoch.gps), 0,
                       self.dt.value, lal.lalDimensionlessUnit, self.size)
        lalts.data.data = self.data
        return lalts

    #@staticmethod
    #def watch(channel, duration, stride=1, host=None, port=None,
    #          outputfile=None, verbose=False):
    #    """Stream data over NDS2, with an updating display
    #    """
    #    from ..io import nds
    #    # user-defined host
    #    if host:
    #        hostlist = [(host, port)]
    #    else:
    #        hostlist = nds.host_resolution_order(channel.ifo)
    #    for host,port in hostlist:
    #        # connect and find channel
    #        if verbose:
    #            print("Connecting to %s:%s" % (host, port))
    #        connection = nds.NDS2Connection(host, port)
    #        try:
    #            channel = connection.find(
    #                      str(channel), nds.nds2.channel.CHANNEL_TYPE_ONLINE)[0]
    #        except IndexError:
    #            continue
    #        # begin iteration
    #        sampling = channel.sample_rate
    #        timeseries = TimeSeries(numpy.zeros((sampling*duration,)),
    #                                channel=Channel.from_nds2(channel))
    #        timeseries.epoch = gpstime.gps_time_now()-duration
    #        plot = timeseries.plot(auto_refresh=True)
    #        trace = plot._layers[timeseries.name]
    #        try:
    #            for buf in connection.iterate([channel.name]):
    #                newdata = buf[0]
    #                epoch = lal.LIGOTimeGPS(newdata.gps_seconds,
    #                                        newdata.gps_nanoseconds)
    #                # shift
    #                timeseries.epoch = epoch + stride - duration
    #                timeseries.data[:-newdata.length] = (
    #                    timeseries.data[newdata.length:])
    #                timeseries.data[-newdata.length:] = newdata.data
    #                trace.set_xdata(timeseries.times.data)
    #                trace.set_ydata(timeseries.data)
    #                plot.epoch = epoch
    #                del plot.ylim
    #                plot.xlim = timeseries.span
    #        except KeyboardInterrupt:
    #            return

    # -------------------------------------------
    # TimeSeries operations

    def __array_wrap__(self, obj, context=None):
        """Wrap an array into a TimeSeries, or a StateTimeSeries if
        dtype == bool.
        """
        if obj.dtype == numpy.dtype(bool):
            from .statevector import StateTimeSeries
            ufunc = context[0]
            value = context[1][-1]
            try:
                op_ = _UFUNC_STRING[ufunc.__name__]
            except KeyError:
                op_ = ufunc.__name__
            result = obj.view(StateTimeSeries)
            result.metadata = self.metadata.copy()
            result.unit = ""
            result.name = '%s %s %s' % (obj.name, op_, value)
            if hasattr(obj, 'unit') and str(obj.unit):
                result.name += ' %s' % str(obj.unit)
        else:
            result = super(TimeSeries, self).__array_wrap__(obj,
                                                            context=context)
        return result


class ArrayTimeSeries(TimeSeries, Array2D):
    xunit = TimeSeries.xunit

    def __new__(cls, data, times=None, epoch=None, channel=None, unit=None,
                sample_rate=None, name=None, **kwargs):
        """Generate a new ArrayTimeSeries.
        """
        # parse Channel input
        if channel:
            channel = (isinstance(channel, Channel) and channel or
                       Channel(channel))
            name = name or channel.name
            unit = unit or channel.unit
            sample_rate = sample_rate or channel.sample_rate
        # generate TimeSeries
        new = Array2D.__new__(cls, data, name=name, unit=unit, epoch=epoch,
                              channel=channel, sample_rate=sample_rate,
                              times=times, **kwargs)
        return new


class TimeSeriesList(list):
    """Fancy list representing a list of `TimeSeries`

    The `TimeSeriesList` provides an easy way to collect and organise
    `TimeSeries` for a single `Channel` over multiple segments.

    Parameters
    ----------
    *items
        any number of `TimeSeries`

    Returns
    -------
    list
        a new `TimeSeriesList`

    Raises
    ------
    TypeError
        if any elements are not `TimeSeries`
    """
    EntryClass = TimeSeries

    def __init__(self, *items):
        """Initalise a new `TimeSeriesList`
        """
        super(TimeSeriesList, self).__init__()
        for item in items:
            self.append(item)

    @property
    def segments(self):
        return SegmentList([item.span for item in self])

    def append(self, item):
        if not isinstance(item, self.EntryClass):
            raise TypeError("Cannot append type '%s' to %s"
                            % (item.__class__.__name__,
                               self.__class__.__name__))
        super(TimeSeriesList, self).append(item)
    append.__doc__ = list.append.__doc__

    def extend(self, item):
        item = TimeSeriesList(item)
        super(TimeSeriesList, self).extend(item)
    extend.__doc__ = list.extend.__doc__

    def coalesce(self):
        """Sort the elements of this `TimeSeriesList` by epoch and merge
        contiguous `TimeSeries` elements into single objects.
        """
        self.sort(key=lambda ts: ts.x0.value)
        i = j = 0
        N = len(self)
        while j < N:
            this = self[j]
            j += 1
            if j < N and this.span[1] >= self[j].span[0]:
                while j < N and this.span[1] >= self[j].span[0]:
                    try:
                        this = self[i] = this.append(self[j])
                    except ValueError:
                        this = this.copy()
                        this = self[i] = this.append(self[j])
                    j += 1
            else:
                self[i] = this
            i += 1
        del self[i:]
        return self

    def join(self, pad=0.0, gap='pad'):
        """Concatenate all of the `TimeSeries` in this list into a
        a single object

        Returns
        -------
        `TimeSeries`
             a single `TimeSeries covering the full span of all entries
             in this list
        """
        if len(self) == 0:
            return self.EntryClass([])
        self.sort(key=lambda t: t.x0.value)
        out = self[0].copy()
        for ts in self[1:]:
            out.append(ts, gap=gap, pad=pad)
        return out


class TimeSeriesDict(OrderedDict):
    """Ordered key-value mapping of named `TimeSeries` containing data
    for many channels over the same time interval.

    Currently the class doesn't do anything special. FIXME.
    """
    EntryClass = TimeSeries
    read = classmethod(io_registry.read)

    def append(self, other, **kwargs):
        for key, ts in other.iteritems():
            if key in self:
                self[key].append(ts, **kwargs)
            else:
                self[key] = ts.copy()

    def prepend(self, other, **kwargs):
        for key, ts in other.iteritems():
            if key in self:
                self[key].prepend(ts, **kwargs)
            else:
                self[key] = ts

    @classmethod
    def fetch(cls, channels, start, end, host=None, port=None,
              verbose=False, connection=None, ndschanneltype=None):
        """Fetch data from NDS for a number of channels.

        Parameters
        ----------
        channels : `list`
            required data channels.
        start : `~gwpy.time.Time`, or float
            GPS start time of data span.
        end : `~gwpy.time.Time`, or float
            GPS end time of data span.
        host : `str`, optional
            URL of NDS server to use, defaults to observatory site host.
        port : `int`, optional
            port number for NDS server query, must be given with `host`.
        verbose : `bool`, optional
            print verbose output about NDS progress.
        connection : :class:`~gwpy.io.nds.NDS2Connection`
            open NDS connection to use.
        ndschanneltype : `int`
            NDS2 channel type integer.

        Returns
        -------
        data : :class:`~gwpy.timeseries.core.TimeSeriesDict`
            a new `TimeSeriesDict` of (`str`, `TimeSeries`) pairs fetched
            from NDS.
        """
        # import module and type-cast arguments
        from ..io import nds as ndsio
        import nds2
        start = int(floor(isinstance(start, Time) and start.gps or start))
        end = int(ceil(isinstance(end, Time) and end.gps or end))

        # set context
        if verbose:
            outputcontext = ndsio.NDSOutputContext()
        else:
            outputcontext = ndsio.NDSOutputContext(open(os.devnull, 'w'),
                                                   open(os.devnull, 'w'))

        # check consistency of channel attributes
        ifos = set([Channel(channel).ifo for channel in channels])
        if len(ifos) == 1:
            ifo = list(ifos)[0]
        else:
            ifo = None
        if (not ndschanneltype and
                all([isinstance(c, Channel) for c in channels])):
            types_ = set([c.type for c in channels])
            if len(types_) == 1:
                ndschanneltype = ndsio.NDS2_CHANNEL_TYPE[list(types_)[0]]
        if not ndschanneltype:
            ndschanneltype = (nds2.channel.CHANNEL_TYPE_RAW |
                              nds2.channel.CHANNEL_TYPE_RDS |
                              nds2.channel.CHANNEL_TYPE_STREND |
                              nds2.channel.CHANNEL_TYPE_MTREND)

        # user-defined host or open connection
        if connection or host:
            hostlist = [(host, port)]
        # best guess host
        else:
            hostlist = ndsio.host_resolution_order(ifo)

        # build channellist
        ndschannels = []
        for channel in channels:
            c = Channel(channel)
            if c.type:
                ndschannels.append(c.ndsname)
            else:
                ndschannels.append(str(c))

        # test for minute trends
        if (any([Channel(c).type == 'm-trend' for c in channels]) and
                (start % 60 or end % 60)):
            warnings.warn("Requested at least one minute trend, but "
                          "start and stop GPS times are not modulo "
                          "60-seconds (from GPS epoch). Times will be "
                          "expanded outwards to compensate")
            if start % 60:
                start = start // 60 * 60
            if end % 60:
                end = end // 60 * 60 + 60

        # loop hosts, stopping on first success
        for host_, port_ in hostlist:
            if connection:
                _conn = connection
            # open connection if needed - check kerberos ticket
            if connection is None:
                if verbose:
                    print("Connecting to %s:%s" % (host_, port_))
                try:
                    with outputcontext:
                        _conn = nds2.connection(host_, port_)
                except RuntimeError as e:
                    if str(e).startswith('Request SASL authentication'):
                        print('\nError authenticating against %s' % host_,
                              file=sys.stderr)
                        ndsio.kinit()
                        with outputcontext:
                            _conn = nds2.connection(host_, port_)
                    else:
                        raise

            with outputcontext:
                # fetch data
                try:
                    if verbose:
                        print("Downloading data...", end=' ')
                    data = _conn.fetch(start, end, ndschannels)
                except RuntimeError as e:
                    print(' Something went wrong.')
                    # if error and user supplied their own server, raise
                    if connection:
                        raise
                    # otherwise warn and move on
                    elif verbose:
                        warnings.warn(str(e), ndsio.NDSWarning)
                else:
                    out = TimeSeriesDict()
                    for buffer_, c in zip(data, channels):
                        out[str(c)] = TimeSeries.from_nds2_buffer(buffer_)
                    print(' Success.')
                    return out

        # if we got this far, we can't get all of the channels in one go
        if len(channels) > 1:
            return TimeSeriesDict(
                       (str(c), TimeSeries.fetch(c, start, end, host=host,
                                                 port=port, verbose=verbose,
                                                 connection=connection,
                                                 ndschanneltype=ndschanneltype))
                       for c in channels)

        raise RuntimeError("Cannot find all relevant data on any known server")
