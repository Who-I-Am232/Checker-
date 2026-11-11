"""Generate synthetic vehicle, building, entity and mixed spawn observations."""
import json
from pathlib import Path


BUILDING_PREFAB = "{DEMO}Prefabs/Structures/TestWall.et"
VEHICLE_PREFAB = "{DEMO}Prefabs/Vehicles/TestTruck.et"
ENTITY_PREFAB = "{DEMO}Prefabs/Props/TestCrate.et"


def generate(path):
    records = []

    def add(kind, t, entity="", **extra):
        item = dict(schema=1, session="synthetic-spawn", seq=len(records) + 1, t_ms=t,
                    utc=1794387600 + t // 1000, kind=kind, source="editable_component",
                    entity=entity, parent="", prefab="", category="GENERIC", x=20, y=0, z=20,
                    actor_id=0, actor_uid="", actor_name="", attribution="unknown",
                    source_entity="", amount=0, count=0, warmup=t < 60000)
        item.update(extra)
        records.append(item)

    add("session_start", 0, source="runtime")
    groups = [(["VEHICLE"] * 12, True), (["BUILDING"] * 6, False),
              (["ENTITY"] * 6, False), (["VEHICLE", "BUILDING", "ENTITY"] * 2, False)]
    for group, (kinds, known) in enumerate(groups):
        for index, kind in enumerate(kinds):
            timestamp = 70000 + group * 10000 + index * 100
            prefab = {"VEHICLE": VEHICLE_PREFAB, "BUILDING": BUILDING_PREFAB, "ENTITY": ENTITY_PREFAB}[kind]
            props = dict(prefab=prefab, category="VEHICLE" if kind == "VEHICLE" else "GENERIC", x=20 + group * 300)
            entity = f"demo-{group}-{index}"
            add("entity_init", timestamp, entity, **props)
            if known:
                add("placement", timestamp + 1, entity, **props, source="conflict_request",
                    actor_id=4, actor_uid="synthetic-player-4", actor_name="DemoSpawner",
                    attribution="server_action_callback")
        add("heartbeat", 75000 + group * 10000, source="runtime")
    add("session_end", 110000, source="runtime")
    path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in records), encoding="utf-8")
    return len(records)


if __name__ == "__main__":
    path = Path(__file__).with_name("events-spawn-0.jsonl")
    print(f"Wrote {generate(path)} synthetic records to {path}")
