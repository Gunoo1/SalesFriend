"""classify_rural — stamp rurality onto ANY table artifact with a zip
column (uploads, OSM results, checkbook vendors, merged prospect lists...),
using the vendored USDA ERS RUCA codes (41k zips, seeded in app.db).

The estates don't need this (districts/colleges/private_schools carry NCES
locale natively; labs carry urban_rural) — this is for everything else."""
from __future__ import annotations

from ..artifacts import store
from ..integrations.rural import (REMOTE_MIN, lookup_ruca, ruca_class, zip5)
from .envelope import error_envelope, prov, table_envelope
from .registry import CostClass, tool_spec

_ZIP_KEY_HINTS = ("zip", "pzip", "zip_code", "zipcode", "postcode",
                  "postal", "zip5")


def _find_zip_col(columns: list[dict], explicit: str | None
                  ) -> int | None:
    keys = [str(c.get("key", "")).lower() for c in columns]
    if explicit:
        want = explicit.strip().lower()
        for i, k in enumerate(keys):
            if k == want:
                return i
        return None
    for i, k in enumerate(keys):
        if k in _ZIP_KEY_HINTS:
            return i
    for i, k in enumerate(keys):
        if "zip" in k:
            return i
    return None


@tool_spec(
    name="classify_rural",
    description=(
        "Stamp RURALITY onto any existing table artifact that has a zip "
        "column: adds RUCA code (USDA rural-urban commuting area, 1-10), an "
        "area class (metro / micropolitan / small town / rural remote) and "
        "a remote flag (RUCA>=7 = the 'middle of nowhere' places reps "
        "rarely visit). Use on uploads, OSM/nearby-org results, checkbook "
        "vendor lists, merged prospect tables. only_remote=true keeps just "
        "the remote rows. (The estates don't need this — k12/colleges/"
        "private_schools/labs finds have native rural filters.) Free, local."),
    input_schema={
        "properties": {
            "artifact_id": {"type": "string"},
            "zip_column": {"type": "string",
                           "description": "column key holding zips; "
                                          "auto-detected when omitted"},
            "only_remote": {"type": "boolean", "default": False,
                            "description": "keep only RUCA>=7 rows"},
            "min_ruca": {"type": "integer",
                         "description": "custom cutoff instead of "
                                        "only_remote (e.g. 4 = anything "
                                        "outside metro)"},
        },
        "required": ["artifact_id"],
    },
    cost_class=CostClass.FREE,
)
def classify_rural(ctx, artifact_id: str, zip_column: str | None = None,
                   only_remote: bool = False,
                   min_ruca: int | None = None) -> dict:
    conn = ctx.rw()
    spec = store.get(conn, artifact_id)
    if not spec:
        return error_envelope(f"unknown artifact {artifact_id}")
    if spec["kind"] not in ("table", "map"):
        return error_envelope(
            f"artifact {artifact_id} is kind={spec['kind']} — "
            "classify_rural needs a table/map artifact")
    cols = list(spec["columns"])
    zi = _find_zip_col(cols, zip_column)
    if zi is None:
        keys = ", ".join(str(c.get("key")) for c in cols)
        return error_envelope(
            f"no zip column found in {artifact_id}"
            + (f" (asked for {zip_column!r})" if zip_column else "")
            + f"; available columns: {keys}", error_type="BadParams")

    rows = spec["rows"]
    zips = [zip5(r[zi]) if zi < len(r) else None for r in rows]
    ruca_by_zip = lookup_ruca(conn, zips)

    cutoff = None
    if min_ruca is not None:
        cutoff = int(min_ruca)
    elif only_remote:
        cutoff = REMOTE_MIN

    new_cols = cols + [
        {"key": "ruca", "label": "RUCA", "type": "number", "format": "int"},
        {"key": "area_class", "label": "Area", "type": "string"},
        {"key": "remote", "label": "Remote", "type": "string"},
    ]
    new_rows, by_class, unmatched, kept_remote = [], {}, 0, 0
    for r, z in zip(rows, zips):
        ruca = ruca_by_zip.get(z) if z else None
        label = ruca_class(ruca)
        if ruca is None:
            unmatched += 1
        else:
            by_class[label] = by_class.get(label, 0) + 1
        is_remote = ruca is not None and ruca >= REMOTE_MIN
        if is_remote:
            kept_remote += 1
        if cutoff is not None and (ruca is None or ruca < cutoff):
            continue
        new_rows.append(list(r) + [ruca, label,
                                   "yes" if is_remote else
                                   ("" if ruca is None else "no")])

    warnings = []
    if unmatched:
        warnings.append(f"{unmatched} rows had no/unknown zip — left "
                        "unclassified" + (" and dropped by the cutoff"
                                          if cutoff is not None else ""))
    title = spec["title"] + (" — remote only" if cutoff is not None
                             else " — rurality")
    filt = (f" {len(new_rows)} rows pass RUCA>={cutoff}."
            if cutoff is not None else "")
    return table_envelope(
        conn, ctx.emit, conversation_id=ctx.conversation_id,
        tool="classify_rural", title=title,
        columns=new_cols, rows=new_rows,
        provenance=(spec.get("provenance") or [])
        + [prov("USDA ERS RUCA codes (2010, ZIP-level)",
                "vendored seed ref_zip_ruca (41k zips); RUCA 1-3 metro, "
                "4-6 micropolitan, 7-9 small town, 10 rural remote",
                "https://www.ers.usda.gov/data-products/"
                "rural-urban-commuting-area-codes")],
        summary=f"rurality stamped on {len(rows)} rows: "
                + ", ".join(f"{v} {k}" for k, v in sorted(
                    by_class.items(), key=lambda kv: -kv[1]))
                + f". {kept_remote} are remote (RUCA>={REMOTE_MIN})." + filt,
        warnings=warnings,
        stats={"by_area_class": by_class, "unmatched_zip": unmatched},
        styling={"tier_rules": [
            {"column": "remote", "eq": "yes", "class": "hot",
             "label": f"Remote (RUCA>={REMOTE_MIN}) — rarely visited"}]},
        map_spec=spec.get("map"))
