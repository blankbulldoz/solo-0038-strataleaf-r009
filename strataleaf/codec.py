import hashlib
import json
import math
import re
from decimal import Decimal
from .errors import InvalidDocument

NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
MISSING = object()


def name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise InvalidDocument("names must match [a-z][a-z0-9_]{0,62}")
    return value


def key(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 512:
        raise InvalidDocument("document key must be a nonempty string of at most 512 bytes")
    return value


def validate_json(value, depth=0):
    if depth > 32:
        raise InvalidDocument("document nesting exceeds 32 levels")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            validate_json(item, depth + 1)
        return
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        for item in value.values():
            validate_json(item, depth + 1)
        return
    raise InvalidDocument("only finite JSON values and string object keys are supported")


def dumps(value):
    validate_json(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def loads(value):
    return json.loads(value)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def field(document, path):
    if not isinstance(path, str) or not path or any(not p for p in path.split(".")):
        raise InvalidDocument("field path must have nonempty dot-separated components")
    value = document
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def _number_payload(value):
    """Exact, order-preserving encoding of a finite int/float; 1 and 1.0 coincide."""
    if value == 0:
        return "1"
    sign, digits, exponent = Decimal(value).as_tuple()
    text = "".join(str(digit) for digit in digits).rstrip("0")
    point = exponent + len(digits) + 1000
    if not 0 <= point <= 9999999:
        raise InvalidDocument("number magnitude is outside the indexable range")
    if sign:
        complement = "".join(chr(ord("9") - ord(char) + ord("0")) for char in text)
        return "0" + str(9999999 - point).zfill(7) + complement + "/"
    return "2" + str(point).zfill(7) + text + "/"


def _component_token(value):
    if value is None:
        return "N"
    if isinstance(value, bool):
        return "B1" if value else "B0"
    if isinstance(value, (int, float)):
        return "M" + _number_payload(value)
    payload = []
    for char in value:
        code = ord(char)
        if char == "\x00":
            payload.append("\x01\x01")
        elif char == "\x01":
            payload.append("\x01\x02")
        elif 0xD800 <= code <= 0xDFFF:
            payload.append("\x01\x03" + format(code, "04x"))
        else:
            payload.append(char)
    return "S" + "".join(payload) + "\x00"


def entry_token(document, paths):
    """Composite index token for an ordered field list, or None when unindexed.

    A document is indexed only when every component is a scalar; a missing path
    or an object/array component excludes the whole entry, while an explicit
    null is a scalar and takes part in the token (and in uniqueness).
    """
    parts = []
    for path in paths:
        value = field(document, path)
        if value is MISSING or isinstance(value, (dict, list)):
            return None
        parts.append(_component_token(value))
    return "".join(parts)
