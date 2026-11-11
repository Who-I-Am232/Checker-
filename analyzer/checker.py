#!/usr/bin/env python3
"""Analyze ConflictAudit JSONL evidence. Python 3.10+, standard library only.

This program flags observations, not cheat software. It has no game-server
credentials, RPC client, player punishment, process scanner, or network output.
"""
from __future__ import annotations

import argparse
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from typing import Any, Callable


@dataclass
class Config:
    settle_ms: int = 2500
    spawn_window_ms: int = 10000
    spawn_threshold: int = 12
    unattributed_threshold: int = 6
    scan_threshold: int = 8
    trigger_window_ms: int = 5000
    trigger_threshold: int = 6
    damage_window_ms: int = 2000
    damage_victims_threshold: int = 8
    cell_meters: int = 150
    cooldown_ms: int = 30000
    max_entities: int = 20000
    max_windows: int = 2048
    max_window_events: int = 2048
    max_sessions: int = 8
    movement_min_delta_ms: int = 250
    movement_max_delta_ms: int = 2500
    movement_foot_distance_m: int = 80
    movement_foot_speed_mps: int = 35
    movement_vehicle_distance_m: int = 200
    movement_vehicle_speed_mps: int = 200
    movement_cooldown_ms: int = 30000
    allowed_prefabs: list[str] = field(default_factory=list)
    watch_prefabs: list[str] = field(default_factory=list)
    spawn_categories: list[str] = field(default_factory=lambda: ["GENERIC", "UNCLASSIFIED", "VEHICLE"])
    known_routes: list[str] = field(default_factory=lambda: ["conflict_request", "editor_place"])

    @classmethod
    def load(cls, path: Path | None) -> Config:
        values = json.loads(path.read_text(encoding="utf-8")) if path else {}
        if not isinstance(values, dict) or set(values) - set(cls.__dataclass_fields__):
            raise ValueError("Config must be an object with documented keys only")
        obj = cls(**values)
        for name, value in asdict(obj).items():
            if name in {"allowed_prefabs", "watch_prefabs", "spawn_categories", "known_routes"}:
                if not isinstance(value, list) or len(value) > 2000 or any(not isinstance(v, str) or not v or len(v) > 1024 for v in value):
                    raise ValueError(f"Invalid string list: {name}")
            elif type(value) is not int or not 1 <= value <= 1000000:
                raise ValueError(f"Invalid positive integer: {name}")
        if set(obj.allowed_prefabs) & set(obj.watch_prefabs):
            raise ValueError("A prefab cannot be both allowed and watched")
        if obj.max_window_events < max(obj.spawn_threshold, obj.unattributed_threshold, obj.scan_threshold, obj.trigger_threshold, obj.damage_victims_threshold):
            raise ValueError("max_window_events is below a detection threshold")
        if obj.movement_min_delta_ms > obj.movement_max_delta_ms:
            raise ValueError("Movement sampling interval is reversed")
        return obj


KINDS = {"session_start", "session_end", "heartbeat", "player_identity", "player_left",
         "entity_init", "placement", "scan_new", "scan_baseline", "scan_incomplete",
         "explosive_damage", "explosive_trigger", "telemetry_loss", "movement_candidate", "movement_reset"}


def validate(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or type(raw.get("schema")) is not int or raw["schema"] != 1:
        raise ValueError("Expected schema 1 event object")
    event = dict(raw)
    for key in ("session", "kind"):
        if not isinstance(event.get(key), str) or not event[key] or len(event[key]) > 128:
            raise ValueError(f"Invalid {key}")
    if any(ord(c) < 32 for c in event["session"]) or event["kind"] not in KINDS:
        raise ValueError("Invalid session or event kind")
    for key in ("seq", "t_ms", "utc"):
        value = event.get(key)
        if type(value) is not int or not 0 <= value <= 2**53 or (key == "seq" and value == 0):
            raise ValueError(f"Invalid {key}")
    for key in ("actor_id", "count"):
        value = event.setdefault(key, 0)
        if type(value) is not int or not 0 <= value <= 2**31 - 1:
            raise ValueError(f"Invalid {key}")
    if type(event.setdefault("warmup", False)) is not bool:
        raise ValueError("Invalid warmup flag")
    for key in ("source", "entity", "parent", "prefab", "category", "actor_uid", "actor_name", "attribution", "source_entity"):
        value = event.setdefault(key, "")
        if not isinstance(value, str) or len(value) > 2048:
            raise ValueError(f"Invalid {key}")
    for key in ("x", "y", "z", "amount"):
        value = event.setdefault(key, 0)
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e15:
            raise ValueError(f"Invalid {key}")
    if event["kind"] in {"entity_init", "placement", "scan_new", "explosive_damage", "explosive_trigger"} and not event["entity"]:
        raise ValueError("Entity observation has no entity ID")
    if event["kind"] == "movement_candidate":
        if not event["entity"] or event["actor_id"] <= 0 or event["attribution"] != "server_movement_sample":
            raise ValueError("Movement observation needs server control attribution")
        if event.get("motion_mode") not in {"FOOT", "VEHICLE"}:
            raise ValueError("Invalid movement mode")
        if type(event.get("delta_ms")) is not int or not 0 < event["delta_ms"] <= event["t_ms"]:
            raise ValueError("Invalid movement interval")
        if type(event.get("inst_speed_valid")) is not bool:
            raise ValueError("Invalid physics speed flag")
        for key in ("from_x", "from_y", "from_z", "distance_m", "avg_speed_mps", "inst_speed_mps"):
            value = event.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e15:
                raise ValueError(f"Invalid movement field: {key}")
        distance = math.dist([event[k] for k in ("from_x", "from_y", "from_z")], [event[k] for k in ("x", "y", "z")])
        speed = distance * 1000 / event["delta_ms"]
        if (not math.isclose(event["distance_m"], distance, rel_tol=0.001, abs_tol=0.1)
                or not math.isclose(event["avg_speed_mps"], speed, rel_tol=0.001, abs_tol=0.1)
                or event["inst_speed_mps"] < 0):
            raise ValueError("Inconsistent movement measurements")
    return event


@dataclass
class Session:
    last_seq: int = 0
    now: int = 0
    pending: dict[str, dict] = field(default_factory=dict)
    entities: OrderedDict[str, dict] = field(default_factory=OrderedDict)
    actions: dict[str, dict] = field(default_factory=dict)
    windows: dict[tuple, deque] = field(default_factory=dict)
    cooldowns: dict[tuple, int] = field(default_factory=dict)
    last_sweep: int = 0
    ended: bool = False


class Analyzer:
    def __init__(self, config: Config | None = None, emit: Callable[[dict], None] | None = None):
        self.config = config or Config()
        self.sessions: OrderedDict[str, Session] = OrderedDict()
        self.alerts: list[dict] = []
        self.emit = emit or self.alerts.append
        self.processed = 0
        self.duplicates = 0
        self.alert_count = 0
        self.last_event: dict | None = None

    def process(self, raw: dict) -> None:
        e = validate(raw)
        sid = e["session"]
        if sid not in self.sessions:
            if len(self.sessions) >= self.config.max_sessions:
                old_sid = next((key for key, value in self.sessions.items() if value.ended), next(iter(self.sessions)))
                old = self.sessions.pop(old_sid)
                self._finalize(old_sid, old, force=True)
                if not old.ended:
                    self._alert(e, "coverage_gap", "warning", "Session state limit: old correlation state evicted", [e])
            self.sessions[sid] = Session()
        s = self.sessions[sid]
        self.sessions.move_to_end(sid)
        if e["seq"] <= s.last_seq:
            self.duplicates += 1
            return
        if e["seq"] != s.last_seq + 1:
            self._alert(e, "sequence_gap", "warning", "Missing telemetry sequence numbers; coverage is incomplete", [e])
        s.last_seq = e["seq"]
        s.now = max(s.now, e["t_ms"])
        self.processed += 1
        self.last_event = e
        self._sweep(s)
        kind = e["kind"]

        if kind in {"telemetry_loss", "scan_incomplete"}:
            self._alert(e, "coverage_gap", "warning", f"Collector reported {kind}; count={e['count']}", [e])
        elif kind == "placement":
            # Store only the observed server-side route. This is not a verdict
            # that the action was legitimate, nor proof of a client's RPC sender.
            s.actions[e["entity"]] = e
            old = s.entities.get(e["entity"])
            if (not old or old["kind"] not in {"entity_init", "placement"}
                    or abs(old["t_ms"] - e["t_ms"]) > self.config.settle_ms):
                if not e["warmup"] and e["category"] in self.config.spawn_categories:
                    s.pending[e["entity"]] = e
            self._remember(s, e)
        elif kind == "entity_init":
            old = s.entities.get(e["entity"])
            # Fresh initialization begins a new entity generation (IDs may be reused).
            if old and old["kind"] == "entity_init" and old["t_ms"] == e["t_ms"]:
                return
            if old:
                s.pending.pop(e["entity"], None)
            self._remember(s, e)
            if not e["warmup"] and e["category"] in self.config.spawn_categories:
                s.pending[e["entity"]] = e
        elif kind == "scan_new":
            old = s.entities.get(e["entity"])
            if old is None:
                self._remember(s, e)
                if not e["warmup"] and not self._allowed(e):
                    self._window(s, e, "spatial_changes", self.config.spawn_window_ms, self.config.scan_threshold,
                                 "Newly observed objects in a scanned cell; movement/streaming also possible", "low")
                    self._watch(e)
        elif kind == "explosive_damage" and not e["warmup"] and e["amount"] > 0:
            # Distinct damaged entities, NOT damage callbacks/hitzones or explosions.
            self._window(s, e, "explosive_damage_cluster", self.config.damage_window_ms,
                         self.config.damage_victims_threshold,
                         "Many distinct entities received explosive damage; normal combat can also cause this", "medium")
        elif kind == "explosive_trigger" and not e["warmup"]:
            self._window(s, e, "explosive_trigger_burst", self.config.trigger_window_ms,
                         self.config.trigger_threshold,
                         "Many distinct explosive devices triggered; includes legitimate demolitions", "medium")
        elif kind == "movement_candidate" and not e["warmup"]:
            self._movement(s, e)

        self._finalize(sid, s, force=kind == "session_end")
        if kind == "session_end":
            s.ended = True
        self._bound(s, e)

    def _movement(self, s: Session, e: dict) -> None:
        cfg = self.config
        if not cfg.movement_min_delta_ms <= e["delta_ms"] <= cfg.movement_max_delta_ms:
            return
        previous = [e[k] for k in ("from_x", "from_y", "from_z")]
        current = [e[k] for k in ("x", "y", "z")]
        distance = math.dist(previous, current)
        speed = distance * 1000 / e["delta_ms"]
        vehicle = e["motion_mode"] == "VEHICLE"
        min_distance = cfg.movement_vehicle_distance_m if vehicle else cfg.movement_foot_distance_m
        min_speed = cfg.movement_vehicle_speed_mps if vehicle else cfg.movement_foot_speed_mps
        if distance < min_distance or speed < min_speed:
            return
        key = ("movement_anomaly", e["actor_uid"] or str(e["actor_id"]), e["entity"])
        if e["t_ms"] < s.cooldowns.get(key, -1):
            return
        s.cooldowns[key] = e["t_ms"] + cfg.movement_cooldown_ms
        self._alert(e, "movement_anomaly", "medium",
                    "Abrupt server-observed displacement; verify legitimate teleport, physics and replication corrections", [e],
                    {"motion_mode": e["motion_mode"], "distance_m": round(distance, 3),
                     "time_delta_s": e["delta_ms"] / 1000, "avg_speed_mps": round(speed, 3),
                     "inst_speed_mps": round(e["inst_speed_mps"], 3) if e["inst_speed_valid"] else None,
                     "from_position": previous, "to_position": current,
                     "tracked_entity": e["entity"], "controlled_entity": e["source_entity"]})

    def _remember(self, s: Session, e: dict) -> None:
        s.entities[e["entity"]] = e
        s.entities.move_to_end(e["entity"])

    def _allowed(self, e: dict) -> bool:
        return e["prefab"] in self.config.allowed_prefabs

    def _watch(self, e: dict) -> None:
        if e["prefab"] in self.config.watch_prefabs:
            self._alert(e, "watched_prefab", "medium", "Observed an administrator-configured prefab of interest", [e])

    def _action(self, s: Session, e: dict) -> dict | None:
        key = e["entity"]
        visited: set[str] = set()
        for _ in range(16):
            if not key or key in visited:
                break
            visited.add(key)
            action = s.actions.get(key)
            if (action and abs(action["t_ms"] - e["t_ms"]) <= self.config.settle_ms
                    and action["source"] in self.config.known_routes):
                return action
            parent = s.entities.get(key)
            key = parent.get("parent", "") if parent else ""
        return None

    def _finalize(self, sid: str, s: Session, force: bool = False) -> None:
        for key, observation in list(s.pending.items()):
            if not force and s.now - observation["t_ms"] < self.config.settle_ms:
                continue
            del s.pending[key]
            if self._allowed(observation):
                continue
            e = dict(observation)
            action = self._action(s, e)
            e.update(actor_id=0, actor_uid="", actor_name="", attribution="unknown")
            if action:
                e["route_observation_seq"] = action["seq"]
                e["route"] = action["source"]
                for k in ("actor_id", "actor_uid", "actor_name", "attribution"):
                    e[k] = action[k]
            self._watch(e)
            self._window(s, e, "spawn_burst", self.config.spawn_window_ms, self.config.spawn_threshold,
                         "Many initialized objects; even known construction routes need review at this rate", "medium")
            if action is None:
                self._window(s, e, "unattributed_spawn_burst", self.config.spawn_window_ms,
                             self.config.unattributed_threshold,
                             "Object burst without a matching instrumented placement route; custom mods can cause this", "medium")

    def _window(self, s: Session, e: dict, rule: str, duration: int, threshold: int, reason: str, severity: str) -> None:
        cell = (math.floor(e["x"] / self.config.cell_meters), math.floor(e["z"] / self.config.cell_meters))
        key = (rule, *cell)
        bucket = s.windows.setdefault(key, deque())
        while len(s.windows) > self.config.max_windows:
            old_key = next(iter(s.windows))
            del s.windows[old_key]
            s.cooldowns.pop(old_key, None)
            self._coverage_once(s, e, "Spatial window budget exceeded")
        # Event-time comparisons tolerate buffered writes. Never compare across sessions.
        cutoff = e["t_ms"] - duration
        while bucket and bucket[0]["t_ms"] < cutoff:
            bucket.popleft()
        if not any(x["entity"] == e["entity"] for x in bucket):
            bucket.append(e)
        if len(bucket) > self.config.max_window_events:
            bucket.popleft()
            self._coverage_once(s, e, "Window event budget exceeded")
        relevant = [x for x in bucket if cutoff <= x["t_ms"] <= e["t_ms"]]
        if len(relevant) < threshold or e["t_ms"] < s.cooldowns.get(key, -1):
            return
        s.cooldowns[key] = e["t_ms"] + self.config.cooldown_ms
        self._alert(e, rule, severity, reason, relevant)

    def _coverage_once(self, s: Session, e: dict, reason: str) -> None:
        key = ("internal_coverage",)
        if s.now >= s.cooldowns.get(key, -1):
            s.cooldowns[key] = s.now + self.config.cooldown_ms
            self._alert(e, "coverage_gap", "warning", reason, [e])

    def _bound(self, s: Session, e: dict) -> None:
        while len(s.entities) > self.config.max_entities:
            key, _ = s.entities.popitem(last=False)
            s.pending.pop(key, None)
            s.actions.pop(key, None)
            self._coverage_once(s, e, "Entity state budget exceeded; old observations evicted")
        while len(s.windows) > self.config.max_windows:
            key = next(iter(s.windows))
            del s.windows[key]
            s.cooldowns.pop(key, None)
            self._coverage_once(s, e, "Spatial window budget exceeded")

    def _sweep(self, s: Session) -> None:
        if s.now - s.last_sweep < 5000:
            return
        s.last_sweep = s.now
        keep = max(self.config.spawn_window_ms, self.config.trigger_window_ms,
                   self.config.damage_window_ms) + self.config.settle_ms + 10000
        s.actions = {k: v for k, v in s.actions.items() if s.now - v["t_ms"] <= keep}
        s.windows = {k: v for k, v in s.windows.items() if v and s.now - v[-1]["t_ms"] <= keep}
        s.cooldowns = {k: v for k, v in s.cooldowns.items() if v >= s.now}

    def _alert(self, e: dict, rule: str, severity: str, reason: str, evidence: list[dict], metrics: dict | None = None) -> None:
        seqs = sorted({item["seq"] for item in evidence})
        token = json.dumps([e["session"], rule, seqs], separators=(",", ":"))
        actors: dict[tuple, dict] = {}
        for item in evidence:
            if rule in {"coverage_gap", "sequence_gap", "collector_silent"}:
                continue  # The last observed player did not cause a telemetry outage.
            if item["actor_id"] <= 0 or item["attribution"] not in {"server_action_callback", "engine_instigator", "server_movement_sample"}:
                continue
            key = (item["actor_id"], item["actor_uid"], item["attribution"])
            actors[key] = {"player_id": item["actor_id"], "uid": item["actor_uid"],
                           "name": item["actor_name"], "basis": item["attribution"]}
        alert = {
            "schema": 1, "alert_id": hashlib.sha256(token.encode()).hexdigest()[:20],
            "session": e["session"], "rule": rule, "severity": severity,
            "verdict": "needs_human_review", "reason": reason,
            "t_ms": e["t_ms"], "utc": e["utc"], "position": [e["x"], e["y"], e["z"]],
            "observation_count": len(evidence), "evidence_seq": seqs[:64],
            "evidence_truncated": len(seqs) > 64,
            "reported_actors": list(actors.values())[:32],
            "actor_caution": "Recorded callback/instigator data is not proof of cheat software or authenticated RPC authorship.",
            "samples": [{k: v[k] for k in ("seq", "entity", "prefab", "source", "attribution")}
                        for v in evidence[:8]],
        }
        if metrics is not None:
            alert["metrics"] = metrics
            alert["actor_caution"] = "Movement identifies the observed character/driver, not the cause of the displacement or cheat software."
        self.alert_count += 1
        self.emit(alert)

    def finish(self) -> None:
        for sid, state in self.sessions.items():
            self._finalize(sid, state, force=True)

    def report_silence(self) -> None:
        if self.last_event:
            self._alert(self.last_event, "collector_silent", "warning",
                        "No new telemetry for 45 wall-clock seconds; inspect collector, server and file access", [self.last_event])


class TailReader:
    """Read closed JSONL lines, holding a partial final line until the next poll."""
    MAX_LINE = 65536

    def __init__(self):
        self.positions: dict[str, tuple[int, int, int]] = {}

    def read(self, path: Path, follow: bool = False):
        st = path.stat()
        key = str(path.resolve())
        previous = self.positions.get(key)
        identity = (st.st_dev, st.st_ino)
        offset = previous[2] if previous and previous[:2] == identity and st.st_size >= previous[2] else 0
        with path.open("rb") as stream:
            stream.seek(offset)
            while True:
                start = stream.tell()
                line = stream.readline(self.MAX_LINE + 1)
                if not line:
                    break
                if len(line) > self.MAX_LINE:
                    # Fail explicitly rather than allocating an unbounded line.
                    raise ValueError(f"Telemetry line exceeds {self.MAX_LINE} bytes: {path}")
                if follow and not line.endswith(b"\n"):
                    stream.seek(start)
                    break
                offset = stream.tell()
                self.positions[key] = (*identity, offset)
                if line.strip():
                    yield json.loads(line)
            self.positions[key] = (*identity, stream.tell())


def input_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise ValueError(f"Input does not exist: {path}")
    files = list(path.glob("events-*.jsonl"))
    if len(files) > 512:
        raise ValueError("More than 512 telemetry files; archive older sessions or select one file")
    def key(p: Path):
        prefix, _, part = p.stem.rpartition("-")
        return (prefix, int(part) if part.isdigit() else 0)
    return sorted(files, key=key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="JSONL file or collector directory")
    parser.add_argument("--output", type=Path, required=True, help="Alert JSONL; append in follow mode")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--follow", action="store_true", help="Poll closed lines and rotated files")
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.config)
        files = input_files(args.input)
        if args.output.resolve() in {p.resolve() for p in files}:
            raise ValueError("Output must not overwrite telemetry")
        if args.input.is_dir() and args.output.resolve().parent == args.input.resolve() and args.output.name.startswith("events-"):
            raise ValueError("Output must not match the input file pattern")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("a" if args.follow else "w", encoding="utf-8") as output:
            def emit(alert: dict) -> None:
                line = json.dumps(alert, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
                output.write(line + "\n")
                output.flush()
                print(line, flush=True)
            analyzer = Analyzer(config, emit)
            reader = TailReader()
            interrupted = False
            last_progress = time.monotonic()
            silence_reported = False
            try:
                while True:
                    progress = False
                    active_files = input_files(args.input)
                    active_keys = {str(p.resolve()) for p in active_files}
                    reader.positions = {k: v for k, v in reader.positions.items() if k in active_keys}
                    for path in active_files:
                        for raw in reader.read(path, args.follow):
                            progress = True
                            analyzer.process(raw)
                    if not args.follow:
                        if not progress:
                            break
                    else:
                        if progress:
                            last_progress = time.monotonic()
                            silence_reported = False
                        elif not silence_reported and time.monotonic() - last_progress >= 45:
                            analyzer.report_silence()
                            print("ConflictAudit: no new telemetry for 45 seconds", file=sys.stderr)
                            silence_reported = True
                        time.sleep(1)
            except KeyboardInterrupt:
                interrupted = True
            if not args.follow:
                analyzer.finish()
            # Do not force-finalize recent events when stopping a live reader:
            # their matching server callback might still be buffered on disk.
            print(json.dumps({"processed": analyzer.processed, "alerts": analyzer.alert_count,
                              "duplicates": analyzer.duplicates, "interrupted": interrupted}), file=sys.stderr)
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f"ConflictAudit: stopped: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
