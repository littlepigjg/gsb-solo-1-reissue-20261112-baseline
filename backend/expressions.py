"""A tiny, side-effect-free expression language for dynamic preset rules.

Only whitelisted variables and functions exist. There is no attribute access,
import, file access, indexing, loops or user-defined calls, so a preset can
express arithmetic and comparisons without becoming executable code.
"""
import math
import re
from functools import lru_cache

MAX_EXPRESSION = 500
MAX_NODES = 400

VARIABLES = {
    "width",
    "height",
    "orientation",
    "source.width",
    "source.height",
    "source.orientation",
    "source.name",
    "source.stem",
    "source.tags",
    "source.megapixels",
    "source.kind",
}

# name -> (minimum arguments, maximum arguments)
FUNCTIONS = {
    "min": (1, 16),
    "max": (1, 16),
    "round": (1, 1),
    "floor": (1, 1),
    "ceil": (1, 1),
    "abs": (1, 1),
    "int": (1, 1),
    "len": (1, 1),
    "clamp": (3, 3),
    "lower": (1, 1),
    "upper": (1, 1),
    "choose": (3, 3),
}

_KEYWORDS = {"and", "or", "not", "in", "true", "false"}

TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<number>[0-9]+(?:\.[0-9]+)?)"
    r"|(?P<string>\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*')"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)"
    r"|(?P<operator>==|!=|<=|>=|<|>|\+|-|\*|/|%|\(|\)|,)"
    r")"
)


class ExpressionError(ValueError):
    """A rule expression is malformed or cannot be evaluated."""


class _Parser:
    def __init__(self, text, tokens):
        self.text = text
        self.tokens = tokens
        self.position = 0
        self.nodes = 0

    def _peek(self):
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _take(self):
        token = self._peek()
        if token is None:
            raise ExpressionError("表达式意外结束")
        self.position += 1
        return token

    def _accept(self, kind, value=None):
        token = self._peek()
        if token and token[0] == kind and (value is None or token[1] == value):
            self.position += 1
            return token
        return None

    def _expect(self, kind, value=None):
        token = self._accept(kind, value)
        if token is None:
            found = "表达式结束" if self._peek() is None else f"“{self._peek()[1]}”"
            raise ExpressionError(f"此处应为 {value or kind}，实际为 {found}")
        return token

    def _node(self, node):
        self.nodes += 1
        if self.nodes > MAX_NODES:
            raise ExpressionError("表达式过于复杂")
        return node

    def parse(self):
        node = self._or()
        if self._peek() is not None:
            raise ExpressionError(f"无法识别“{self._peek()[1]}”")
        return node

    def _or(self):
        node = self._and()
        while self._accept("name", "or"):
            node = self._node(("bin", "or", node, self._and()))
        return node

    def _and(self):
        node = self._not()
        while self._accept("name", "and"):
            node = self._node(("bin", "and", node, self._not()))
        return node

    def _not(self):
        if self._accept("name", "not"):
            return self._node(("un", "not", self._not()))
        return self._comparison()

    def _comparison(self):
        node = self._additive()
        for operator in ("==", "!=", "<=", ">=", "<", ">"):
            if self._accept("operator", operator):
                return self._node(("bin", operator, node, self._additive()))
        if self._accept("name", "in"):
            return self._node(("bin", "in", node, self._additive()))
        return node

    def _additive(self):
        node = self._multiplicative()
        while True:
            token = self._accept("operator", "+") or self._accept("operator", "-")
            if token is None:
                return node
            node = self._node(("bin", token[1], node, self._multiplicative()))

    def _multiplicative(self):
        node = self._unary()
        while True:
            token = self._accept("operator", "*") or self._accept("operator", "/") or self._accept("operator", "%")
            if token is None:
                return node
            node = self._node(("bin", token[1], node, self._unary()))

    def _unary(self):
        if self._accept("operator", "-"):
            return self._node(("un", "-", self._unary()))
        if self._accept("operator", "+"):
            return self._unary()
        return self._primary()

    def _primary(self):
        token = self._peek()
        if token is None:
            raise ExpressionError("表达式意外结束")
        if token[0] == "number":
            self.position += 1
            value = float(token[1]) if "." in token[1] else int(token[1])
            return self._node(("num", value))
        if token[0] == "string":
            self.position += 1
            return self._node(("str", _unquote(token[1])))
        if token[0] == "operator" and token[1] == "(":
            self.position += 1
            node = self._or()
            self._expect("operator", ")")
            return node
        if token[0] == "name":
            self.position += 1
            name = token[1]
            if name == "true":
                return self._node(("bool", True))
            if name == "false":
                return self._node(("bool", False))
            if name in _KEYWORDS:
                raise ExpressionError(f"“{name}”不能出现在这里")
            if self._peek() and self._peek() == ("operator", "("):
                return self._call(name)
            if name not in VARIABLES:
                raise ExpressionError(f"未知变量“{name}”")
            return self._node(("var", name))
        raise ExpressionError(f"无法识别“{token[1]}”")

    def _call(self, name):
        if "." in name or name not in FUNCTIONS:
            raise ExpressionError(f"未知函数“{name}”")
        self._expect("operator", "(")
        arguments = []
        if not (self._peek() and self._peek() == ("operator", ")")):
            arguments.append(self._or())
            while self._accept("operator", ","):
                arguments.append(self._or())
        self._expect("operator", ")")
        minimum, maximum = FUNCTIONS[name]
        if not minimum <= len(arguments) <= maximum:
            expected = str(minimum) if minimum == maximum else f"{minimum}–{maximum}"
            raise ExpressionError(f"函数“{name}”需要 {expected} 个参数")
        return self._node(("call", name, tuple(arguments)))


def _unquote(raw):
    body = raw[1:-1]
    output = []
    index = 0
    escapes = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"'}
    while index < len(body):
        character = body[index]
        if character == "\\" and index + 1 < len(body):
            following = body[index + 1]
            if following not in escapes:
                raise ExpressionError(f"不支持的转义“\\{following}”")
            output.append(escapes[following])
            index += 2
            continue
        output.append(character)
        index += 1
    return "".join(output)


@lru_cache(maxsize=512)
def compile_expression(text):
    """Parse and statically validate an expression, returning an immutable AST."""
    if not isinstance(text, str):
        raise ExpressionError("表达式必须是文本")
    text = text.strip()
    if not text:
        raise ExpressionError("表达式不能为空")
    if len(text) > MAX_EXPRESSION:
        raise ExpressionError(f"表达式不能超过 {MAX_EXPRESSION} 个字符")
    tokens = []
    position = 0
    while position < len(text):
        match = TOKEN.match(text, position)
        if not match or match.end() == position:
            raise ExpressionError(f"无法识别“{text[position:position + 12]}”")
        kind = match.lastgroup
        tokens.append((kind, match.group(kind)))
        position = match.end()
        if len(tokens) > MAX_NODES:
            raise ExpressionError("表达式过于复杂")
    return _Parser(text, tokens).parse()


def _is_number(value):
    return type(value) is not bool and isinstance(value, (int, float)) and math.isfinite(value)


def _require_number(value, label="此运算"):
    if not _is_number(value):
        raise ExpressionError(f"{label}需要数字")
    return value


def _half_up(value):
    if value >= 0:
        return int(math.floor(value + 0.5))
    return int(math.ceil(value - 0.5))


def _lookup(name, context):
    if name not in context:
        raise ExpressionError(f"变量“{name}”在当前步骤不可用")
    value = context[name]
    return value


def evaluate(node, context):
    """Evaluate a compiled expression against a primitive-only context."""
    kind = node[0]
    if kind in {"num", "str", "bool"}:
        return node[1]
    if kind == "var":
        return _lookup(node[1], context)
    if kind == "un":
        value = evaluate(node[2], context)
        if node[1] == "not":
            if type(value) is not bool:
                raise ExpressionError("“not”需要布尔值")
            return not value
        return -_require_number(value, "负号")
    if kind == "bin":
        return _binary(node[1], node[2], node[3], context)
    if kind == "call":
        if node[1] == "choose":
            return _choose([node[2][0], node[2][1], node[2][2]], context)
        return _call(node[1], [evaluate(argument, context) for argument in node[2]])
    raise ExpressionError("无法识别的表达式节点")


def _binary(operator, left_node, right_node, context):
    if operator == "and":
        left = evaluate(left_node, context)
        if type(left) is not bool:
            raise ExpressionError("“and”需要布尔值")
        if not left:
            return False
        right = evaluate(right_node, context)
        if type(right) is not bool:
            raise ExpressionError("“and”需要布尔值")
        return right
    if operator == "or":
        left = evaluate(left_node, context)
        if type(left) is not bool:
            raise ExpressionError("“or”需要布尔值")
        if left:
            return True
        right = evaluate(right_node, context)
        if type(right) is not bool:
            raise ExpressionError("“or”需要布尔值")
        return right
    left = evaluate(left_node, context)
    right = evaluate(right_node, context)
    if operator in {"==", "!="}:
        if _is_number(left) and _is_number(right):
            equal = float(left) == float(right)
        elif isinstance(left, str) and isinstance(right, str):
            equal = left == right
        elif type(left) is bool and type(right) is bool:
            equal = left is right
        else:
            raise ExpressionError("只能比较两个数字、两个文本或两个布尔值")
        return equal if operator == "==" else not equal
    if operator in {"<", "<=", ">", ">="}:
        if _is_number(left) and _is_number(right):
            a, b = float(left), float(right)
        elif isinstance(left, str) and isinstance(right, str):
            a, b = left, right
        else:
            raise ExpressionError("大小比较只支持两个数字或两个文本")
        return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[operator]
    if operator == "in":
        if isinstance(right, list):
            return left in right
        if isinstance(right, str):
            if not isinstance(left, str):
                raise ExpressionError("在文本中查找时，左侧必须是文本")
            return left in right
        raise ExpressionError("“in”右侧必须是文本或标签列表")
    if operator == "+":
        if isinstance(left, str) or isinstance(right, str):
            if not isinstance(left, str) or not isinstance(right, str):
                raise ExpressionError("文本只能与文本拼接")
            return left + right
    left = _require_number(left, f"“{operator}”左侧")
    right = _require_number(right, f"“{operator}”右侧")
    if operator == "+":
        return left + right
    if operator == "-":
        return left - right
    if operator == "*":
        return left * right
    if operator == "/":
        if float(right) == 0:
            raise ExpressionError("不能除以零")
        return float(left) / float(right)
    if operator == "%":
        if float(right) == 0:
            raise ExpressionError("不能除以零")
        return float(left) % float(right)
    raise ExpressionError(f"未知运算符“{operator}”")


def _choose(nodes, context):
    condition = evaluate(nodes[0], context)
    if type(condition) is not bool:
        raise ExpressionError("“choose”的第一个参数必须是布尔条件")
    return evaluate(nodes[1] if condition else nodes[2], context)


def _call(name, arguments):
    if name in {"min", "max"}:
        values = [_require_number(value, f"“{name}”参数") for value in arguments]
        return min(values) if name == "min" else max(values)
    if name == "round":
        return _half_up(_require_number(arguments[0], "“round”参数"))
    if name == "floor":
        return int(math.floor(_require_number(arguments[0], "“floor”参数")))
    if name == "ceil":
        return int(math.ceil(_require_number(arguments[0], "“ceil”参数")))
    if name == "abs":
        return abs(_require_number(arguments[0], "“abs”参数"))
    if name == "int":
        return int(_require_number(arguments[0], "“int”参数"))
    if name == "len":
        value = arguments[0]
        if not isinstance(value, (str, list)):
            raise ExpressionError("“len”需要文本或标签列表")
        return len(value)
    if name == "clamp":
        value, low, high = (_require_number(item, "“clamp”参数") for item in arguments)
        if low > high:
            raise ExpressionError("“clamp”的下限不能大于上限")
        return min(max(value, low), high)
    if name == "lower":
        if not isinstance(arguments[0], str):
            raise ExpressionError("“lower”需要文本")
        return arguments[0].lower()
    if name == "upper":
        if not isinstance(arguments[0], str):
            raise ExpressionError("“upper”需要文本")
        return arguments[0].upper()
    raise ExpressionError(f"未知函数“{name}”")
