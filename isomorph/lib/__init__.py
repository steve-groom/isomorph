"""Library blocks: ordinary @blocks in the house style, and the one
generator that wires them.

Nothing here is inferred. Every block is a module with the name it has
in this package, every register in it has a name a fitter report will
show, and the generator leaves only instances and wires behind. See
DATAFLOW.md for what this deliberately is not.
"""
from .stream_pipe import stream_pipe
from .stream_fifo import stream_fifo
from .stream_fork import stream_fork
from .stream_join import stream_join
from .ram_block import ram_block
from .stream_to_memory import stream_to_memory
from .stream_from_memory import stream_from_memory
from .pipeline import pipeline

__all__ = ['stream_pipe', 'stream_fifo', 'stream_fork', 'stream_join',
           'ram_block', 'stream_to_memory', 'stream_from_memory',
           'pipeline']
