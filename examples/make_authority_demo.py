"""Synthetic GM provenance and unrelated player observations, not real evidence."""
import json
from pathlib import Path


def generate(path):
    records = []

    def add(kind, t, entity="", **extra):
        row = dict(schema=1, session="synthetic-authority", seq=len(records) + 1,
                   t_ms=t, utc=1794387600 + t // 1000, kind=kind, entity=entity,
                   source="runtime", prefab="{DEMO}Prefabs/Props/Example.et", category="GENERIC",
                   x=20, y=0, z=20, actor_id=0, actor_uid="", actor_name="", actor_role="UNKNOWN",
                   attribution="unknown", warmup=t < 60000)
        row.update(extra)
        records.append(row)

    def permission(status, source, entity):
        return dict(authorization=status, authorization_source=source, authorization_entity=entity,
                    authorization_id="demo-gm-" + entity, authorized_actor_id=4,
                    authorized_actor_uid="synthetic-admin-4", authorized_actor_name="DemoAdmin",
                    authorized_actor_role="ADMIN")

    add("session_start", 0)
    for i in range(12):
        entity = f"gm-vehicle-{i}"
        add("placement", 70000 + i * 100, entity, category="VEHICLE", source="editor_place",
            prefab="{DEMO}Prefabs/Vehicles/TestTruck.et",
            actor_id=4, actor_uid="synthetic-admin-4", actor_name="DemoAdmin", actor_role="ADMIN",
            attribution="server_action_callback", **permission("gm_authorized", "gm_editor", entity))
    # Same location/time range: the GM burst cannot suppress this unknown burst.
    for i in range(6):
        add("entity_init", 72000 + i * 100, f"unknown-{i}", source="editable_component")
    for group, status in enumerate(["gm_authorized", "gm_origin", "unverified"]):
        for i in range(8):
            fields = {}
            if status != "unverified":
                fields = permission(status, "gm_effect_link" if status == "gm_authorized" else "gm_editor", f"fx-root-{group}")
            add("explosive_damage", 80000 + group * 10000 + i * 100, f"target-{group}-{i}",
                source="damage_manager", source_entity=f"projectile-{group}", amount=20, x=320 + group * 300, **fields)
    add("session_end", 110000)
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in records), encoding="utf-8")
    return len(records)


if __name__ == "__main__":
    path = Path(__file__).with_name("events-authority-0.jsonl")
    print(f"Wrote {generate(path)} synthetic records to {path}")
