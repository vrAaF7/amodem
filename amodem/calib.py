from . import common
from . import dsp
from . import sampling
from . import stream

import numpy as np
import itertools
import logging
import subprocess

log = logging.getLogger(__name__)


def volume_controller(cmd):
    def controller(level):
        assert 0 < level <= 1
        percent = 100 * level
        args = '{0} {1:.0f}%'.format(cmd, percent)
        log.debug('Setting volume to %7.3f%% -> "%s"', percent, args)
        subprocess.check_call(args=args, shell=True)
    return controller if cmd else (lambda level: None)


def send(config, dst, volume_cmd=None, gain=1.0, limit=None):
    volume_ctl = volume_controller(volume_cmd)
    volume_ctl(1.0)  # full scale output volume

    calibration_symbols = int(1.0 * config.Fs)
    t = np.arange(0, calibration_symbols) * config.Ts
    signals = [gain * np.sin(2 * np.pi * f * t) for f in config.frequencies]
    signals = [common.dumps(s) for s in signals]

    for signal in itertools.islice(itertools.cycle(signals), limit):
        dst.write(signal)


def frame_iter(config, src, frame_length):
    frame_size = frame_length * config.Nsym * config.sample_size
    omegas = 2 * np.pi * np.array(config.frequencies) / config.Fs

    while True:
        data = src.read(frame_size)
        if len(data) < frame_size:
            return
        data = common.loads(data)
        frame = data - np.mean(data)

        sampler = sampling.Sampler(frame)
        symbols = dsp.Demux(sampler, omegas, config.Nsym)

        symbols = np.array(list(symbols))
        coeffs = np.mean(np.abs(symbols) ** 2, axis=0) ** 0.5

        peak = np.max(np.abs(frame))
        total = np.sqrt(np.dot(frame, frame) / (0.5 * len(frame)))
        yield coeffs, peak, total


def detector(config, src, frame_length=200):

    errors = ['weak', 'strong', 'noisy']
    for coeffs, peak, total in frame_iter(config, src, frame_length):
        max_index = np.argmax(coeffs)
        freq = config.frequencies[max_index]
        rms = abs(coeffs[max_index])
        coherency = rms / total
        flags = [total > 0.1, peak < 1.0, coherency > 0.99]

        success = all(flags)
        if success:
            msg = 'good signal'
        else:
            msg = 'too {0} signal'.format(errors[flags.index(False)])

        yield common.AttributeHolder(dict(
            freq=freq, rms=rms, peak=peak, coherency=coherency,
            total=total, success=success, msg=msg
        ))


def volume_calibration(result_iterator, volume_ctl):
    min_level = 0.01
    max_level = 1.0
    level = 0.5
    step = 0.25

    target_level = 0.4  # not too strong, not too weak
    iters_per_update = 10  # update every 2 seconds

    for index, result in enumerate(itertools.chain([None], result_iterator)):
        if index % iters_per_update == 0:
            if index > 0:  # skip dummy (first result)
                sign = 1 if (result.total < target_level) else -1
                level = level + step * sign
                level = min(max(level, min_level), max_level)
                step = step * 0.5

            volume_ctl(level)  # should run "before" first actual iteration

        if index > 0:  # skip dummy (first result)
            yield result


def iter_window(iterable, size):
    block = []
    while True:
        item = next(iterable)
        block.append(item)
        block = block[-size:]
        if len(block) == size:
            yield block


def recv(config, src, verbose=False, volume_cmd=None, dump_audio=None):
    fmt = '{0.freq:6.0f} Hz: {0.msg:20s}'
    if verbose:
        fields = ['total', 'rms', 'coherency', 'peak']
        fmt += ', '.join('{0}={{0.{0}:.4f}}'.format(f) for f in fields)

    volume_ctl = volume_controller(volume_cmd)

    if dump_audio:
        src = stream.Dumper(src, dump_audio)
    result_iterator = detector(config=config, src=src)
    result_iterator = volume_calibration(result_iterator, volume_ctl)
    result_iterator = iter_window(result_iterator, size=3)
    for r in result_iterator:
        # don't log errors during frequency changes
        if r[0].success and r[2].success and r[0].freq != r[2].freq:
            r[1].msg = r[1].msg if r[1].success else 'frequency change'
        log.info(fmt.format(r[1]))
