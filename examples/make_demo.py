"""Generate artificial observations; these are NOT evidence from the user's server."""
import json
from pathlib import Path


def generate(path):
    records = []

    def add(kind, t, entity="", **kwargs):
        item = dict(schema=1, session="synthetic-demo", seq=len(records) + 1, t_ms=t,
                    utc=1794387600 + t // 1000, kind=kind, source="editable_component",
                    entity=entity, parent="", prefab="{DEMO}Prefabs/Structures/TestWall.et",
                    category="GENERIC", x=20, y=0, z=20, actor_id=0, actor_uid="",
                    actor_name="", attribution="unknown", source_entity="", amount=0,
                    count=0, warmup=t < 60000)
        item.update(kwargs)
        records.append(item)

    add("session_start", 0, source="runtime")
    for i in range(3):
        add("entity_init", 62000 + i * 100, f"legal-{i}")
        add("placement", 62010 + i * 100, f"legal-{i}", source="conflict_request",
            actor_id=1, actor_uid="synthetic-player-1", actor_name="Demo builder",
            attribution="server_action_callback")
    add("heartbeat", 66000, source="runtime")
    for i in range(6):
        add("entity_init", 80000 + i * 100, f"unknown-{i}", x=500, z=500)
    add("heartbeat", 85000, source="runtime")
    for i in range(10):
        add("explosive_damage", 100000 + i * 10, "one-target", source="damage_manager", amount=5)
    for i in range(8):
        add("explosive_damage", 105000 + i * 100, f"target-{i}", source="damage_manager",
            amount=25, actor_id=2, attribution="engine_instigator", source_entity="demo-projectile")
    add("session_end", 110000, source="runtime")
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")
    return len(records)


if __name__ == "__main__":
    path = Path(__file__).with_name("events-demo-0.jsonl")
    print(f"Wrote {generate(path)} synthetic records to {path}")
