"""Width and constant expressions: what the author wrote, checked.

A width or a constant that was written as an expression over the
module's own names is emitted as that expression rather than as the
number one elaboration happened to produce. SPEC 3.6 already says a
slice bound is emitted as written; this is the same rule for a
declaration, a localparam and a cast.

Nothing here is emitted unless it is provably the expression that
produced the value: every name in it is something in scope, and
working it out with their values gives what elaboration computed.
There is no eval - four arithmetic operators, max, min, and names the
module declares, so there is nothing that could run anything.

It lives on its own because all three of analyse, emit_sv and
emit_vhdl need it and analyse cannot import an emitter.
"""
import ast


# the arithmetic a declared width may be written in, and what each
# operator is called in both emitted languages. Python's // is integer
# division and is spelt / in SystemVerilog and VHDL, where // would be
# the start of a comment
_WIDTH_OPS = {
    ast.Add: ('+', 1), ast.Sub: ('-', 1),
    ast.Mult: ('*', 2), ast.FloorDiv: ('/', 2), ast.Div: ('/', 2),
}


# max() and min() of two widths, which is how a block that takes two
# operands sizes itself. They are the one place the two languages want
# different text for one expression: VHDL-2008 has maximum() in
# std.standard, and SystemVerilog has no such function for a constant
# expression, so it gets the conditional that means the same thing.
_WIDTH_CALLS = {'max': ('>', 'maximum'), 'min': ('<', 'minimum')}


def _width_value (node, known):
    """The value of a width expression, without eval().

    Only names the module declares and the four arithmetic operators
    are allowed, so there is nothing here that could run anything.
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int):
            raise ValueError('not an integer')
        return node.value
    if isinstance(node, ast.Name):
        if node.id not in known:
            raise ValueError('not a parameter')
        return known[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return -_width_value(node.operand, known)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _WIDTH_CALLS and len(node.args) >= 2
            and not node.keywords):
        values = [_width_value(a, known) for a in node.args]
        return max(values) if node.func.id == 'max' else min(values)
    if isinstance(node, ast.BinOp) and type(node.op) in _WIDTH_OPS:
        left = _width_value(node.left, known)
        right = _width_value(node.right, known)
        if type(node.op) in (ast.FloorDiv, ast.Div):
            if right == 0:
                raise ValueError('division by zero')
            return left // right
        return {ast.Add: lambda a, b: a + b,
                ast.Sub: lambda a, b: a - b,
                ast.Mult: lambda a, b: a * b}[type(node.op)](left, right)
    raise ValueError('not width arithmetic')


def _width_text (node, outer = 0, lang = 'sv'):
    """The expression as the HDL spells it, parenthesised where the
    precedence needs it and nowhere else."""
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.UnaryOp):
        return '-' + _width_text(node.operand, 3, lang)
    if isinstance(node, ast.Call):
        compare, function = _WIDTH_CALLS[node.func.id]
        parts = [_width_text(a, 0, lang) for a in node.args]
        if lang == 'vhdl':
            text = parts[0]
            for part in parts[1:]:
                text = f'{function}({text}, {part})'
            return text
        text = parts[0]
        for part in parts[1:]:
            text = f'({text} {compare} {part} ? {text} : {part})'
        return text
    symbol, precedence = _WIDTH_OPS[type(node.op)]
    text = (_width_text(node.left, precedence, lang)
            + symbol
            + _width_text(node.right, precedence + 1, lang))
    return f'({text})' if precedence < outer else text


def _less_one (node):
    """One less than the expression, folded into it where it can be.

    signal(WIDTH - 1) declares bits WIDTH-2 down to 0, and writing
    that as WIDTH-1-1 would be arithmetic the author did not write."""
    if isinstance(node, ast.Constant):
        return ast.Constant(node.value - 1)
    if isinstance(node, ast.BinOp) and isinstance(node.right, ast.Constant):
        if isinstance(node.op, ast.Add):
            if node.right.value == 1:
                return node.left
            return ast.BinOp(node.left, ast.Add(),
                             ast.Constant(node.right.value - 1))
        if isinstance(node.op, ast.Sub):
            return ast.BinOp(node.left, ast.Sub(),
                             ast.Constant(node.right.value + 1))
    return ast.BinOp(node, ast.Sub(), ast.Constant(1))


def checked_expression (text, value, scope):
    """The author's expression as an AST, where it is provably the one
    that produced this value, or None.

    Every name in it is something in scope, and working it out with
    their values gives the value elaboration computed. There is no
    eval here: four arithmetic operators and names the module
    declares, so there is nothing that could run anything.
    """
    if not text:
        return None
    try:
        tree = ast.parse(text, mode = 'eval').body
    except (SyntaxError, ValueError):
        return None
    known = {n: v for n, v in (scope or {}).items()
             if isinstance(v, int) and not isinstance(v, bool)}
    # the name of a called function is an ast.Name too, and max is not
    # a width the module declares
    called = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    names = {n.id for n in ast.walk(tree)
             if isinstance(n, ast.Name) and id(n) not in called}
    if not names or not names <= set(known):
        return None
    try:
        if _width_value(tree, known) != value:
            return None
    except (ValueError, TypeError, RecursionError):
        return None
    return tree


def render_expression (text, lang = 'sv'):
    """An expression that has already been checked, in the language's
    own spelling. No scope, because the checking was done where the
    scope was known and the answer does not change."""
    if not text:
        return None
    try:
        tree = ast.parse(text, mode = 'eval').body
    except (SyntaxError, ValueError):
        return None
    return _width_text(tree, 0, lang)


def constant_expression (text, value, scope, lang = 'sv'):
    """A localparam's own expression rather than the number it came to.

    `WIDTHH = WIDTH // 2` says why sixteen; `localparam WIDTHH = 16`
    says nothing, and the arithmetic of a block comes out as a set of
    unrelated numbers. `scope` is what is already declared above this
    one, so a constant may name a parameter or a constant before it
    and never one after (PARAMETERS.md stage 1).
    """
    tree = checked_expression(text, value, scope)
    return None if tree is None else _width_text(tree, 0, lang)


def width_expression (width_expr, width, scope, lang = 'sv'):
    """The top bit of a declared width, written as the author wrote it.

    `signal(WIDTH - 1)` should declare `[WIDTH-2:0]`, not the `[6:0]`
    that one elaboration happened to produce. SPEC 3.6 already says a
    slice bound is emitted as written; this is the same rule for a
    declaration, and it is what lets one module serve every width
    rather than a file per size.

    The expression is only used when it is provably the one that built
    this signal: every name in it is a parameter or constant of this
    module, and working it out with their values gives the width the
    design really has. Anything else - a name from the elaborate
    function that the module never declared, a width that came out
    some other way - falls back to the literal. A declaration that
    said something the design does not do would be worse than an
    unreadable one.

    `scope` is what may be named where the declaration goes, and it is
    not the same in both places. A signal is declared under the
    localparams and may use them; a port list is above them and may
    only use the parameters, which is why a port width naming a
    localparam went out as [WIDTHD-1:0] against a module that never
    declared WIDTHD, and Verilator said so.
    """
    tree = checked_expression(width_expr, width, scope)
    return None if tree is None else _width_text(_less_one(tree), 0, lang)
