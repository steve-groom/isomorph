"""IR interpreter: cycle-accurate smoke matching SPEC 6.

Walks the analysed IR. Not the original Python process functions.
tick() is eval, posedge, NBA commit, eval. Combinational loops that
do not settle in SETTLE_LIMIT passes are an error.
"""
from . import ir
from .emit_c99 import SETTLE_LIMIT, ff_driven_names
from .signal import IsomorphError


class SimError(IsomorphError):
    pass


def mask (width):
    if width <= 0:
        return 0
    return (1 << width) - 1


def split_index (name):
    if '[' in name and name.endswith(']'):
        base, rest = name.split('[', 1)
        try:
            return base, int(rest[:-1])
        except ValueError:
            return name, None
    return name, None


class Store:
    """One module instance: live values, nxt for ff, child instances."""

    def __init__ (self, module, by_name):
        self.m = module
        self.v = {}
        self.nxt = {}
        self.child = {}
        self.width = {}
        self.array = {}
        self.kind = {}
        self.type = {}
        ff = ff_driven_names(module)
        for p in module.ports:
            self._add(p.name, p.width, p.array, p.name in ff, p.kind, p.type)
        for s in module.signals:
            self._add(s.name, s.width, s.array, s.name in ff, s.kind, s.type,
                      reset = s.reset)
        for inst in module.instances:
            child = by_name[inst.module]
            self.child[inst.name] = Store(child, by_name)

    def _add (self, name, width, array, has_nxt, kind, typ, reset = None):
        self.width[name] = width
        self.array[name] = array
        self.kind[name] = kind
        self.type[name] = typ
        init = 0 if reset is None else int(reset) & mask(width)
        if array:
            self.v[name] = [init] * array
            if has_nxt:
                self.nxt[name] = [init] * array
        else:
            self.v[name] = init
            if has_nxt:
                self.nxt[name] = init

    def live_tuple (self):
        items = []
        for p in self.m.ports:
            items.append(_freeze(self.v[p.name]))
        for s in self.m.signals:
            items.append(_freeze(self.v[s.name]))
        for inst in self.m.instances:
            items.append(self.child[inst.name].live_tuple())
        return tuple(items)

    def walk_live (self, prefix = ''):
        """Yield (hier_name, width, value) for every live field."""
        for p in self.m.ports:
            yield from _walk_field(prefix, p.name, p.width, p.array,
                                   self.v[p.name])
        for s in self.m.signals:
            yield from _walk_field(prefix, s.name, s.width, s.array,
                                   self.v[s.name])
        for inst in self.m.instances:
            pfx = f'{prefix}{inst.name}.'
            yield from self.child[inst.name].walk_live(pfx)

    def resolve (self, path):
        parts = path.split('.')
        cur = self
        for p in parts[:-1]:
            if p not in cur.child:
                raise SimError(f'unknown instance {p} in {path}')
            cur = cur.child[p]
        return cur, parts[-1]


def _freeze (v):
    if isinstance(v, list):
        return tuple(v)
    return v


def _walk_field (prefix, name, width, array, value):
    if array:
        for i, elt in enumerate(value):
            yield f'{prefix}{name}[{i}]', width, elt
    else:
        yield f'{prefix}{name}', width, value


class Executor:
    def __init__ (self, modules):
        if not modules:
            raise SimError('no modules to execute')
        self.modules = modules
        self.by_name = {m.name: m for m in modules}
        self.top = modules[-1]
        self.store = Store(self.top, self.by_name)
        self.events = []
        self.settle_passes = 0

    def eval (self):
        if not self._eval_store(self.store):
            raise SimError(f'combinational loop in {self.top.name}')

    def posedge (self, clock = None, commit = True):
        self._posedge_store(self.store, clock)
        if commit:
            self._commit_store(self.store, None)
            self.eval()

    def commit (self):
        self._commit_store(self.store, None)
        self.eval()

    def tick (self, n = 1, clock = None):
        n = int(n)
        for _ in range(n):
            self.eval()
            self.posedge(clock)

    def get (self, path):
        store, name = self.store.resolve(path)
        base, idx = split_index(name)
        if base not in store.v:
            raise SimError(f'unknown signal {path}')
        val = store.v[base]
        if idx is not None:
            return val[idx]
        return val

    def set (self, path, value):
        store, name = self.store.resolve(path)
        base, idx = split_index(name)
        if base not in store.v:
            raise SimError(f'unknown signal {path}')
        w = store.width[base]
        value = int(value) & mask(w)
        if idx is not None:
            store.v[base][idx] = value
        elif isinstance(store.v[base], list):
            raise SimError(f'array {path} assigned without an index')
        else:
            store.v[base] = value

    def _eval_store (self, store):
        for n in range(SETTLE_LIMIT):
            prev = store.live_tuple()
            self._comb_once(store)
            self.settle_passes = n + 1
            if store.live_tuple() == prev:
                if n + 1 >= SETTLE_LIMIT - 4:
                    self.events.append({
                        'kind': 'settle_near',
                        'module': store.m.name,
                        'passes': n + 1,
                    })
                return True
        self.events.append({
            'kind': 'comb_loop',
            'module': store.m.name,
        })
        return False

    def _comb_once (self, store):
        m = store.m
        for inst in m.instances:
            self._copy_in(store, inst)
            child = store.child[inst.name]
            if not self._eval_store(child):
                raise SimError(f'combinational loop in {child.m.name}')
            self._copy_out(store, inst)
        ctx = Ctx(store, False)
        for a in m.assigns:
            assign_target(ctx, a.target, eval_expr(ctx, a.value))
        for p in m.processes:
            if p.kind == 'comb':
                run_stmts(ctx, p.body)
        self._async_resets(store)

    def _async_resets (self, store):
        """An asynchronous reset acts while it is held, not only at the
        clock edge. This is a cycle simulator, so the closest honest
        model is to apply the reset branch on every settle pass while
        the reset is asserted, to the live value and to the pending one
        so a later commit cannot undo it."""
        for p in store.m.processes:
            if p.kind != 'ff' or not p.reset:
                continue
            value = store.v.get(p.reset)
            if value is None:
                continue
            asserted = bool(value) if p.reset_polarity == 'pos' \
                else not bool(value)
            if not asserted:
                continue
            branch = p.body[0].branches[0][1]
            run_stmts(Ctx(store, False), branch)
            run_stmts(Ctx(store, True), branch)

    def _posedge_store (self, store, clock):
        ctx = Ctx(store, True)
        for p in store.m.processes:
            if p.kind != 'ff':
                continue
            if clock is not None and p.clock != clock:
                continue
            run_stmts(ctx, p.body)
        for inst in store.m.instances:
            child_clock = clock
            if clock is not None:
                child_clock = self._map_clock(store, inst, clock)
                if child_clock is None:
                    continue
            self._posedge_store(store.child[inst.name], child_clock)

    def _map_clock (self, store, inst, parent_clock):
        child = store.child[inst.name]
        for formal, actual in inst.ports.items():
            if actual is None:
                continue
            if (getattr(actual, 'op', None) == 'ref'
                    and actual.value == parent_clock):
                if any(p.kind == 'ff' and p.clock == formal
                       for p in child.m.processes):
                    return formal
        if any(p.kind == 'ff' and p.clock == parent_clock
               for p in child.m.processes):
            return parent_clock
        return None

    def _commit_store (self, store, clock):
        for name, nxt in store.nxt.items():
            if isinstance(nxt, list):
                store.v[name] = list(nxt)
            else:
                store.v[name] = nxt
        for inst in store.m.instances:
            self._commit_store(store.child[inst.name], clock)

    def _copy_in (self, store, inst):
        child = store.child[inst.name]
        directions = {p.name: p.direction for p in child.m.ports}
        widths = {p.name: p.width for p in child.m.ports}
        ctx = Ctx(store, False)
        for formal, actual in inst.ports.items():
            if actual is None or directions.get(formal) == 'out':
                continue
            val = eval_expr(ctx, actual)
            w = widths.get(formal, actual.width)
            if isinstance(child.v.get(formal), list):
                continue
            child.v[formal] = int(val) & mask(w)

    def _copy_out (self, store, inst):
        child = store.child[inst.name]
        directions = {p.name: p.direction for p in child.m.ports}
        ctx = Ctx(store, False)
        for formal, actual in inst.ports.items():
            if actual is None or directions.get(formal) != 'out':
                continue
            val = child.v[formal]
            if isinstance(val, list):
                continue
            assign_target(ctx, actual, val)


class Ctx:
    def __init__ (self, store, ff_write):
        self.store = store
        self.m = store.m
        self.ff_write = ff_write
        self.ff_names = ff_driven_names(store.m)
        self.locals = {}
        self.loop_vars = {}
        self.int_names = {}
        for n, v in store.m.constants.items():
            self.int_names[n] = int(v)
        for n, v in store.m.parameters.items():
            if isinstance(v, bool):
                self.int_names[n] = int(v)
            elif isinstance(v, int):
                self.int_names[n] = v
        self.fn_return = None


def run_stmts (ctx, body):
    for s in body:
        if ctx.fn_return is not None:
            return
        run_stmt(ctx, s)


def run_stmt (ctx, s):
    if isinstance(s, ir.Assign):
        assign_target(ctx, s.target, eval_expr(ctx, s.value))
    elif isinstance(s, ir.If):
        for cond, body in s.branches:
            if cond is None or eval_cond(ctx, cond):
                run_stmts(ctx, body)
                break
    elif isinstance(s, ir.For):
        for i in range(s.start, s.stop):
            ctx.loop_vars[s.var] = i
            run_stmts(ctx, s.body)
            if ctx.fn_return is not None:
                break
        ctx.loop_vars.pop(s.var, None)
    elif isinstance(s, ir.Match):
        subj = eval_expr(ctx, s.subject)
        for pat, body in s.arms:
            if pat is None:
                run_stmts(ctx, body)
                break
            if pat.op == 'bits':
                bit_mask, val = bits_mask(pat.value)
                if (subj & bit_mask) == val:
                    run_stmts(ctx, body)
                    break
            elif eval_expr(ctx, pat) == subj:
                run_stmts(ctx, body)
                break
    elif isinstance(s, ir.Assert):
        if not eval_cond(ctx, s.cond):
            raise SimError(f'assertion failed at line {s.line}')
    elif isinstance(s, ir.Return):
        ctx.fn_return = eval_expr(ctx, s.value) & mask(s.value.width)


def assign_target (ctx, t, value):
    value = int(value) & mask(t.width)
    if t.op == 'bit':
        idx = eval_index(ctx, t.args[1])
        base = t.args[0]
        if t.width != 1 or _is_array_ref(ctx, base):
            _store_indexed(ctx, base, idx, value, t.width)
            return
        cur = eval_expr(ctx, base, lhs = True)
        new = (cur & ~(1 << idx)) | ((value & 1) << idx)
        _store_expr(ctx, base, new)
        return
    if t.op == 'slice':
        lo = t.value[1]
        if len(t.args) >= 3 and t.args[2].op == 'const':
            lo = int(t.args[2].value)
        w = t.width
        cur = eval_expr(ctx, t.args[0], lhs = True)
        m = mask(w)
        new = (cur & ~(m << lo)) | ((value & m) << lo)
        _store_expr(ctx, t.args[0], new)
        return
    if t.op == 'field':
        lo = int(t.args[1].value) if len(t.args) > 1 else 0
        w = t.width
        cur = eval_expr(ctx, t.args[0], lhs = True)
        m = mask(w)
        new = (cur & ~(m << lo)) | ((value & m) << lo)
        _store_expr(ctx, t.args[0], new)
        return
    if t.op == 'ref':
        store_ref(ctx, t.value, value, t.width)
        return
    raise SimError(f'cannot assign {t.op}')


def _is_array_ref (ctx, e):
    if e.op != 'ref':
        return False
    base, _ = split_index(e.value)
    return isinstance(ctx.store.v.get(base), list)


def _store_indexed (ctx, base, idx, value, width):
    if base.op != 'ref':
        raise SimError('indexed assign on a non-ref')
    name = base.value
    base_name, _ = split_index(name)
    use_nxt = ctx.ff_write and base_name in ctx.ff_names
    table = (ctx.store.nxt if use_nxt and base_name in ctx.store.nxt
             else ctx.store.v)
    slot = table[base_name]
    if not isinstance(slot, list):
        raise SimError(f'{name} is not an array')
    slot[idx] = int(value) & mask(width)


def _store_expr (ctx, e, value):
    if e.op == 'ref':
        store_ref(ctx, e.value, value, e.width)
        return
    raise SimError(f'cannot store through {e.op}')


def store_ref (ctx, name, value, width):
    if name in ctx.locals:
        ctx.locals[name] = int(value) & mask(width)
        return
    base, idx = split_index(name)
    use_nxt = ctx.ff_write and base in ctx.ff_names
    table = ctx.store.nxt if use_nxt and base in ctx.store.nxt else ctx.store.v
    value = int(value) & mask(width)
    if idx is not None:
        table[base][idx] = value
        return
    if isinstance(table.get(base), list):
        raise SimError(f'array {name} assigned without an index')
    table[base] = value


def load_ref (ctx, name, lhs = False):
    if name in ctx.locals:
        return ctx.locals[name]
    if name in ctx.loop_vars:
        return ctx.loop_vars[name]
    if name in ctx.int_names:
        return ctx.int_names[name]
    base, idx = split_index(name)
    use_nxt = lhs and ctx.ff_write and base in ctx.ff_names
    table = ctx.store.nxt if use_nxt and base in ctx.store.nxt else ctx.store.v
    if base not in table:
        raise SimError(f'unknown name {name}')
    val = table[base]
    if idx is not None:
        return val[idx]
    return val


def eval_cond (ctx, e):
    if e.op == 'cmp':
        return eval_cmp(ctx, e)
    if e.op == 'not':
        return not eval_cond(ctx, e.args[0])
    return eval_expr(ctx, e) != 0


def eval_cmp (ctx, e):
    op = e.value
    a = eval_expr(ctx, e.args[0])
    b = eval_expr(ctx, e.args[1])
    if op in ('<', '<=', '>', '>=') and e.args[0].signed:
        a = to_int64(a)
        b = to_int64(b)
    if op == '==':
        return a == b
    if op == '!=':
        return a != b
    if op == '<':
        return a < b
    if op == '<=':
        return a <= b
    if op == '>':
        return a > b
    if op == '>=':
        return a >= b
    return False


def to_int64 (v):
    v &= (1 << 64) - 1
    if v & (1 << 63):
        return v - (1 << 64)
    return v


def eval_index (ctx, e):
    if e.op == 'const':
        return int(e.value)
    if e.op == 'ref' and e.value in ctx.int_names | ctx.loop_vars:
        if e.value in ctx.loop_vars:
            return int(ctx.loop_vars[e.value])
        return int(ctx.int_names[e.value])
    if e.op == 'binop':
        return int(eval_expr(ctx, e))
    return int(eval_expr(ctx, e))


def eval_expr (ctx, e, lhs = False):
    if e is None:
        return 0
    op = e.op
    a = e.args
    if op == 'ref':
        val = load_ref(ctx, e.value, lhs = lhs)
        if isinstance(val, list):
            return val
        return int(val) & mask(e.width) if not lhs else int(val)
    if op == 'const':
        return int(e.value)
    if op == 'enum':
        return int(e.value.value)
    if op == 'bit':
        return eval_bit(ctx, e, lhs)
    if op == 'slice':
        src = eval_expr(ctx, a[0], lhs = lhs)
        if lhs:
            return src
        lo = e.value[1]
        if len(a) >= 3 and a[2].op == 'const':
            lo = int(a[2].value)
        return (int(src) >> lo) & mask(e.width)
    if op == 'part':
        src = eval_expr(ctx, a[0])
        i = eval_index(ctx, a[1])
        return (int(src) >> i) & mask(e.value)
    if op == 'part_down':
        src = eval_expr(ctx, a[0])
        i = eval_index(ctx, a[1])
        w = e.value
        return (int(src) >> (i - (w - 1))) & mask(w)
    if op == 'field':
        src = eval_expr(ctx, a[0], lhs = lhs)
        if lhs:
            return src
        lo = int(a[1].value) if len(a) > 1 and a[1].op == 'const' else 0
        return (int(src) >> lo) & mask(e.width)
    if op == 'concat':
        return eval_concat(ctx, e)
    if op == 'replicate':
        return eval_replicate(ctx, e)
    if op == 'binop':
        return eval_binop(ctx, e)
    if op == 'cmp':
        return 1 if eval_cmp(ctx, e) else 0
    if op == 'unop':
        inner = eval_expr(ctx, a[0])
        w = e.width
        if e.value == '-':
            return (-int(inner)) & mask(w)
        return (~int(inner)) & mask(w)
    if op == 'not':
        return 0 if eval_expr(ctx, a[0]) else 1
    if op == 'ifexp':
        if eval_cond(ctx, a[0]):
            return eval_expr(ctx, a[1])
        return eval_expr(ctx, a[2])
    if op == 'signed':
        return eval_expr(ctx, a[0])
    if op == 'extend':
        inner = eval_expr(ctx, a[0])
        if e.signed:
            return sext(inner, a[0].width, e.width)
        return int(inner) & mask(a[0].width)
    if op == 'call':
        return eval_call(ctx, e)
    if op == 'bits':
        _, val = bits_mask(e.value)
        return val
    return 0


def eval_bit (ctx, e, lhs):
    base, idx_e = e.args
    idx = eval_index(ctx, idx_e)
    if _is_array_ref(ctx, base):
        val = load_ref(ctx, base.value, lhs = lhs)
        return val[idx]
    src = eval_expr(ctx, base, lhs = lhs)
    if lhs:
        return src
    return (int(src) >> idx) & 1


def eval_concat (ctx, e):
    acc = 0
    shift = 0
    for x in reversed(e.args):
        acc |= (eval_expr(ctx, x) & mask(x.width)) << shift
        shift += x.width
    return acc & mask(e.width if e.width else shift)


def eval_replicate (ctx, e):
    n = e.value
    inner = e.args[0]
    src = eval_expr(ctx, inner)
    w = inner.width
    if w == 1:
        return mask(n) if src & 1 else 0
    acc = 0
    for i in range(n):
        acc |= (src & mask(w)) << (i * w)
    return acc & mask(e.width if e.width else n * w)


def as_signed (value, e):
    """The value an operand carries, negative when the expression is
    signed and its top bit is set.

    signed() only marks an expression; the bits underneath are the same.
    That is enough for + - and the bitwise operators, whose two's
    complement result is the same either way, and not enough for * and
    >>, where sign decides the answer."""
    w = e.width
    v = int(value) & mask(w)
    if getattr(e, 'signed', False) and w > 0 and (v & (1 << (w - 1))):
        return v - (1 << w)
    return v


def eval_binop (ctx, e):
    op = e.value
    a, b = e.args
    w = e.width
    left = eval_expr(ctx, a)
    right = eval_expr(ctx, b)
    if op == '&':
        return (int(left) & int(right)) & mask(w)
    if op == '|':
        return (int(left) | int(right)) & mask(w)
    if op == '^':
        return (int(left) ^ int(right)) & mask(w)
    if op == '<<':
        return (as_signed(left, a) << int(right)) & mask(w)
    if op == '>>':
        # arithmetic when the left operand is signed
        return (as_signed(left, a) >> int(right)) & mask(w)
    if op == '+':
        return (as_signed(left, a) + as_signed(right, b)) & mask(w)
    if op == '-':
        return (as_signed(left, a) - as_signed(right, b)) & mask(w)
    if op == '*':
        return (as_signed(left, a) * as_signed(right, b)) & mask(w)
    return (as_signed(left, a) + as_signed(right, b)) & mask(w)


def eval_call (ctx, e):
    fn = find_fn(ctx.m, e.value, len(e.args))
    if fn is None:
        raise SimError(f'unknown function {e.value}')
    args = [eval_expr(ctx, x) for x in e.args]
    fctx = Ctx(ctx.store, False)
    for (n, w, _), val in zip(fn.params, args):
        fctx.locals[n] = int(val) & mask(w)
    for n, w in fn.locals.items():
        fctx.locals[n] = 0
    fctx.int_names = dict(ctx.int_names)
    run_stmts(fctx, fn.body)
    if fctx.fn_return is None:
        raise SimError(f'{fn.name} did not return')
    return fctx.fn_return & mask(fn.width)


def find_fn (module, name, nargs):
    for f in module.functions:
        if f.name == name and len(f.params) == nargs:
            return f
    for f in module.functions:
        if f.name == name:
            return f
    return None


def bits_mask (pattern):
    bit_mask = 0
    val = 0
    for i, ch in enumerate(reversed(pattern)):
        if ch == '1':
            bit_mask |= 1 << i
            val |= 1 << i
        elif ch == '0':
            bit_mask |= 1 << i
    return bit_mask, val


def sext (val, w, out_w):
    val = int(val) & mask(w)
    if w <= 0:
        return 0
    if val & (1 << (w - 1)):
        val |= mask(out_w) ^ mask(w)
    return val & mask(out_w)
