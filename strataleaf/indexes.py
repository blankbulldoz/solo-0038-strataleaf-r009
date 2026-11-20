from .codec import _component_token, dumps, entry_token, field, loads, name
from .errors import Conflict, InvalidDocument, NotFound
from .mvcc import visible_rows

RANGE_OPS = {"lt": "token < ?", "le": "token <= ?", "gt": "token > ?", "ge": "token >= ?"}


def index_fields(definition):
    raw = definition["fields"]
    return loads(raw) if raw else [definition["field"]]


def refresh_document(connection, collection, key, document):
    connection.execute("DELETE FROM index_values WHERE collection=? AND key=?", (collection, key))
    if document is None:
        return
    for definition in connection.execute("SELECT * FROM index_defs WHERE collection=?", (collection,)).fetchall():
        token = entry_token(document, index_fields(definition))
        if token is None:
            continue
        if definition["unique_flag"]:
            existing = connection.execute("SELECT key FROM index_values WHERE collection=? AND name=? AND token=? LIMIT 1",
                                          (collection, definition["name"], token)).fetchone()
            if existing:
                raise Conflict("unique index conflict: " + definition["name"])
        connection.execute("INSERT INTO index_values VALUES (?,?,?,?)", (collection, definition["name"], token, key))


class Indexes:
    def __init__(self, store):
        self.store = store

    def create(self, collection, index_name, path, unique=False):
        name(index_name)
        paths = [path] if isinstance(path, str) else list(path) if isinstance(path, (list, tuple)) else []
        if not 1 <= len(paths) <= 8:
            raise InvalidDocument("an index requires between 1 and 8 field paths")
        for item in paths:
            field({}, item)
        if len(set(paths)) != len(paths):
            raise InvalidDocument("index field paths must be distinct")
        if not isinstance(unique, bool):
            raise InvalidDocument("unique must be boolean")
        with self.store.atomic(writable=True) as connection:
            if not connection.execute("SELECT 1 FROM collections WHERE name=?", (collection,)).fetchone():
                raise NotFound("unknown collection")
            if connection.execute("SELECT 1 FROM index_defs WHERE collection=? AND name=?", (collection, index_name)).fetchone():
                raise Conflict("index already exists")
            connection.execute("INSERT INTO index_defs (collection,name,field,unique_flag,fields) VALUES (?,?,?,?,?)",
                               (collection, index_name, paths[0], int(unique), dumps(paths)))
            seen = set()
            for row in visible_rows(connection, collection, int(self.store.get("head"))):
                token = entry_token(loads(row["body"]), paths)
                if token is None:
                    continue
                if unique and token in seen:
                    raise Conflict("existing documents violate unique index")
                seen.add(token)
                connection.execute("INSERT INTO index_values VALUES (?,?,?,?)", (collection, index_name, token, row["key"]))
            self.store.set("catalog_epoch", int(self.store.get("catalog_epoch")) + 1)
        return {"name": index_name, "field": paths[0], "fields": paths, "unique": unique}

    def drop(self, collection, index_name):
        with self.store.atomic(writable=True) as connection:
            changed = connection.execute("DELETE FROM index_defs WHERE collection=? AND name=?", (collection, index_name)).rowcount
            if not changed:
                raise NotFound("unknown index")
            self.store.set("catalog_epoch", int(self.store.get("catalog_epoch")) + 1)

    def list(self, collection):
        with self.store.read() as connection:
            return [{**dict(row), "fields": index_fields(row)}
                    for row in connection.execute("SELECT * FROM index_defs WHERE collection=? ORDER BY name", (collection,))]


def _successor(token):
    return token[:-1] + chr(ord(token[-1]) + 1)


def _conjunction(expression):
    if expression is None:
        return []
    if isinstance(expression, dict) and set(expression) == {"and"}:
        return [term for child in expression["and"] for term in _conjunction(child)]
    return [expression]


def plan_index(connection, collection, predicate):
    """Pick the index with the longest usable equality prefix for a predicate.

    An index is usable only when the predicate's top-level conjunction fixes a
    leading run of its fields with scalar equality terms; a numeric range on
    the next field refines the scan. Returns (name, keys, plan) or None.
    """
    equals, ranges = {}, {}
    for term in _conjunction(predicate):
        if not isinstance(term, dict) or "op" not in term:
            continue
        if term["op"] == "eq" and not isinstance(term["value"], (dict, list)):
            equals.setdefault(term["field"], term["value"])
        elif term["op"] in RANGE_OPS and isinstance(term["value"], (int, float)) and not isinstance(term["value"], bool):
            ranges.setdefault(term["field"], []).append((term["op"], term["value"]))
    best = None
    for definition in connection.execute("SELECT * FROM index_defs WHERE collection=? ORDER BY name", (collection,)):
        paths = index_fields(definition)
        prefix = 0
        while prefix < len(paths) and paths[prefix] in equals:
            prefix += 1
        if prefix == 0:
            continue
        bounds = ranges.get(paths[prefix], []) if prefix < len(paths) else []
        score = (prefix, bool(bounds))
        if best is None or score > best[0]:
            best = (score, definition, paths, prefix, bounds)
    if best is None:
        return None
    _, definition, paths, prefix, bounds = best
    head = "".join(_component_token(equals[path]) for path in paths[:prefix])
    conditions, params = ["token >= ?", "token < ?"], [collection, definition["name"], head, _successor(head)]
    for op, value in bounds:
        conditions.append(RANGE_OPS[op])
        params.append(head + _component_token(value))
    keys = {row[0] for row in connection.execute(
        "SELECT key FROM index_values WHERE collection=? AND name=? AND " + " AND ".join(conditions), params)}
    if len(paths) == 1:
        plan = {"access": "equality_index", "index": definition["name"]}
    else:
        plan = {"access": "composite_index", "index": definition["name"],
                "equality": paths[:prefix], "range": paths[prefix] if bounds else None}
    return definition["name"], keys, plan
