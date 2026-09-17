"""pipeline(): a chain of stream blocks, wired at elaboration.

    chain = pipeline(i_stream, o_stream, [
        ('gain', gain_stage, {'GAIN': 3}),
        ('pipe', stream_pipe),
        ('fifo', stream_fifo, {'DEPTH': 4}),
    ], i_clock, i_reset)

builds the link streams between the stages, binds i_clock and i_reset
to every stage whose signature has them, and returns a namespace of
what it made: chain.gain is the gain_stage instance, chain.gain_out
the stream leaving it. Held in a block local, those are named by
their path - the instance chain_gain, the wire chain_gain_out_valid -
so the fitter report reads as the Python does.

This runs once and leaves only instances and wires behind. It is the
generate-by-Python this converter is built on, applied to
dataflow, and it reads
nothing it was not given: a stage is a block, a name and parameters,
and anything else is an error naming the stage. A stage whose output
is wider or narrower than its input says so with out_width.
"""
import inspect
import sys
from types import SimpleNamespace

from isomorph.lib.ports import stream
from ..signal import IsomorphError


def pipeline (i_stream, o_stream, stages, i_clock = None, i_reset = None):
    if not stages:
        raise IsomorphError('pipeline(): no stages')
    # every stage is an instance made on the caller's line, so that it
    # sits among the caller's other instances in the emitted order and
    # the comment above the call is the comment above the chain
    line = sys._getframe(1).f_lineno
    made = {}
    upstream = i_stream
    for index, entry in enumerate(stages):
        name, stage_block, params = _entry(entry)
        if name in made:
            raise IsomorphError(f'pipeline(): stage {name!r} is named twice')
        formals = inspect.signature(stage_block).parameters
        for needed in ('i_stream', 'o_stream'):
            if needed not in formals:
                raise IsomorphError(
                    f'pipeline(): stage {name!r} ({stage_block.__name__}) '
                    f'has no {needed} port')
        # out_width is for the link and is not the stage's to see
        width = _width(params, upstream)
        for key in params:
            if key not in formals:
                raise IsomorphError(
                    f'pipeline(): stage {name!r} ({stage_block.__name__}) '
                    f'takes no parameter {key!r}')
        last = (index == len(stages) - 1)
        if last:
            downstream = o_stream
            if width != len(o_stream.data):
                raise IsomorphError(
                    f'pipeline(): the last stage {name!r} says out_width '
                    f'= {width} and o_stream is {len(o_stream.data)} '
                    'bits wide')
        else:
            downstream = stream(width)
            made[f'{name}_out'] = downstream
        kwargs = dict(params)
        kwargs['i_stream'] = upstream
        kwargs['o_stream'] = downstream
        for pin, actual in (('i_clock', i_clock), ('i_reset', i_reset)):
            if pin in formals:
                if actual is None:
                    raise IsomorphError(
                        f'pipeline(): stage {name!r} takes {pin} and '
                        'pipeline() was not given one')
                kwargs[pin] = actual
        made[name] = stage_block(**kwargs)
        made[name].line = line
        upstream = downstream
    return SimpleNamespace(**made)


def _width (params, upstream):
    if 'out_width' in params:
        return int(params.pop('out_width'))
    return len(upstream.data)


def _entry (entry):
    """(name, block) or (name, block, params) -> the three, checked."""
    if not isinstance(entry, (list, tuple)) or len(entry) not in (2, 3):
        raise IsomorphError(
            f'pipeline(): a stage is (name, block) or (name, block, '
            f'params), not {entry!r}')
    name = entry[0]
    stage_block = entry[1]
    params = dict(entry[2]) if len(entry) == 3 else {}
    if not isinstance(name, str) or not name.isidentifier():
        raise IsomorphError(
            f'pipeline(): stage name {name!r} is not an identifier; it '
            'becomes the instance name')
    if not getattr(stage_block, 'is_block', False):
        raise IsomorphError(
            f'pipeline(): stage {name!r} is not a @block: {stage_block!r}')
    params.pop('i_stream', None)
    params.pop('o_stream', None)
    return name, stage_block, params
