#!/usr/bin/env python3
"""Deliver ConflictAudit alerts via a Discord webhook. Preview only unless --send.

Python 3.10+, standard library only. No Discord gateway bot or game credentials.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import unicodedata
from urllib import error, parse, request
import uuid


MAX_LINE = 65536
MAX_PENDING = 10000
OBJECT_RULES = {"spawn_burst", "unattributed_spawn_burst", "spatial_changes", "watched_prefab"}
OBJECT_KINDS = ("VEHICLE", "BUILDING", "ENTITY", "MIXED")
LABELS = {
    "movement_anomaly": "Підозріле переміщення / можливий телепорт",
    "spawn_burst": "Підозрілий спавн: перевищено поріг кількості об’єктів",
    "unattributed_spawn_burst": "Підозрілий спавн: шлях створення не встановлено",
    "spatial_changes": "Нові об’єкти у перевіреній зоні",
    "explosive_damage_cluster": "Сплеск вибухових пошкоджень",
    "explosive_trigger_burst": "Масове спрацювання вибухових пристроїв",
    "watched_prefab": "Об’єкт зі списку спостереження",
    "coverage_gap": "Неповна телеметрія",
    "sequence_gap": "Пропущені записи телеметрії",
    "collector_silent": "Немає нової телеметрії понад 45 секунд",
}


def clean(value: object, limit: int = 120) -> str:
    """Keep names on one line, remove bidi/control codes, neutralize Markdown."""
    text = str(value)
    text = " ".join("".join(c if not unicodedata.category(c).startswith("C") else " " for c in text).split())
    text = text[:limit]
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|~@])", r"\\\1", text)


def number(value: object) -> str:
    if type(value) not in (float, int) or abs(value) > 1e15 or not math.isfinite(value):
        return "н/д"
    return f"{value:.3f}".rstrip("0").rstrip(".")


def position(value: object) -> str:
    if not isinstance(value, list) or len(value) != 3:
        return "н/д"
    return ", ".join(number(v) for v in value)


def validate_alert(value: object) -> dict:
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1:
        raise ValueError("Expected schema 1 alert")
    for key in ("alert_id", "session", "rule"):
        if not isinstance(value.get(key), str) or not value[key] or len(value[key]) > 128 or any(ord(c) < 32 for c in value[key]):
            raise ValueError(f"Invalid alert {key}")
    if value.get("verdict") != "needs_human_review":
        raise ValueError("Expected a review-only alert")
    actors = value.get("reported_actors", [])
    if not isinstance(actors, list) or len(actors) > 32 or any(not isinstance(a, dict) for a in actors):
        raise ValueError("Invalid reported actors")
    for actor in actors:
        for key in ("name", "uid"):
            if not isinstance(actor.get(key, ""), str) or len(actor.get(key, "")) > 2048:
                raise ValueError("Invalid actor text")
    if "metrics" in value and not isinstance(value["metrics"], dict):
        raise ValueError("Invalid metrics")
    metrics = value.get("metrics", {})
    if "motion_mode" in metrics and metrics["motion_mode"] not in ("FOOT", "VEHICLE"):
        raise ValueError("Invalid motion mode")
    for key in ("distance_m", "time_delta_s", "avg_speed_mps", "inst_speed_mps"):
        number_value = metrics.get(key)
        if number_value is not None and (type(number_value) not in (int, float) or abs(number_value) > 1e15 or not math.isfinite(number_value)):
            raise ValueError("Invalid movement metric")
    if "object_kind" in metrics:
        if metrics["object_kind"] not in OBJECT_KINDS:
            raise ValueError("Invalid object kind")
        counts = metrics.get("object_counts")
        if (not isinstance(counts, dict) or set(counts) != set(OBJECT_KINDS[:3])
                or any(type(v) is not int or not 0 <= v <= 2**31 - 1 for v in counts.values())):
            raise ValueError("Invalid object counts")
        if type(metrics.get("count")) is not int or metrics["count"] <= 0 or sum(counts.values()) != metrics["count"]:
            raise ValueError("Inconsistent object counts")
        for key in ("unknown_route_count", "unattributed_count"):
            if type(metrics.get(key)) is not int or not 0 <= metrics[key] <= metrics["count"]:
                raise ValueError("Invalid attribution count")
        for key in ("window_s", "observed_rate_per_s"):
            v = metrics.get(key)
            if v is not None and (type(v) not in (int, float) or not 0 <= v <= 1e15 or not math.isfinite(v)):
                raise ValueError("Invalid object metric")
        prefabs = metrics.get("prefabs", [])
        if not isinstance(prefabs, list) or len(prefabs) > 8:
            raise ValueError("Invalid prefab summary")
        for row in prefabs:
            if (not isinstance(row, dict) or not isinstance(row.get("prefab"), str) or len(row["prefab"]) > 2048
                    or type(row.get("count")) is not int or not 1 <= row["count"] <= metrics["count"]):
                raise ValueError("Invalid prefab count")
    authority = value.get("authorization")
    if authority is not None:
        if not isinstance(authority, dict) or authority.get("status") not in ("unverified", "gm_origin", "gm_authorized", "mixed"):
            raise ValueError("Invalid authorization summary")
        for key in ("authorized_count", "gm_origin_count", "unverified_count"):
            if type(authority.get(key)) is not int or not 0 <= authority[key] <= 2**31 - 1:
                raise ValueError("Invalid authorization count")
        sources = authority.get("sources", [])
        if not isinstance(sources, list) or len(sources) > 8:
            raise ValueError("Invalid authorization sources")
        for source in sources:
            if not isinstance(source, dict) or source.get("role") not in ("ADMIN", "GAME_MASTER"):
                raise ValueError("Invalid authorized role")
            for key in ("uid", "name", "action_id", "basis", "entity"):
                if not isinstance(source.get(key), str) or len(source[key]) > 2048:
                    raise ValueError("Invalid authorization source text")
        if authority["status"] == "gm_authorized" and (not authority["authorized_count"] or authority["unverified_count"] or not sources):
            raise ValueError("Incomplete confirmed GM action")
    return value


def payload_for(alert: dict, server_name: str) -> dict:
    validate_alert(alert)
    lines = [f"**{clean(server_name, 80)}**"]
    metrics = alert.get("metrics", {})
    authority = alert.get("authorization", {})
    auth_status = authority.get("status", "unverified")
    objects = alert["rule"] in OBJECT_RULES
    mode = metrics.get("object_kind", "ENTITY") if objects else metrics.get("motion_mode")
    prefix = f"[{mode}] " if mode in (*OBJECT_KINDS, "FOOT") else ""
    actors = alert.get("reported_actors", [])
    if auth_status == "gm_authorized":
        unique = {(x.get("player_id"), x["uid"]): x for x in authority["sources"]}
        actors = list(unique.values())
        lines.append("Ініціатор підтвердженої GM-дії:")
    if not actors:
        lines.extend([prefix + "Гравця не встановлено", "[UID невідомий]"])
    for actor in actors[:2]:
        lines.append(prefix + clean(actor.get("name") or f"Player ID {actor.get('player_id', '?')}", 80))
        lines.append("[" + clean(actor.get("uid") or "UID невідомий", 100) + "]")
        role = actor.get("role", "UNKNOWN")
        if role in ("ADMIN", "GAME_MASTER", "PLAYER"):
            lines.append("role=" + role)
        if objects and "observation_count" in actor:
            lines.append("Пов’язаних із callback гравця об’єктів: " + number(actor["observation_count"]))
    if len(actors) > 2:
        lines.append(f"Ще учасників у локальному записі: {len(actors) - 2}")
    if auth_status == "gm_authorized":
        operation = ("створення об’єктів" if objects else "вибухові пошкодження"
                     if alert["rule"] == "explosive_damage_cluster" else "вибухові тригери")
        lines.append("Підтверджена дія Game Master / адміністратора: " + operation)
    else:
        lines.append(LABELS.get(alert["rule"], clean(alert["rule"])))
    if authority:
        lines.append("authorization=" + auth_status.upper())
        if auth_status == "gm_origin":
            lines.append("Джерело походить із GM-спавну; дозвіл на цей ефект не підтверджено.")
        if authority.get("sources_truncated"):
            lines.append("Джерела GM скорочено; повні зв’язки — у raw-журналі.")
    if alert["rule"] == "movement_anomaly":
        lines.extend([
            f"distance={number(metrics.get('distance_m'))} m; time_delta={number(metrics.get('time_delta_s'))} s",
            f"AVG_SPEED={number(metrics.get('avg_speed_mps'))} m/s; INST_SPEED={number(metrics.get('inst_speed_mps'))} m/s",
            "Звідки: " + position(metrics.get("from_position")),
            "Куди: " + position(metrics.get("to_position")),
        ])
    elif objects:
        lines.extend([
            "count=" + number(metrics.get("count", alert.get("observation_count")))
            + "; time_delta=" + number(metrics.get("time_delta_s")) + " s; window=" + number(metrics.get("window_s")) + " s",
        ])
        counts = metrics.get("object_counts")
        if counts:
            lines.append("; ".join(kind + "=" + number(counts[kind]) for kind in OBJECT_KINDS[:3]))
        lines.append("OBSERVED_RATE=" + number(metrics.get("observed_rate_per_s")) + " /s")
        lines.append("route_unknown=" + number(metrics.get("unknown_route_count"))
                     + "; actor_unknown=" + number(metrics.get("unattributed_count")))
        for row in metrics.get("prefabs", [])[:2]:
            lines.append("Prefab: " + clean(row["prefab"] or "невідомий", 120) + " × " + number(row["count"]))
        if metrics.get("prefabs_truncated") or len(metrics.get("prefabs", [])) > 2:
            lines.append("Інші prefab — у локальному alert.")
        lines.append("Координати: " + position(alert.get("position")))
    else:
        lines.append("Кількість спостережень: " + number(alert.get("observation_count")))
        lines.append("Координати: " + position(alert.get("position")))
    utc = alert.get("utc")
    stamp = "н/д"
    if type(utc) is int and 0 <= utc <= 253402300799:
        stamp = datetime.fromtimestamp(utc, timezone.utc).isoformat()
    footer = ["UTC: " + stamp, "Alert: " + clean(alert["alert_id"], 128),
              "Потрібна перевірка адміністратора. Це не доказ використання читів."]
    if alert["rule"] == "movement_anomaly":
        footer.append("Зазначено персонажа/водія; причина переміщення невідома.")
    elif objects:
        footer.append("Кількість стосується групи спостережень; це не доведений спавн одним гравцем.")
        if alert["rule"] == "spatial_changes":
            footer.append("Це нові спостереження: можливі рух або streaming, а не новий спавн.")
    if auth_status == "gm_authorized":
        footer.append("Дозвіл стосується записаної дії; зловживання повноваженнями перевіряються вручну.")
    # Keep alert ID and the review caveat even with long Unicode names/prefabs.
    tail = "\n" + "\n".join(footer)
    budget = 3800 - len(tail.encode("utf-16-le"))
    body = "\n".join(lines).encode("utf-16-le")
    if len(body) > budget:
        body = body[:budget - 2].decode("utf-16-le", errors="ignore").rstrip("\\").encode("utf-16-le") + "…".encode("utf-16-le")
    content = body.decode("utf-16-le") + tail
    return {"username": "ConflictAudit", "content": content, "allowed_mentions": {"parse": []}}


def webhook_url(raw: str) -> str:
    try:
        url = parse.urlsplit(raw)
        valid = (url.scheme == "https" and url.netloc == "discord.com" and not url.fragment
                 and re.fullmatch(r"/api/(?:v[0-9]+/)?webhooks/[0-9]{5,25}/[A-Za-z0-9._-]{10,}", url.path))
        query = parse.parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
        valid = valid and set(query) <= {"wait", "thread_id"}
        if "thread_id" in query:
            valid = valid and len(query["thread_id"]) == 1 and bool(re.fullmatch(r"[0-9]{5,25}", query["thread_id"][0]))
        if not valid:
            raise ValueError()
    except ValueError:
        # Do not put the URL/token into exception text or logs.
        raise ValueError("Invalid Discord webhook URL") from None
    query["wait"] = ["true"]
    return parse.urlunsplit(("https", "discord.com", url.path, parse.urlencode(query, doseq=True), ""))


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: dict


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Webhook:
    def __init__(self, url: str):
        self.url = webhook_url(url)
        self.opener = request.build_opener(NoRedirect())

    def post(self, payload: dict) -> Reply:
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        req = request.Request(self.url, data, {"Content-Type": "application/json", "User-Agent": "ConflictAudit/1.0"}, method="POST")
        try:
            response = self.opener.open(req, timeout=10)
        except error.HTTPError as exc:
            response = exc
        # All URL/network exception messages are redacted by Worker/main.
        with response:
            raw = response.read(MAX_LINE + 1)
            try:
                body = json.loads(raw) if len(raw) <= MAX_LINE else {}
            except (ValueError, UnicodeError):
                body = {}
            return Reply(response.code, {k.lower(): v for k, v in response.headers.items()}, body if isinstance(body, dict) else {})


class Outbox:
    """Atomic file cursor + outbox; sent IDs retained to deduplicate replays."""
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS alerts (
                server TEXT NOT NULL, alert_id TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
                available REAL NOT NULL DEFAULT 0, lease TEXT, leased_until REAL NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '', PRIMARY KEY(server, alert_id));
            CREATE TABLE IF NOT EXISTS cursors (
                server TEXT NOT NULL, path TEXT NOT NULL, device INTEGER, inode INTEGER,
                offset INTEGER NOT NULL, fingerprint TEXT NOT NULL, PRIMARY KEY(server, path));
            CREATE TABLE IF NOT EXISTS pacing (server TEXT PRIMARY KEY, available REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS ready_alerts ON alerts(server, status, available);
        """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    @staticmethod
    def fingerprint(stream, offset: int) -> str:
        stream.seek(0)
        prefix = stream.read(min(offset, 512))
        stream.seek(max(0, offset - 512))
        suffix = stream.read(min(offset, 512))
        return hashlib.sha256(prefix + suffix).hexdigest()

    def ingest(self, path: Path, server: str, name: str, max_pending: int = MAX_PENDING) -> int:
        path = path.resolve()
        inserted = 0
        with path.open("rb") as stream, self.transaction():
            stat = os.fstat(stream.fileno())
            cursor = self.db.execute("SELECT * FROM cursors WHERE server=? AND path=?", (server, str(path))).fetchone()
            offset = 0
            if (cursor and (cursor["device"], cursor["inode"]) == (stat.st_dev, stat.st_ino)
                    and cursor["offset"] <= stat.st_size and self.fingerprint(stream, cursor["offset"]) == cursor["fingerprint"]):
                offset = cursor["offset"]
            pending = self.db.execute("SELECT COUNT(*) FROM alerts WHERE server=? AND status='queued'", (server,)).fetchone()[0]
            stream.seek(offset)
            for _ in range(1000):
                line = stream.readline(MAX_LINE + 1)
                if len(line) > MAX_LINE:
                    raise ValueError("Alert line exceeds 64 KiB; input cursor not advanced")
                if not line or not line.endswith(b"\n"):
                    break
                if line.strip():
                    alert = validate_alert(json.loads(line))
                    exists = self.db.execute("SELECT 1 FROM alerts WHERE server=? AND alert_id=?", (server, alert["alert_id"])).fetchone()
                    if not exists:
                        if pending >= max_pending:
                            break  # Do not consume a record we cannot persist.
                        payload = json.dumps(payload_for(alert, name), ensure_ascii=False, allow_nan=False)
                        self.db.execute("INSERT INTO alerts(server,alert_id,payload) VALUES(?,?,?)", (server, alert["alert_id"], payload))
                        pending += 1
                        inserted += 1
                offset = stream.tell()
            fingerprint = self.fingerprint(stream, offset)
            self.db.execute("INSERT OR REPLACE INTO cursors VALUES(?,?,?,?,?,?)",
                            (server, str(path), stat.st_dev, stat.st_ino, offset, fingerprint))
        return inserted

    def claim(self, server: str, now: float) -> dict | None:
        with self.transaction():
            pace = self.db.execute("SELECT available FROM pacing WHERE server=?", (server,)).fetchone()
            if pace and pace[0] > now:
                return None
            row = self.db.execute("SELECT * FROM alerts WHERE server=? AND status='queued' AND available<=? AND leased_until<=? ORDER BY rowid LIMIT 1",
                                  (server, now, now)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["lease"] = uuid.uuid4().hex
            item["attempts"] += 1
            self.db.execute("UPDATE alerts SET lease=?, leased_until=?, attempts=? WHERE server=? AND alert_id=?",
                            (item["lease"], now + 60, item["attempts"], server, item["alert_id"]))
            self.db.execute("INSERT OR REPLACE INTO pacing VALUES(?,?)", (server, now + 1))
            return item

    def complete(self, item: dict, now: float, status: str, delay: float, reason: str = ""):
        with self.transaction():
            self.db.execute("""UPDATE alerts SET status=?, available=?, lease=NULL, leased_until=0, error=?,
                            payload=CASE WHEN ?='sent' THEN '{}' ELSE payload END
                            WHERE server=? AND alert_id=? AND lease=?""",
                            (status, now + delay, reason, status, item["server"], item["alert_id"], item["lease"]))
            self.db.execute("INSERT INTO pacing VALUES(?,?) ON CONFLICT(server) DO UPDATE SET available=MAX(available,excluded.available)",
                            (item["server"], now + max(1, delay)))

    def counts(self, server: str) -> dict:
        result = {"queued": 0, "sent": 0, "failed": 0}
        result.update(dict(self.db.execute("SELECT status,COUNT(*) FROM alerts WHERE server=? GROUP BY status", (server,))))
        return result


class DeliveryStopped(Exception):
    pass


def seconds(value: object) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) and parsed >= 0 else 0
    except (ValueError, TypeError):
        return 0


class Worker:
    def __init__(self, outbox: Outbox, server: str, transport):
        self.outbox, self.server, self.transport = outbox, server, transport

    def once(self, now: float | None = None) -> bool:
        started = time.time() if now is None else now
        item = self.outbox.claim(self.server, started)
        if not item:
            return False
        retry = min(300, 2 ** min(item["attempts"], 9))
        try:
            reply = self.transport.post(json.loads(item["payload"]))
        except (OSError, error.URLError, TimeoutError):
            self.outbox.complete(item, time.time() if now is None else now, "queued", retry, "network_error")
            print("ConflictAudit: network failure; alert retained for retry", file=sys.stderr)
            return True
        finished = time.time() if now is None else now
        cooldown = seconds(reply.headers.get("x-ratelimit-reset-after")) if reply.headers.get("x-ratelimit-remaining") == "0" else 0
        if 200 <= reply.status < 300 and isinstance(reply.body.get("id"), str) and reply.body["id"].isdigit():
            self.outbox.complete(item, finished, "sent", cooldown)
        elif reply.status == 429:
            delay = max(1, cooldown, seconds(reply.headers.get("retry-after")), seconds(reply.body.get("retry_after")))
            self.outbox.complete(item, finished, "queued", delay, "rate_limited")
        elif reply.status in {401, 403, 404} or 300 <= reply.status < 400:
            self.outbox.complete(item, finished, "queued", max(300, cooldown), f"http_{reply.status}")
            raise DeliveryStopped(f"Webhook unavailable (HTTP {reply.status}); check configuration. Queue retained.")
        elif 400 <= reply.status < 500 and reply.status not in {408, 425}:
            self.outbox.complete(item, finished, "failed", cooldown, f"http_{reply.status}")
            print(f"ConflictAudit: alert retained as failed (HTTP {reply.status}); inspect state database", file=sys.stderr)
        else:
            self.outbox.complete(item, finished, "queued", max(retry, cooldown), f"unconfirmed_http_{reply.status}")
            print(f"ConflictAudit: unconfirmed delivery (HTTP {reply.status}); retry scheduled", file=sys.stderr)
        return True


def preview(path: Path, name: str):
    # No database or secret read in preview; a later --send still sees all records.
    with path.open("rb") as stream:
        while True:
            line = stream.readline(MAX_LINE + 1)
            if len(line) > MAX_LINE:
                raise ValueError("Alert line exceeds 64 KiB")
            if not line or not line.endswith(b"\n"):
                break
            if line.strip():
                print(json.dumps(payload_for(validate_alert(json.loads(line)), name), ensure_ascii=False, allow_nan=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Analyzer alert JSONL file (not raw events)")
    parser.add_argument("--server-id", required=True, help="Stable unique ID; keep unchanged across restarts")
    parser.add_argument("--server-name", required=True, help="Display name, e.g. Server 1")
    parser.add_argument("--state", type=Path, default=Path("runtime/discord-state.sqlite3"))
    parser.add_argument("--send", action="store_true", help="Enable real delivery; without --follow runs one queue iteration")
    parser.add_argument("--follow", action="store_true", help="Poll input and deliver/retry continuously (requires --send)")
    parser.add_argument("--webhook-env", default="DISCORD_WEBHOOK_URL", help="Environment variable containing secret URL")
    args = parser.parse_args(argv)
    box = None
    try:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", args.server_id) or not args.server_name.strip():
            raise ValueError("Provide a stable server ID and a nonempty display name")
        if args.follow and not args.send:
            raise ValueError("Preview is one pass; --follow requires --send")
        if not args.send:
            preview(args.input, args.server_name)
            return 0
        if args.input.resolve() == args.state.resolve():
            raise ValueError("State database must not overwrite input")
        url = os.environ.get(args.webhook_env, "")
        if not url:
            raise ValueError("Webhook environment variable is empty")
        transport = Webhook(url)
        args.state.parent.mkdir(parents=True, exist_ok=True)
        box = Outbox(args.state)
        worker = Worker(box, args.server_id, transport)
        missing_reported = False
        while True:
            if args.input.exists():
                box.ingest(args.input, args.server_id, args.server_name)
                missing_reported = False
            elif not args.follow:
                raise ValueError("Alert input file does not exist")
            elif not missing_reported:
                print("ConflictAudit: waiting for alert input file", file=sys.stderr)
                missing_reported = True
            worker.once()
            if not args.follow:
                counts = box.counts(args.server_id)
                print(json.dumps(counts), file=sys.stderr)
                return 3 if counts["queued"] or counts["failed"] else 0
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, sqlite3.Error, DeliveryStopped) as exc:
        # JSON/OS exceptions may include supplied text: only explicit sanitized
        # delivery errors are displayed. Never print exception URL/response bodies.
        message = str(exc) if isinstance(exc, DeliveryStopped) else type(exc).__name__ + ": check input, state and configuration"
        print("ConflictAudit: stopped: " + message, file=sys.stderr)
        return 2
    finally:
        if box:
            box.close()


if __name__ == "__main__":
    raise SystemExit(main())
