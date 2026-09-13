"""The intermediate form between elaboration and the emitters.

Every expression carries a width and a signedness. Statements are the
small set of section 3.4; modules are section 4.1."""
from dataclasses import dataclass, field


@dataclass
class Expr:
    op: str                    # ref, const, enum, bit, slice, part,
                               # part_down, field, concat, replicate, binop,
                               # unop, cmp, ifexp, call, signed, not, bits
    width: int
    signed: bool = False
    args: list = field(default_factory = list)
    value: object = None       # constant value, operator, name, field
    line: int = 0
    base: str = None           # 'hex', 'bin', 'oct': how it was written
    # for an extend: how the target's width was written, so the cast
    # follows the generic instead of folding (PARAMETERS.md stage 3)
    width_expr: str = None

    def __repr__ (self):
        return render(self)


@dataclass
class Assign:
    target: Expr
    value: Expr
    line: int = 0
    comments: list = field(default_factory = list)
    trailing: str = None


@dataclass
class If:
    branches: list             # [(cond Expr or None for else, [stmts])]
    line: int = 0
    comments: list = field(default_factory = list)
    trailing: str = None
    # full-line comments written above each elif / else keyword, one
    # list per entry of branches (the first is always empty)
    branch_comments: list = field(default_factory = list)


@dataclass
class Comment:
    """A full-line comment that follows the last statement of a suite.
    Every other comment rides on the item below it; this one has no
    item below it, so it is a statement of its own."""
    text: str
    line: int = 0


@dataclass
class For:
    var: str
    start: int
    stop: int
    body: list
    line: int = 0
    comments: list = field(default_factory = list)
    trailing: str = None


@dataclass
class Assert:
    cond: Expr
    message: str = None         # the text written after the comma
    line: int = 0
    comments: list = field(default_factory = list)
    trailing: str = None


@dataclass
class Return:
    value: Expr
    line: int = 0
    comments: list = field(default_factory = list)
    trailing: str = None


@dataclass
class Match:
    subject: Expr
    arms: list                 # [(pattern Expr or None, [stmts])]
    unique: bool = False
    line: int = 0
    comments: list = field(default_factory = list)
    trailing: str = None


@dataclass
class Sig:
    name: str
    width: int
    kind: str                  # bit, vector, enum, struct
    init: list = None          # array contents at configuration
    type: object = None
    attributes: dict = field(default_factory = dict)
    array: int = 0             # element count if an array
    line: int = 0
    comments: list = field(default_factory = list)
    trailing: str = None
    # how the width was written, when it was an expression over the
    # module's parameters rather than a number (SPEC 3.6, 4.1)
    width_expr: str = None


@dataclass
class Port:
    name: str
    width: int
    direction: str             # in, out
    kind: str = 'vector'
    type: object = None
    array: int = 0
    comments: list = field(default_factory = list)
    trailing: str = None
    attributes: dict = field(default_factory = dict)
    period_ns: float = None    # declared by clock(), for the .sdc
    width_expr: str = None     # the expression the width was written as


@dataclass
class Function:
    name: str
    params: list               # [(name, width, signed)]
    locals: dict               # name -> width
    body: list
    width: int = 0
    signed: bool = False


@dataclass
class Process:
    name: str
    kind: str                  # comb, ff
    clock: str = None
    polarity: str = 'pos'
    body: list = field(default_factory = list)
    attributes: dict = field(default_factory = dict)
    line: int = 0
    comments: list = field(default_factory = list)
    reset: str = None          # asynchronous reset signal (5.14)
    reset_polarity: str = 'pos'
    reason: str = None         # why this flop has one


@dataclass
class ContAssign:
    target: Expr
    value: Expr
    line: int = 0
    comments: list = field(default_factory = list)
    trailing: str = None


@dataclass
class Instance:
    name: str
    module: str
    ports: dict                # formal -> Expr (ref) or None for open
    line: int = 0
    comments: list = field(default_factory = list)
    params: dict = field(default_factory = dict)   # overrides, if any
    # one of an array built by a loop (SPEC 5.15). Every backend that
    # runs a design sees the members one at a time, named array[k];
    # the two HDL emitters collapse them back into one generate.
    array: str = None          # the array's name, or None
    index: int = 0             # which member this is
    count: int = 0             # how many there are
    var: str = None            # the genvar, on member 0
    shape: dict = field(default_factory = dict)    # formal -> how it
                               # varies, on member 0: ('same', Expr) or
                               # ('index', base name)


@dataclass
class Module:
    name: str
    block: str
    parameters: dict
    ports: list
    signals: list
    constants: dict
    enums: dict
    functions: list
    processes: list
    assigns: list
    instances: list
    source: str = ''
    header: str = ''
    # something the fitter has and isomorph does not: ports, no body
    blackbox: bool = False
    blackbox_source: str = None
    # every clock-domain crossing in the design, on the top module
    # only: what a timing constraint has to name (ROADMAP item 7)
    crossings: list = field(default_factory = list)
    # what each constant was written as, where that was an expression
    # rather than a number: name -> source text (PARAMETERS.md stage 1)
    constant_exprs: dict = field(default_factory = dict)


def render (e):
    """Readable text for dumps; not an emitter."""
    a = e.args
    if e.op == 'ref':
        return e.value
    if e.op == 'const':
        return f"{e.value}'{e.width}"
    if e.op == 'enum':
        return f'{e.value.type.name}.{e.value.name}'
    if e.op == 'bit':
        index = str(a[1].value) if a[1].op == 'const' else render(a[1])
        return f'{render(a[0])}[{index}]'
    if e.op == 'slice':
        if len(a) >= 3:
            return f'{render(a[0])}[{render(a[1])}:{render(a[2])}]'
        return f'{render(a[0])}[{e.value[0] - 1}:{e.value[1]}]'
    if e.op == 'part':
        return f'{render(a[0])}[{render(a[1])} +: {e.value}]'
    if e.op == 'part_down':
        return f'{render(a[0])}[{render(a[1])} -: {e.value}]'
    if e.op == 'field':
        return f'{render(a[0])}.{e.value}'
    if e.op == 'concat':
        return '{' + ', '.join(render(x) for x in a) + '}'
    if e.op == 'replicate':
        return '{' + f'{e.value}{{{render(a[0])}}}' + '}'
    if e.op == 'binop':
        return f'({render(a[0])} {e.value} {render(a[1])})'
    if e.op == 'cmp':
        return f'({render(a[0])} {e.value} {render(a[1])})'
    if e.op == 'unop':
        return f'({e.value}{render(a[0])})'
    if e.op == 'not':
        return f'(!{render(a[0])})'
    if e.op == 'ifexp':
        return f'({render(a[0])} ? {render(a[1])} : {render(a[2])})'
    if e.op == 'signed':
        return f'$signed({render(a[0])})'
    if e.op == 'call':
        return f'{e.value}(' + ', '.join(render(x) for x in a) + ')'
    if e.op == 'bits':
        return f"'{e.value}'"
    if e.op == 'extend':
        return f"{'sext' if e.signed else 'zext'}{e.width}({render(a[0])})"
    return f'<{e.op}>'
