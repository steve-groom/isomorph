"""Deterministic points of interest, picked out of a cycle log."""
import json
import os


def load_ndjson (path):
    rows = []
    with open(path, encoding = 'ascii') as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def reconstruct (rows):
    values = {}
    history = []
    for rec in rows:
        values.update({k: int(v) for k, v in rec.get('ch', {}).items()})
        history.append({'t': rec.get('t', rec.get('cycle', 0)),
                        'cycle': rec.get('cycle', 0),
                        'values': dict(values)})
    return history


def report (ndjson_path, sidecar_path = None, events_path = None,
            stuck_cycles = 64):
    rows = load_ndjson(ndjson_path)
    history = reconstruct(rows)
    sidecar = None
    if sidecar_path and os.path.isfile(sidecar_path):
        with open(sidecar_path, encoding = 'ascii') as f:
            sidecar = json.load(f)
    events = []
    events += reset_windows(history)
    events += fsm_transitions(history, sidecar)
    events += stuck_signals(history, stuck_cycles)
    if events_path and os.path.isfile(events_path):
        with open(events_path, encoding = 'ascii') as f:
            extra = json.load(f)
        if isinstance(extra, list):
            events += extra
    text = format_report(ndjson_path, history, events)
    return {
        'cycles': len(history),
        'events': events,
        'text': text,
    }


def reset_windows (history):
    events = []
    keys = []
    if history:
        for k in history[0]['values']:
            leaf = k.split('.')[-1]
            if 'reset' in leaf.lower():
                keys.append(k)
    for key in keys:
        prev = None
        start = None
        for rec in history:
            v = rec['values'].get(key)
            if v is None:
                continue
            if v and not prev:
                start = rec['cycle']
            if prev and not v and start is not None:
                events.append({
                    'kind': 'reset',
                    'signal': key,
                    'from_cycle': start,
                    'to_cycle': rec['cycle'],
                })
                start = None
            prev = v
    return events


def fsm_transitions (history, sidecar):
    events = []
    enum_signals = {}
    if sidecar:
        for m in sidecar.get('modules', []):
            enums = m.get('enums', {})
            for s in m.get('signals', []):
                if s.get('kind') != 'enum':
                    continue
                t = s.get('type') or {}
                ename = t.get('name')
                members = None
                if ename and ename in enums:
                    members = enums[ename]['members']
                if members is None:
                    continue
                lookup = {int(x['value']): x['name'] for x in members}
                enum_signals[s['name']] = lookup
    if not enum_signals or not history:
        return events
    prev = {}
    for rec in history:
        for hier, val in rec['values'].items():
            leaf = hier.split('.')[-1]
            if leaf not in enum_signals:
                continue
            lookup = enum_signals[leaf]
            if leaf in prev and prev[leaf] != val:
                events.append({
                    'kind': 'fsm',
                    'signal': hier,
                    'cycle': rec['cycle'],
                    'from': lookup.get(prev[leaf], prev[leaf]),
                    'to': lookup.get(val, val),
                })
            prev[leaf] = val
    return events


def stuck_signals (history, stuck_cycles):
    events = []
    if not history or stuck_cycles <= 0:
        return events
    last_change = {}
    last_val = {}
    for rec in history:
        for hier, val in rec['values'].items():
            if last_val.get(hier) != val:
                last_change[hier] = rec['cycle']
                last_val[hier] = val
    end = history[-1]['cycle']
    for hier, start in last_change.items():
        held = end - start
        if held >= stuck_cycles:
            leaf = hier.split('.')[-1]
            if leaf.startswith('i_'):
                continue
            events.append({
                'kind': 'stuck',
                'signal': hier,
                'value': last_val[hier],
                'for_cycles': held,
            })
    return events


def format_report (path, history, events):
    n = len(history)
    lines = [f'sim-report {path}', f'cycles {n}']
    if not events:
        lines.append('no events')
        return '\n'.join(lines) + '\n'
    for e in events:
        kind = e['kind']
        if kind == 'reset':
            lines.append(
                f"reset {e['signal']} cycles {e['from_cycle']}"
                f"-{e['to_cycle']}")
        elif kind == 'fsm':
            lines.append(
                f"fsm {e['signal']} cycle {e['cycle']} "
                f"{e['from']} -> {e['to']}")
        elif kind == 'stuck':
            lines.append(
                f"stuck {e['signal']} = {e['value']} "
                f"for {e['for_cycles']} cycles")
        elif kind == 'protocol':
            lines.append(
                f"protocol {e.get('check', '')} {e.get('rule', '')} "
                f"cycle {e.get('cycle', '?')}: {e.get('message', '')}")
        else:
            lines.append(str(e))
    return '\n'.join(lines) + '\n'
