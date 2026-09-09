"""JSON sidecar of analysed modules (SPEC 7, SIM_PLAN S1)."""
import json
import os

from .emit_c99 import ff_driven_names


def sidecar (modules):
    """A JSON-serialisable dict for tools, ctypes and sim-report."""
    if not modules:
        return {'version': 1, 'top': '', 'modules': []}
    return {
        'version': 1,
        'top': modules[-1].name,
        'modules': [module_sidecar(m) for m in modules],
    }


def write_sidecar (modules, path):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok = True)
    with open(path, 'w', encoding = 'ascii', newline = '\n') as f:
        json.dump(sidecar(modules), f, indent = 2)
        f.write('\n')
    return path


def module_sidecar (m):
    ff = sorted(ff_driven_names(m))
    enums = {}
    for name, e in m.enums.items():
        enums[name] = {
            'width': e.width,
            'encoding': getattr(e, 'encoding', 'auto'),
            'members': [{'name': x.name, 'value': int(x.value)}
                        for x in e.members],
        }
    return {
        'name': m.name,
        'block': m.block,
        'parameters': {k: _json_value(v) for k, v in m.parameters.items()},
        'ports': [port_sidecar(p) for p in m.ports],
        'signals': [signal_sidecar(s) for s in m.signals],
        'constants': {k: int(v) for k, v in m.constants.items()},
        'enums': enums,
        'ff': ff,
        'processes': [process_sidecar(p) for p in m.processes],
        'instances': [instance_sidecar(i) for i in m.instances],
    }


def port_sidecar (p):
    return {
        'name': p.name,
        'width': p.width,
        'direction': p.direction,
        'kind': p.kind,
        'array': p.array,
        'type': type_sidecar(p.type),
    }


def signal_sidecar (s):
    return {
        'name': s.name,
        'width': s.width,
        'kind': s.kind,
        'array': s.array,
        'type': type_sidecar(s.type),
        'attributes': dict(s.attributes) if s.attributes else {},
    }


def process_sidecar (p):
    return {
        'name': p.name,
        'kind': p.kind,
        'clock': p.clock,
        'polarity': p.polarity,
    }


def instance_sidecar (i):
    return {
        'name': i.name,
        'module': i.module,
        'ports': list(i.ports.keys()),
    }


def type_sidecar (t):
    if t is None:
        return None
    fields = getattr(t, 'fields', None)
    if fields is not None:
        return {
            'kind': 'struct',
            'name': getattr(t, 'name', None),
            'fields': dict(fields),
            'width': getattr(t, 'width', None),
        }
    members = getattr(t, 'members', None)
    if members is not None:
        return {
            'kind': 'enum',
            'name': getattr(t, 'name', None),
            'width': getattr(t, 'width', None),
        }
    return None


def _json_value (v):
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        return v
    name = getattr(v, 'name', None)
    if name is not None:
        return name
    return str(v)
