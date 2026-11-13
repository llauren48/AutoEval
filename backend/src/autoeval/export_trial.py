"""Read-only, dependency-closed trial export. Never mutates source or exports Outbox."""

import asyncio
import hashlib
import json
import re
from pathlib import Path

from sqlalchemy import inspect, text

from autoeval.api import create_app

TEST = re.compile(r"^(?:TEST-|test-|fixture-|browser-|model-browser-|navigation-|pipeline-)")
EXCLUDED = {"outbox", "execution_lease", "planning_capacity_lease"}
OWNED = {
    "asset_version": "asset_id",
    "run_monitor_snapshot": "binding_id",
    "test_config_item": "config_version_id",
    "config_dataset_binding": "config_version_id",
    "model_config_binding": "model_asset_id",
    "target_binding_version": "model_version_id",
    "connection_probe": "target_id",
    "standard_input_compilation": "input_id",
}


def strings(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)
    elif isinstance(value, str):
        yield value


async def export(destination: Path):
    if destination.exists():
        raise ValueError("Export destination must be new")
    app = create_app()
    async with app.router.lifespan_context(app), app.state.engine.connect() as conn:
        await conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))

        def schema(c):
            i = inspect(c)
            return {t: i.get_foreign_keys(t) for t in i.get_table_names()}

        fks = await conn.run_sync(schema)
        data = {}
        for table in fks:
            data[table] = [
                r[0]
                for r in (await conn.execute(text(f'SELECT row_to_json(t) FROM "{table}" t'))).all()
            ]
    index = {t: {r["id"]: r for r in rows if "id" in r} for t, rows in data.items()}
    lookup = {key: t for t, rows in index.items() for key in rows}
    keep = {t: set() for t in data}

    def add(t, r):
        if t not in EXCLUDED:
            keep[t].add(r["id"])

    for t, field in [
        ("asset", "asset_key"),
        ("test_config_version", "config_key"),
        ("evaluation_profile", "profile_key"),
        ("standard_input_version", "source_model"),
    ]:
        for r in data[t]:
            if not TEST.match(r[field]):
                add(t, r)
    for r in data["ssh_resource_binding"]:
        if r["host"] == "179.214.2.3":
            add("ssh_resource_binding", r)
    for r in data["evaluation_report"]:
        if r["execution_origin"] == "real":
            add("evaluation_report", r)
    for r in data["leaderboard_record"]:
        if (
            r["state"] == "approved"
            and not TEST.match(r["source_key"])
            and (
                r["payload"].get("source_type") == "official"
                or r.get("report_id") in keep["evaluation_report"]
            )
        ):
            add("leaderboard_record", r)
    changed = True
    while changed:
        previous = sum(map(len, keep.values()))
        selected_ids = set().union(*keep.values())
        for t, rows in index.items():
            if t in EXCLUDED:
                continue
            for key in list(keep[t]):
                row = rows[key]
                for fk in fks[t]:
                    if fk["referred_columns"] == ["id"] and len(fk["constrained_columns"]) == 1:
                        value = row.get(fk["constrained_columns"][0])
                        if value:
                            keep[fk["referred_table"]].add(value)
                for value in strings(row):
                    owner = lookup.get(value)
                    if owner and owner not in EXCLUDED:
                        keep[owner].add(value)
            for row in rows.values():
                owner_col = OWNED.get(t)
                if owner_col and row.get(owner_col) in selected_ids:
                    add(t, row)
                if row.get("run_id") in keep.get("evaluation_run", set()):
                    add(t, row)
                if t == "report_analysis" and row.get("report_id") in keep["evaluation_report"]:
                    add(t, row)
        changed = previous != sum(map(len, keep.values()))
    payload = {
        t: [r for r in rows if t == "alembic_version" or r.get("id") in keep[t]]
        for t, rows in data.items()
    }
    assert not payload["outbox"] and not payload["execution_lease"]
    for row in payload["evaluation_run"]:
        if row["state"] not in {"completed", "failed", "cancelled"}:
            raise ValueError("Selected real history has nonterminal run; cutover blocked")
    for t, rows in payload.items():
        for row in rows:
            for fk in fks[t]:
                if fk["referred_columns"] == ["id"] and len(fk["constrained_columns"]) == 1:
                    v = row.get(fk["constrained_columns"][0])
                    if v and v not in keep[fk["referred_table"]]:
                        raise ValueError("Export foreign key closure failed")
    destination.mkdir(parents=True)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    (destination / "data.json").write_bytes(raw)
    manifest = {
        "counts": {t: len(r) for t, r in payload.items()},
        "sha256": hashlib.sha256(raw).hexdigest(),
        "excluded_intents": sorted(EXCLUDED),
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    asyncio.run(export(args.destination))
