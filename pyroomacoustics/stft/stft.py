# Author: Eric Bezzam
# Date: Feb 1, 2016

"""Class for real-time STFT analysis and processing."""
from __future__ import division

import sys
import numpy as np
from numpy.lib.stride_tricks import as_strided as _as_strided
import warnings
from .dft import DFT


class STFT(object):
    """
    A class for STFT processing.

    Parameters
    -----------
    N : int
        number of samples per frame
    hop : int
        hop size
    analysis_window : numpy array
        window applied to block before analysis
    synthesis : numpy array
        window applied to the block before synthesis
    channels : int
        number of signals

    transform (optional) : str
        which FFT package to use: 'numpy' (default), 'pyfftw', or 'mkl'
    streaming (optional) : bool
        whether (True) or not (False, default) to "stitch" samples between 
        repeated calls of 'analysis' and 'synthesis' if we are receiving a 
        continuous stream of samples.
    num_frames (optional) : int
        Number of frames to be processed. If set, this will be strictly enforced
        as the STFT block will allocate memory accordingly. If not set, there
        will be no check on the number of frames sent to 
        analysis/process/synthesis

        NOTE: 
        1) num_frames = 0, corresponds to a "real-time" case in which each input
           block corresponds to [hop] samples.
        2) num_frames > 0, requires [(num_frames-1)*hop + N] samples as the last
           frame must contain [N] samples.
    """

    def __init__(self, N, hop=None, analysis_window=None, 
        synthesis_window=None, channels=1, transform='numpy', streaming=False,
        **kwargs):

        # initialize parameters
        self.num_samples = N            # number of samples per frame
        self.num_channels = channels    # number of channels
        if hop is not None:             # hop size --> number of input samples
            self.hop = hop  
        else:
            self.hop = self.num_samples

        # analysis and synthesis window
        self.analysis_window = analysis_window
        self.synthesis_window = synthesis_window

        # prepare variables for DFT object
        self.transform = transform
        self.nfft = self.num_samples   # differ when there is zero-padding
        self.nbin = self.nfft // 2 + 1

        # initialize filter + zero padding --> use set_filter
        self.zf = 0
        self.zb = 0
        self.H = None           # filter frequency spectrum
        self.H_multi = None     # for multiple frames

        # check keywords
        if 'num_frames' in kwargs.keys():
            self.fixed_input = True
            num_frames = kwargs['num_frames']
            if num_frames < 0:
                raise ValueError('num_frames must be non-negative!')
            self.num_frames = num_frames
        else:
            self.fixed_input = False
            self.num_frames = 0

        # allocate all the required buffers
        self.streaming = streaming
        self._make_buffers()



    def _make_buffers(self):
        """
        Allocate memory for internal buffers according to FFT size, number of
        channels, and number of frames.
        """

        # state variables
        self.n_state = self.num_samples - self.hop
        self.n_state_out = self.nfft - self.hop

        # make DFT object
        self.dft = DFT(nfft=self.nfft,
                       D=self.num_channels,
                       analysis_window=self.analysis_window,
                       synthesis_window=self.synthesis_window,
                       transform=self.transform)
        """
        Need to "squeeze" in case num_channels=1 as the FFTW package can only
        take 1D array for 1D DFT.
        """
        # The input buffer, float32 for speed!
        self.fft_in_buffer = np.squeeze(np.zeros((self.nfft, self.num_channels), 
            dtype=np.float32))
        #  a number of useful views on the input buffer
        self.fft_in_state = self.fft_in_buffer[self.zf:self.zf+self.n_state,]
        self.fresh_samples = self.fft_in_buffer[self.zf+self.n_state:\
                                            self.zf+self.n_state+self.hop,]
        self.old_samples = self.fft_in_buffer[self.zf+self.hop:
                                            self.zf+self.hop+self.n_state,]

        # state buffer
        self.x_p = np.squeeze(np.zeros((self.n_state, self.num_channels), 
            dtype=np.float32))
        # prev reconstructed samples
        self.y_p = np.squeeze(np.zeros((self.n_state_out, self.num_channels), 
            dtype=np.float32))
        # output samples
        self.out = np.squeeze(np.zeros((self.hop,self.num_channels), 
            dtype=np.float32))

        # if fixed number of frames to process
        if self.fixed_input:
            if self.num_frames==0:
                self.X = np.squeeze(np.zeros((self.nbin,self.num_channels), 
                    dtype=np.complex64))
            else:
                self.X = np.squeeze(
                    np.zeros((self.num_frames,self.nbin,self.num_channels), 
                    dtype=np.complex64))
                # DFT object for multiple frames
                self.dft_frames = DFT(nfft=self.nfft,D=self.num_frames,
                                    analysis_window=self.analysis_window,
                                    synthesis_window=self.synthesis_window,
                                    transform=self.transform)
        else: # we will allocate these on-the-fly
            self.X = None
            self.dft_frames = None


    def reset(self):
        """
        Reset state variables. Necesary after changing or setting the filter 
        or zero padding.
        """

        if self.num_channels==1:
            self.fft_in_buffer[:] = 0.
            self.x_p[:] = 0.
            self.y_p[:] = 0.
            self.X[:] = 0.
            self.out[:] = 0.
        else:
            self.fft_in_buffer[:,:] = 0.
            self.x_p[:,:] = 0.
            self.y_p[:,:] = 0.
            self.X[:,:] = 0.
            self.out[:,:] = 0.


    def zero_pad_front(self, zf):
        """
        Set zero-padding at beginning of frame.
        """
        self.zf = zf
        self.nfft = self.num_samples+self.zb+self.zf
        self.nbin = self.nfft//2+1
        if self.analysis_window is not None:
            self.analysis_window = np.concatenate((np.zeros(zf), 
                self.analysis_window))
        if self.synthesis_window is not None:
            self.synthesis_window = np.concatenate((np.zeros(zf), 
                self.synthesis_window))

        # We need to reallocate buffers after changing zero padding
        self._make_buffers()


    def zero_pad_back(self, zb):
        """
        Set zero-padding at end of frame.
        """
        self.zb = zb
        self.nfft = self.num_samples+self.zb+self.zf
        self.nbin = self.nfft//2+1
        if self.analysis_window is not None:
            self.analysis_window = np.concatenate((self.analysis_window, 
                np.zeros(zb)))
        if self.synthesis_window is not None:
            self.synthesis_window = np.concatenate((self.synthesis_window, 
                np.zeros(zb)))

        # We need to reallocate buffers after changing zero padding
        self._make_buffers()


    def set_filter(self, coeff, zb=None, zf=None, freq=False):
        """
        Set time-domain FIR filter with appropriate zero-padding.
        Frequency spectrum of the filter is computed and set for the object. 
        There is also a check for sufficient zero-padding.

        Parameters
        -----------
        coeff : numpy array 
            Filter in time domain.
        zb : int
            Amount of zero-padding added to back/end of frame.
        zf : int
            Amount of zero-padding added to front/beginning of frame.
        freq : bool
            Whether or not given coefficients (coeff) are in the frequency domain.
        """
        # apply zero-padding
        if zb is not None:
            self.zero_pad_back(zb)
        if zf is not None:
            self.zero_pad_front(zf)
        if not freq:
            # compute filter magnitude and phase spectrum
            self.H = np.complex64(np.fft.rfft(coeff, self.nfft, axis=0))
            # check for sufficient zero-padding
            if self.nfft < (self.num_samples+len(coeff)-1):
                raise ValueError('Insufficient zero-padding for chosen number of samples per frame (L) and filter length (h). Require zero-padding such that new length is at least (L+h-1).')
        else:
            if len(coeff)!=self.nbin:
                raise ValueError('Invalid length for frequency domain coefficients.')
            self.H = coeff

        # prepare filter if fixed input case
        if self.fixed_input:
            if self.num_channels == 1:
                self.H_multi = np.tile(self.H,(self.num_frames,1))
            else:
                self.H_multi = np.tile(self.H,(self.num_frames,1,1))
                # self.H_multi = np.swapaxes(self.H_multi,0,1)



    def analysis(self, x):
        """
        Parameters
        -----------
        x  : 2D numpy array, [samples, channels]
            Time-domain signal.
        """

        # ----check correct number of channels
        x_shape = x.shape
        if self.num_channels > 1:
            if len(x_shape) < 1:   # received mono
                raise ValueError("Received 1-channel signal. Expecting %d channels." \
                    % (self.num_channels))
            if x_shape[1] != self.num_channels:
                raise ValueError("Incorrect number of channels. Received %d, expecting %d." \
                    % (x_shape[1], self.num_channels))
        else:   # expecting mono
            if len(x_shape) > 1:    # received multi-channel
                raise ValueError("Received %d channels; expecting 1D mono signal." \
                    % (x_shape[1]))

        # ----check number of frames
        if self.streaming:  # need integer multiple of hops

            if self.fixed_input:
                if x_shape[0] != self.num_frames*self.hop:
                    raise ValueError(\
                        'Input must be of length %d; received %d samples.' \
                        % (self.num_frames*self.hop, x_shape[0]))
            else:
                self.num_frames = int(np.ceil(x_shape[0]/self.hop))
                extra_samples = (self.num_frames*self.hop)-x_shape[0]
                if extra_samples:
                    warnings.warn("Received %d samples. Appending  %d zeros for integer multiple of hops." \
                        % (x_shape[0],
                            extra_samples))
                    x = np.concatenate((x, 
                            np.squeeze(
                                np.zeros((extra_samples,self.num_channels)))
                            ))

        # non-streaming
        # need at least num_samples for last frame
        # e.g.[hop|hop|...|hop|num_samples]
        else:

            if self.fixed_input:
                if x_shape[0]!=(self.hop*(self.num_frames-1)+self.num_samples):
                    raise ValueError('Input must be of length %d; received %d samples.' 
                        % ((self.hop*(self.num_frames-1)+ self.num_samples), 
                        x_shape[0]))
            else:
                if x_shape[0] < self.num_samples:
                    # raise ValueError('Not enough samples. Received %d; need \
                    #     at least %d.' % (x_shape[0],self.num_samples))
                    extra_samples = self.num_samples - x_shape[0]
                    warnings.warn("Not enough samples. Received %d; appending %d zeros for full valid frame." \
                        % (x_shape[0],
                            extra_samples))
                    x = np.concatenate((x, 
                            np.squeeze(
                                np.zeros((extra_samples,self.num_channels)))
                            ))
                    self.num_frames = 1
                else:

                    # calculate num_frames and append zeros if necessary
                    self.num_frames = \
                        int(np.ceil((x_shape[0]-self.num_samples)/self.hop) + 1)
                    extra_samples = ((self.num_frames-1)*self.hop+
                        self.num_samples)-x_shape[0]
                    if extra_samples:
                        warnings.warn("Received %d samples. Appending %d zeros for integer multiple of hops." \
                            % (x_shape[0], extra_samples))
                        x = np.concatenate((x, 
                                np.squeeze(
                                    np.zeros((extra_samples,self.num_channels)))
                                ))

        # ----allocate memory if necessary
        if not self.fixed_input:
            self.X = np.squeeze(np.zeros((self.num_frames,self.nbin,
                    self.num_channels), dtype=np.complex64))
            self.dft_frames = DFT(nfft=self.nfft,D=self.num_frames,
                    analysis_window=self.analysis_window,
                    synthesis_window=self.synthesis_window,
                    transform=self.transform)

        # ----use appropriate function
        if self.streaming:
            self._analysis_streaming(x)
        else:
            self.reset()
            self._analysis_non_streaming(x)
                
        return self.X


    def _analysis_single(self, x_n):
        """
        Transform new samples to STFT domain for analysis.

        Parameters
        -----------
        x_n : numpy array
            [self.hop] new samples
        """

        # correct input size check in: dft.analysis()
        self.fresh_samples[:,] = x_n[:,]  # introduce new samples
        self.x_p[:,] = self.old_samples   # save next state

        # apply DFT to current frame
        self.X[:] = self.dft.analysis(self.fft_in_buffer)

        # shift backwards in the buffer the state
        self.fft_in_state[:,] = self.x_p[:,]


    def _analysis_streaming(self, x):
        """
        STFT analysis for streaming case in which we expect
        [num_frames*hop] samples
        """

        if self.num_frames==1:
            self._analysis_single(x)
        else:
            n = 0
            for k in range(self.num_frames):
                # introduce new samples
                self.fresh_samples[:,] = x[n:n+self.hop,]  
                # save next state
                self.x_p[:,] = self.old_samples   

                # apply DFT to current frame
                self.X[k,] = self.dft.analysis(self.fft_in_buffer)

                # shift backwards in the buffer the state
                self.fft_in_state[:,] = self.x_p[:,]

                n += self.hop

            # ## ----- STRIDED WAY
            # #USE PREVIOUS SAMPLES!
            # # print(self.old_samples.shape)
            # # print(x.shape)
            # x = np.concatenate((self.old_samples,x))
            # # print(x.shape)
            # new_strides = (x.strides[0],self.hop * x.strides[0])
            # new_shape = (self.num_samples,self.num_frames)

            # if self.num_channels > 1:
            #     for c in range(self.num_channels):

            #         y = _as_strided(x[:,c], shape=new_shape, strides=new_strides)
            #         y = np.concatenate((np.zeros((self.zf,self.num_frames)), y, 
            #                             np.zeros((self.zb,self.num_frames))))
            #         self.X[:,:,c] = self.dft_frames.analysis(y).T

            #         # store last frame
            #         self.fft_in_buffer[:,c] = y[:,-1]
            #         # self.fft_in_state[:,c] = self.old_samples[:,c]

            # else:

            #     y = _as_strided(x, shape=new_shape, strides=new_strides)
            #     y = np.concatenate((np.zeros((self.zf,self.num_frames)), y, 
            #                         np.zeros((self.zb,self.num_frames))))
            #     self.X[:] = self.dft_frames.analysis(y).T

            #     # store last frame
            #     self.fft_in_buffer[:] = y[:,-1]
            #     # self.fft_in_state[:] = self.old_samples[:]



    def _analysis_non_streaming(self, x):
        """
        STFT analysis for non-streaming case in which we expect
        [(num_frames-1)*hop+num_samples] samples
        """

        ## ----- STRIDED WAY
        new_strides = (x.strides[0],self.hop * x.strides[0])
        new_shape = (self.num_samples,self.num_frames)

        if self.num_channels > 1:
            for c in range(self.num_channels):

                y = _as_strided(x[:,c], shape=new_shape, strides=new_strides)
                y = np.concatenate((np.zeros((self.zf,self.num_frames)), y, 
                                    np.zeros((self.zb,self.num_frames))))

                if self.num_frames==1:
                    self.X[:,c] = self.dft_frames.analysis(y[:,0]).T
                else:
                    self.X[:,:,c] = self.dft_frames.analysis(y).T
        else:

            y = _as_strided(x, shape=new_shape, strides=new_strides)
            y = np.concatenate((np.zeros((self.zf,self.num_frames)), y, 
                                np.zeros((self.zb,self.num_frames))))

            if self.num_frames==1:
                self.X[:] = self.dft_frames.analysis(y[:,0]).T
            else:
                self.X[:] = self.dft_frames.analysis(y).T


    def _check_input_frequency_dimensions(self, X):
        """
        Ensure that given frequency data is valid, i.e. number of channels and
        number of frequency bins.

        If fixed_input=True, ensure expected number of frames. Otherwise, infer 
        from given data.

        Axis order of X should be : [frames, frequencies, channels]
        """

        # check number of frames and correct number of bins
        X_shape = X.shape
        if len(X_shape)==1:  # single channel, one frame
            num_frames = 1
        elif len(X_shape)==2 and self.num_channels>1: # multi-channel, one frame
            num_frames = 1
        elif len(X_shape)==2 and self.num_channels==1: # single channel, multiple frames
            num_frames = X_shape[0]
        elif len(X_shape)==3 and self.num_channels>1: # multi-channel, multiple frames
            num_frames = X_shape[0]
        else:
            raise ValueError("Invalid input shape.")

        # check number of bins
        if num_frames == 1:
            if X_shape[0]!=self.nbin:
                raise ValueError('Invalid number of frequency bins! Expecting %d, got %d'
                    % (self.nbin,X_shape[0]))
        else:
            if X_shape[1]!=self.nbin:
                raise ValueError('Invalid number of frequency bins! Expecting %d, got %d'
                    % (self.nbin,X_shape[0]))

        # check number of frames, if fixed input size
        if self.fixed_input:
            if num_frames != self.num_frames:
                raise ValueError('Input must have %d frames!', 
                    self.num_frames)
            self.X[:] = X  # reset if size is alright
        else:
            self.X = X
            self.num_frames = num_frames



    def process(self, X=None):

        """
        Parameters
        -----------
        X  : numpy array
            X can take on multiple shapes:
            1) (N,) if it is single channel and only one frame
            2) (N,D) if it is multi-channel and only one frame
            3) (F,N) if it is single channel but multiple frames
            4) (F,N,D) if it is multi-channel and multiple frames

        Returns
        -----------
        x_r : numpy array
            Reconstructed time-domain signal.

        """

        # check that there is filter
        if self.H is None:
            warnings.warn("No filter is set! Exiting...")
            return

        if X is not None:
            self._check_input_frequency_dimensions(X)

        # use appropriate function
        if self.num_frames==1:
            self._process_single()
        elif self.num_frames>1:
            self._process_multiple()

        return self.X


    def _process_single(self):

        np.multiply(self.X, self.H, self.X)


    def _process_multiple(self):

        if not self.fixed_input:
            if self.num_channels == 1:
                self.H_multi = np.tile(self.H,(self.num_frames,1))
            else:
                self.H_multi = np.tile(self.H,(self.num_frames,1,1))

        np.multiply(self.X, self.H_multi, self.X)


    def synthesis(self, X=None):

        """
        Parameters
        -----------
        X  : numpy array of frequency content
            X can take on multiple shapes:
            1) (N,) if it is single channel and only one frame
            2) (N,D) if it is multi-channel and only one frame
            3) (F,N) if it is single channel but multiple frames
            4) (F,N,D) if it is multi-channel and multiple frames
            where:
            - F is the number of frames
            - N is the number of frequency bins
            - D is the number of channels


        Returns
        -----------
        x_r : numpy array
            Reconstructed time-domain signal.

        """

        if X is not None:
            self._check_input_frequency_dimensions(X)

        # use appropriate function
        if self.num_frames==1:
            return self._synthesis_single()
        elif self.num_frames>1:
            return self._synthesis_multiple()


    def _synthesis_single(self):
        """
        Transform to time domain and reconstruct output with overlap-and-add.

        Returns
        -------
        numpy array
            Reconstructed array of samples of length <self.hop> (Optional)
        """

        # apply IDFT to current frame
        self.dft.synthesis(self.X)

        return self._overlap_and_add()



    def _synthesis_multiple(self):
        """
        Apply STFT analysis to multiple frames.

        Returns
        -----------
        x_r : numpy array
            Recovered signal.

        """

        # synthesis + overlap and add
        if self.num_channels > 1:

            x_r = np.zeros((self.num_frames*self.hop,self.num_channels), 
                dtype=np.float32)

            n = 0
            for f in range(self.num_frames):

                # appy IDFT to current frame and reconstruct output
                x_r[n:n+self.hop,] = \
                    self._overlap_and_add(self.dft.synthesis(self.X[f,:,:]))
                n += self.hop

        else:

            x_r = np.zeros(self.num_frames*self.hop, dtype=np.float32)

            # treat number of frames as the multiple channels for DFT
            if not self.fixed_input:
                self.dft_frames = DFT(nfft=self.nfft,D=self.num_frames,
                    analysis_window=self.analysis_window,
                    synthesis_window=self.synthesis_window,
                    transform=self.transform)

            # back to time domain
            mx = self.dft_frames.synthesis(self.X.T)

            # overlap and add
            n = 0
            for f in range(self.num_frames):
                x_r[n:n+self.hop,] = self._overlap_and_add(mx[:,f])
                n += self.hop

        return x_r


    def _overlap_and_add(self, x=None):

        if x is None:
            x = self.dft.x

        self.out[:,] = x[0:self.hop,]  # fresh output samples

        # add state from previous frames when overlap is used
        if self.n_state_out > 0:
            m = np.minimum(self.hop, self.n_state_out)
            self.out[:m,] += self.y_p[:m,]
            # update state variables
            self.y_p[:-self.hop,] = self.y_p[self.hop:,]  # shift out left
            self.y_p[-self.hop:,] = 0.
            self.y_p[:,] += x[-self.n_state_out:,]

        return self.out


" ---------------------------------------------------------------------------- "
" --------------- One-shot functions to avoid creating object. --------------- "
" ---------------------------------------------------------------------------- "
# Authors: Robin Scheibler, Ivan Dokmanic, Sidney Barthe

def analysis(x, L, hop, transform=np.fft.fft, win=None, zp_back=0, zp_front=0):
    '''
    Parameters
    ----------
    x: 
        input signal
    L: 
        frame size
    hop: 
        shift size between frames
    transform: 
        the transform routine to apply (default FFT)
    win: 
        the window to apply (default None)
    zp_back: 
        zero padding to apply at the end of the frame
    zp_front: 
        zero padding to apply at the beginning of the frame

    Returns
    -------
    The STFT of x
    '''

    # the transform size
    N = L + zp_back + zp_front

    # window needs to be same size as transform
    if (win is not None and len(win) != N):
        print('Window length need to be equal to frame length + zero padding.')
        sys.exit(-1)

    # reshape
    new_strides = (hop * x.strides[0], x.strides[0])
    new_shape = ((len(x) - L) // hop + 1, L)
    y = _as_strided(x, shape=new_shape, strides=new_strides)

    # add the zero-padding
    y = np.concatenate(
        (np.zeros(
            (y.shape[0], zp_front)), y, np.zeros(
            (y.shape[0], zp_back))), axis=1)

    # apply window if needed
    if (win is not None):
        y = win * y
        # y = np.expand_dims(win, 0)*y

    # transform along rows
    Z = transform(y, axis=1)

    # apply transform
    return Z


# inverse STFT
def synthesis(X, L, hop, transform=np.fft.ifft, win=None, zp_back=0, zp_front=0):

    # the transform size
    N = L + zp_back + zp_front

    # window needs to be same size as transform
    if (win is not None and len(win) != N):
        print('Window length need to be equal to frame length + zero padding.')
        sys.exit(-1)

    # inverse transform
    iX = transform(X, axis=1)
    if (iX.dtype == 'complex128'):
        iX = np.real(iX)

    # apply synthesis window if necessary
    if (win is not None):
        iX *= win

    # create output signal
    x = np.zeros(X.shape[0] * hop + (L - hop) + zp_back + zp_front)

    # overlap add
    for i in range(X.shape[0]):
        x[i * hop:i * hop + N] += iX[i]

    return x


# a routine for long convolutions using overlap add method
def overlap_add(in1, in2, L):

    # set the shortest sequence as the filter
    if (len(in1) > len(in2)):
        x = in1
        h = in2
    else:
        h = in1
        x = in2

    # filter length
    M = len(h)

    # FFT size
    N = L + M - 1

    # frequency domain filter (zero-padded)
    H = np.fft.rfft(h, N)

    # prepare output signal
    ylen = int(np.ceil(len(x) / float(L)) * L + M - 1)
    y = np.zeros(ylen)

    # overlap add
    i = 0
    while (i < len(x)):
        y[i:i + N] += np.fft.irfft(np.fft.rfft(x[i:i + L], N) * H, N)
        i += L

    return y[:len(x) + M - 1]


def spectroplot(Z, N, hop, fs, fdiv=None, tdiv=None,
                vmin=None, vmax=None, cmap=None, interpolation='none', colorbar=True):

    import matplotlib.pyplot as plt

    plt.imshow(
        20 * np.log10(np.abs(Z[:N // 2 + 1, :])),
        aspect='auto',
        origin='lower',
        vmin=vmin, vmax=vmax, cmap=cmap, interpolation=interpolation)

    # label y axis correctly
    plt.ylabel('Freq [Hz]')
    yticks = plt.getp(plt.gca(), 'yticks')
    plt.setp(plt.gca(), 'yticklabels', np.round(yticks / float(N) * fs))
    if (fdiv is not None):
        tick_lbls = np.arange(0, fs / 2, fdiv)
        tick_locs = tick_lbls * N / fs
        plt.yticks(tick_locs, tick_lbls)

    # label x axis correctly
    plt.xlabel('Time [s]')
    xticks = plt.getp(plt.gca(), 'xticks')
    plt.setp(plt.gca(), 'xticklabels', xticks / float(fs) * hop)
    if (tdiv is not None):
        unit = float(hop) / fs
        length = unit * Z.shape[1]
        tick_lbls = np.arange(0, int(length), tdiv)
        tick_locs = tick_lbls * fs / hop
        plt.xticks(tick_locs, tick_lbls)

    if colorbar is True:
        plt.colorbar(orientation='horizontal')

