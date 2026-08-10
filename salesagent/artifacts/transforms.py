"""The transform op grammar: pure Python over stored artifact rows. The LLM
emits a ~40-token ops spec; data never transits the context window.

Ops: filter | sort | groupby | select | limit | rename | set_styling |
     to_chart | to_map | to_table | revert
Errors are actionable — they name the available columns so the agent can
self-correct (or decide to re-query the source).
"""
from __future__ import annotations

from typing import Any

CMPS = {"eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains",
        "between", "isnull", "notnull"}
AGG_FNS = {"sum", "count", "avg", "min", "max"}
TIER_CLASSES = {"hot", "warm", "now", "std"}   # green / yellow / red / gray
TIER_TESTS = {"eq", "contains", "gte", "lte"}


class TransformError(ValueError):
    pass


def _col_index(columns: list[dict], key: str) -> int:
    for i, c in enumerate(columns):
        if c["key"] == key:
            return i
    raise TransformError(
        f"no column '{key}'; available columns: "
        f"{[c['key'] for c in columns]}")


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None


def _cmp(cell: Any, cmp: str, value: Any) -> bool:
    if cmp == "isnull":
        return cell is None or cell == ""
    if cmp == "notnull":
        return not (cell is None or cell == "")
    if cmp == "in":
        vals = value if isinstance(value, list) else [value]
        return any(str(cell).strip().lower() == str(v).strip().lower() for v in vals)
    if cmp == "not_in":
        vals = value if isinstance(value, list) else [value]
        return all(str(cell).strip().lower() != str(v).strip().lower() for v in vals)
    if cmp == "contains":
        return str(value).lower() in str(cell or "").lower()
    if cmp in ("eq", "ne"):
        a, b = _num(cell), _num(value)
        same = (a is not None and b is not None and a == b) or \
               (str(cell).strip().lower() == str(value).strip().lower())
        return same if cmp == "eq" else not same
    # numeric comparisons; a null cell never passes
    a = _num(cell)
    if a is None:
        return False
    if cmp == "between":
        lo, hi = _num(value[0]), _num(value[1])
        return lo is not None and hi is not None and lo <= a <= hi
    b = _num(value)
    if b is None:
        return False
    return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[cmp]


def apply_ops(base: dict, ops: list[dict]) -> dict:
    """base = full artifact spec from store.get(). Returns a result dict:
    {columns, rows, kind, title, styling, chart, map, revert_to}."""
    columns = [dict(c) for c in base["columns"]]
    rows = [list(r) for r in base["rows"]]
    kind = base["kind"]
    title = base.get("title")
    styling = base.get("styling")
    chart = base.get("chart")
    map_spec = base.get("map")
    revert_to: int | None = None

    for op in ops:
        name = op.get("op")
        if name == "revert":
            revert_to = int(op["to_version"])

        elif name == "filter":
            i = _col_index(columns, op["col"])
            cmp = op.get("cmp", "eq")
            if cmp not in CMPS:
                raise TransformError(f"unknown cmp '{cmp}'; use one of {sorted(CMPS)}")
            rows = [r for r in rows if _cmp(r[i], cmp, op.get("value"))]

        elif name == "sort":
            for spec in reversed(op.get("by", [])):
                i = _col_index(columns, spec["col"])
                desc = str(spec.get("dir", "asc")).lower() == "desc"
                # numeric-aware sort; nulls always last regardless of direction
                def key(r, i=i):
                    v = _num(r[i])
                    if v is None:
                        return (2, "", 0) if r[i] in (None, "") else (1, str(r[i]).lower(), 0)
                    return (0, "", v)
                nulls = [r for r in rows if r[i] in (None, "")]
                vals = [r for r in rows if r[i] not in (None, "")]
                vals.sort(key=key, reverse=desc)
                rows = vals + nulls

        elif name == "groupby":
            by = op.get("by", [])
            aggs = op.get("aggs", [])
            if not by:
                raise TransformError("groupby needs 'by': [col, ...]")
            bi = [_col_index(columns, b) for b in by]
            plan = []
            for a in aggs:
                fn = a.get("fn", "count")
                if fn not in AGG_FNS:
                    raise TransformError(f"unknown agg fn '{fn}'; use {sorted(AGG_FNS)}")
                ci = _col_index(columns, a["col"]) if fn != "count" or a.get("col") else None
                plan.append((fn, ci, a.get("as") or (f"{fn}_{a.get('col', 'rows')}")))
            groups: dict[tuple, list] = {}
            for r in rows:
                groups.setdefault(tuple(r[i] for i in bi), []).append(r)
            new_rows = []
            for gkey, members in groups.items():
                out = list(gkey)
                for fn, ci, _label in plan:
                    if fn == "count":
                        out.append(len(members))
                        continue
                    nums = [x for x in (_num(m[ci]) for m in members) if x is not None]
                    if not nums:
                        out.append(None)
                    elif fn == "sum":
                        out.append(sum(nums))
                    elif fn == "avg":
                        out.append(sum(nums) / len(nums))
                    elif fn == "min":
                        out.append(min(nums))
                    elif fn == "max":
                        out.append(max(nums))
                new_rows.append(out)
            old_cols = {c["key"]: c for c in columns}
            new_columns = [dict(old_cols[b]) for b in by]
            for j, (fn, _ci, label) in enumerate(plan):
                src = aggs[j].get("col") or ""
                fmt = "int" if fn == "count" else \
                    (old_cols.get(src, {}).get("format") or "number")
                new_columns.append({"key": label, "label": label,
                                    "type": "int" if fn == "count" else "number",
                                    "format": fmt})
            columns = new_columns
            rows = sorted(new_rows, key=lambda r: str(r[0]).lower())

        elif name == "select":
            keys = op.get("cols", [])
            idx = [_col_index(columns, k) for k in keys]
            columns = [columns[i] for i in idx]
            rows = [[r[i] for i in idx] for r in rows]

        elif name == "limit":
            rows = rows[: max(1, int(op.get("n", 100)))]

        elif name == "rename":
            mapping = op.get("map", {})
            for c in columns:
                if c["key"] in mapping:
                    c["label"] = mapping[c["key"]]

        elif name == "append_rows":
            new = op.get("rows")
            if not isinstance(new, list) or not new \
                    or not all(isinstance(r, dict) for r in new):
                raise TransformError(
                    "append_rows needs rows: [{column_key: value, ...}, ...]")
            if len(new) > 200:
                raise TransformError("append_rows caps at 200 rows per call")
            key_idx = {c["key"]: i for i, c in enumerate(columns)}
            for r in new:
                unknown = [k for k in r if k not in key_idx]
                if unknown:
                    raise TransformError(
                        f"unknown columns {unknown}; available columns: "
                        f"{[c['key'] for c in columns]}")
                row = [None] * len(columns)
                for k, val in r.items():
                    row[key_idx[k]] = val
                rows.append(row)

        elif name == "concat":
            # stack another table's rows under this one; columns unioned by key
            other = op.get("_other")
            if not other:
                raise TransformError(
                    "concat needs artifact_id of another table in this conversation")
            mykeys = {c["key"] for c in columns}
            for c in other["columns"]:
                if c["key"] not in mykeys:
                    columns.append(dict(c))
            key_idx = {c["key"]: i for i, c in enumerate(columns)}
            width = len(columns)
            for r in rows:
                r.extend([None] * (width - len(r)))
            okeys = [c["key"] for c in other["columns"]]
            for orow in other["rows"]:
                row = [None] * width
                for k, val in zip(okeys, orow):
                    row[key_idx[k]] = val
                rows.append(row)

        elif name == "join":
            # pull columns from another table where key values match
            other = op.get("_other")
            if not other:
                raise TransformError(
                    "join needs artifact_id of another table in this conversation")
            on = op.get("on")
            if not on:
                raise TransformError("join needs on: <column key in this table>")
            li = _col_index(columns, on)
            right_on = op.get("right_on") or on
            okeys = [c["key"] for c in other["columns"]]
            if right_on not in okeys:
                raise TransformError(
                    f"no column '{right_on}' in the other table; its columns: {okeys}")
            ri = okeys.index(right_on)
            how = op.get("how", "left")
            if how not in ("left", "inner"):
                raise TransformError("join 'how' must be left|inner")
            pick = op.get("select")
            if pick:
                missing = [k for k in pick if k not in okeys]
                if missing:
                    raise TransformError(
                        f"select columns {missing} not in the other table; "
                        f"its columns: {okeys}")

            def jkey(v):
                n = _num(v)
                return ("n", n) if n is not None else \
                    ("s", str(v or "").strip().lower())

            index: dict = {}
            for orow in other["rows"]:
                index.setdefault(jkey(orow[ri]), orow)   # first match wins
            taken = {c["key"] for c in columns}
            bring: list[tuple[int, dict]] = []
            for j, c in enumerate(other["columns"]):
                if c["key"] == right_on or (pick and c["key"] not in pick):
                    continue
                nc = dict(c)
                while nc["key"] in taken:   # collision -> suffix
                    nc["key"] += "_2"
                    nc["label"] = (nc.get("label") or nc["key"])
                taken.add(nc["key"])
                bring.append((j, nc))
            columns.extend(nc for _, nc in bring)
            joined = []
            for r in rows:
                m = index.get(jkey(r[li]))
                if m is None:
                    if how == "inner":
                        continue
                    r.extend([None] * len(bring))
                else:
                    r.extend(m[j] for j, _ in bring)
                joined.append(r)
            rows = joined

        elif name == "set_styling":
            st = op.get("styling")
            if st is not None:
                if not isinstance(st, dict):
                    raise TransformError(
                        "styling must be an object like {tier_rules: "
                        "[{column, eq|contains|gte|lte: value, class: hot|warm|now|std}]}")
                for rule in st.get("tier_rules") or []:
                    if not isinstance(rule, dict):
                        raise TransformError("each tier_rule must be an object")
                    _col_index(columns, rule.get("column") or "")
                    if rule.get("class") not in TIER_CLASSES:
                        raise TransformError(
                            f"tier class must be one of {sorted(TIER_CLASSES)} "
                            "(hot=green, warm=yellow, now=red, std=gray)")
                    if not any(k in rule for k in TIER_TESTS):
                        raise TransformError(
                            f"each tier_rule needs a test: one of {sorted(TIER_TESTS)}")
                    if "label" in rule:
                        if not isinstance(rule["label"], str):
                            raise TransformError(
                                "tier_rule label must be a short string "
                                "(it becomes the legend text)")
                        rule["label"] = rule["label"].strip()[:80]
            styling = st

        elif name == "to_chart":
            chart = op.get("chart") or {}
            if chart.get("x"):
                _col_index(columns, chart["x"])
            if chart.get("y"):
                _col_index(columns, chart["y"])
            colors = chart.get("colors")
            if colors is not None and (
                    not isinstance(colors, dict)
                    or not all(isinstance(k, str) and isinstance(v, str)
                               for k, v in colors.items())):
                raise TransformError(
                    "chart.colors must map label -> color, e.g. "
                    "{\"NJ\": \"#c00000\", \"PA\": \"hot\"} (values: hex or "
                    "hot|warm|now|std)")
            kind = "chart"

        elif name == "to_map":
            map_spec = op.get("map") or {"lat": "lat", "lng": "lng"}
            _col_index(columns, map_spec.get("lat", "lat"))
            _col_index(columns, map_spec.get("lng", "lng"))
            kind = "map"

        elif name == "to_table":
            kind = "table"

        else:
            raise TransformError(f"unknown op '{name}'")

    return {"columns": columns, "rows": rows, "kind": kind, "title": title,
            "styling": styling, "chart": chart, "map": map_spec,
            "revert_to": revert_to}
