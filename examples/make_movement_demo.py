"""Synthetic movement observations, not actual player evidence."""
import json
from pathlib import Path


def movement(seq=1, *, distance=702.195, delta=1000, mode="VEHICLE", t=70000, **extra):
    value = dict(schema=1, session="synthetic-movement", seq=seq, t_ms=t,
                 utc=1794387600 + t // 1000, kind="movement_candidate", source="movement_sampler",
                 entity="demo-vehicle", source_entity="demo-character", parent="",
                 prefab="{DEMO}Prefabs/Vehicles/Example.et", category=mode,
                 x=100 + distance, y=10, z=200, from_x=100, from_y=10, from_z=200,
                 actor_id=7, actor_uid="synthetic-player-7", actor_name="DemoDriver",
                 attribution="server_movement_sample", motion_mode=mode, delta_ms=delta,
                 distance_m=distance, avg_speed_mps=distance * 1000 / delta,
                 inst_speed_mps=145.189, inst_speed_valid=True, warmup=False, count=0, amount=0)
    value.update(extra)
    return value


def generate(path):
    # One threshold crossing; the ordinary vehicle candidate stays below the
    # analyzer's threshold, and the final record is a reset audit, not movement.
    records = [movement(), movement(2, distance=120, t=71000),
               dict(schema=1, session="synthetic-movement", seq=3, t_ms=72000,
                    utc=1794387672, kind="movement_reset", source="authorized_server_adapter",
                    category="synthetic_admin_action", actor_id=7, attribution="server_action_callback")]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
    return len(records)


if __name__ == "__main__":
    path = Path(__file__).with_name("events-movement-0.jsonl")
    print(f"Wrote {generate(path)} synthetic records to {path}")
