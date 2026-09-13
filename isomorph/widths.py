"""
Width and constant expressions: what the author wrote, checked.
"""
import ast


# // is a comment in both HDLs; integer division is /
_WIDTH_OPS = {
    ast.Add: ('+', 1), ast.Sub: ('-', 1),
    ast.Mult: ('*', 2), ast.FloorDiv: ('/', 2), ast.Div: ('/', 2),
}


# SystemVerilog has no max() for a constant expression, so it gets the
# conditional; VHDL-2008 has maximum() in std.standard
_WIDTH_CALLS = {'max': ('>', 'maximum'), 'min': ('<', 'minimum')}


def _width_value (node, known):
    """The value, without eval()."""
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
    """The expression as the HDL spells it."""
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
    """One less, folded in: WIDTH-1 gives WIDTH-2, not WIDTH-1-1."""
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
    """The expression as an AST, or None unless every name is in scope
    and working it out gives this value."""
    if not text:
        return None
    try:
        tree = ast.parse(text, mode = 'eval').body
    except (SyntaxError, ValueError):
        return None
    known = {n: v for n, v in (scope or {}).items()
             if isinstance(v, int) and not isinstance(v, bool)}
    # a called function's name is an ast.Name too
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
    """Already checked, so no scope: only the spelling changes."""
    if not text:
        return None
    try:
        tree = ast.parse(text, mode = 'eval').body
    except (SyntaxError, ValueError):
        return None
    return _width_text(tree, 0, lang)


def constant_expression (text, value, scope, lang = 'sv'):
    """A localparam's expression. scope is what is declared above it,
    so a constant may name one before it and never one after."""
    tree = checked_expression(text, value, scope)
    return None if tree is None else _width_text(tree, 0, lang)


def width_expression (width_expr, width, scope, lang = 'sv'):
    """The top bit of a declared width, as it was written.

    scope is what may be named where the declaration goes: a signal
    sits under the localparams and may use them, a port list sits
    above them and may not.
    """
    tree = checked_expression(width_expr, width, scope)
    return None if tree is None else _width_text(_less_one(tree), 0, lang)
