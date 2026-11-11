import copy
import json
from pathlib import Path
import tempfile
import unittest

from analyzer.checker import Analyzer, Config
from examples.make_spawn_demo import BUILDING_PREFAB, ENTITY_PREFAB, VEHICLE_PREFAB, generate
from notifier.discord_bot import Outbox, payload_for, validate_alert


def spawn(seq, *, kind="entity_init", t=None, category="GENERIC", prefab=ENTITY_PREFAB, **extra):
    result = dict(schema=1, session="spawn-test", seq=seq, utc=1794387600,
                  t_ms=70000 + (seq - 1) * 100 if t is None else t, kind=kind,
                  source="editable_component", entity=f"object-{seq}", category=category,
                  prefab=prefab, x=20, y=0, z=20)
    result.update(extra)
    return result


def batch(config=None, *, category="GENERIC", prefab=ENTITY_PREFAB, known=False, count=None, **extra):
    analyzer = Analyzer(config)
    for i in range(count or (12 if known else 6)):
        row = spawn(i + 1, category=category, prefab=prefab, **extra)
        if known:
            row.update(kind="placement", source="conflict_request", actor_id=4,
                       actor_uid="synthetic-player-4", actor_name="DemoSpawner", attribution="server_action_callback")
        analyzer.process(row)
    analyzer.finish()
    return analyzer


class SpawnAnalysisTests(unittest.TestCase):
    def test_vehicle_metrics_and_callback_actor_count(self):
        result = batch(category="VEHICLE", prefab=VEHICLE_PREFAB, known=True).alerts[0]
        self.assertEqual(result["rule"], "spawn_burst")
        self.assertEqual(result["metrics"]["object_kind"], "VEHICLE")
        self.assertEqual(result["metrics"]["object_counts"], dict(VEHICLE=12, BUILDING=0, ENTITY=0))
        self.assertEqual(result["metrics"]["time_delta_s"], 1.1)
        self.assertEqual(result["metrics"]["window_s"], 10)
        self.assertEqual(result["metrics"]["unknown_route_count"], 0)
        self.assertEqual(result["reported_actors"][0]["observation_count"], 12)

    def test_generic_building_needs_explicit_mapping_but_is_always_observed(self):
        ordinary = batch(prefab=BUILDING_PREFAB).alerts[0]
        mapped = batch(Config(building_prefabs=[BUILDING_PREFAB]), prefab=BUILDING_PREFAB).alerts[0]
        self.assertEqual(ordinary["metrics"]["object_kind"], "ENTITY")
        self.assertEqual(mapped["metrics"]["object_kind"], "BUILDING")
        self.assertEqual(ordinary["alert_id"], mapped["alert_id"])

    def test_custom_vehicle_prefab_mapping(self):
        result = batch(Config(vehicle_prefabs=[VEHICLE_PREFAB]), prefab=VEHICLE_PREFAB).alerts[0]
        self.assertEqual(result["metrics"]["object_kind"], "VEHICLE")

    def test_unknown_spawn_does_not_accuse_nearby_player(self):
        result = batch(actor_id=99, actor_uid="nearby", actor_name="Nearby",
                       attribution="server_action_callback").alerts[0]
        self.assertEqual(result["reported_actors"], [])
        self.assertEqual(result["metrics"]["unknown_route_count"], 6)
        self.assertEqual(result["metrics"]["unattributed_count"], 6)

    def test_mixed_group_counts_all_types(self):
        analyzer = Analyzer(Config(building_prefabs=[BUILDING_PREFAB]))
        for index, kind in enumerate(["VEHICLE", "BUILDING", "ENTITY"] * 2):
            analyzer.process(spawn(index + 1, category="VEHICLE" if kind == "VEHICLE" else "GENERIC",
                                   prefab={"VEHICLE": VEHICLE_PREFAB, "BUILDING": BUILDING_PREFAB, "ENTITY": ENTITY_PREFAB}[kind]))
        analyzer.finish()
        self.assertEqual(len(analyzer.alerts), 1)
        metrics = analyzer.alerts[0]["metrics"]
        self.assertEqual(metrics["object_kind"], "MIXED")
        self.assertEqual(metrics["object_counts"], dict(VEHICLE=2, BUILDING=2, ENTITY=2))

    def test_unknown_burst_does_not_also_raise_general_burst(self):
        result = batch(count=30)
        self.assertEqual([a["rule"] for a in result.alerts], ["unattributed_spawn_burst"])

    def test_same_threshold_prefers_unknown_route_reason(self):
        result = batch(Config(spawn_threshold=6, unattributed_threshold=6))
        self.assertEqual([a["rule"] for a in result.alerts], ["unattributed_spawn_burst"])

    def test_later_episode_after_cooldown_still_alerts(self):
        analyzer = batch(count=12)
        for i in range(6):
            analyzer.process(spawn(13 + i, t=110000 + i * 100))
        analyzer.finish()
        self.assertEqual(len(analyzer.alerts), 2)

    def test_other_cell_and_session_not_suppressed(self):
        analyzer = batch(count=12)
        for i in range(6):
            analyzer.process(spawn(13 + i, t=72000 + i * 100, x=1000))
        for i in range(6):
            analyzer.process(spawn(i + 1, session="another-session"))
        analyzer.finish()
        self.assertEqual(len(analyzer.alerts), 3)

    def test_each_actor_has_own_count_in_shared_group(self):
        analyzer = Analyzer()
        for i in range(12):
            player = 1 if i < 8 else 2
            analyzer.process(spawn(i + 1, kind="placement", source="conflict_request",
                                   actor_id=player, actor_uid=f"demo-{player}", attribution="server_action_callback"))
        analyzer.finish()
        result = analyzer.alerts[0]
        self.assertEqual(result["metrics"]["count"], 12)
        self.assertEqual({a["player_id"]: a["observation_count"] for a in result["reported_actors"]}, {1: 8, 2: 4})

    def test_zero_duration_has_no_infinite_rate(self):
        result = batch(t=70000).alerts[0]
        self.assertEqual(result["metrics"]["time_delta_s"], 0)
        self.assertIsNone(result["metrics"]["observed_rate_per_s"])

    def test_scan_remains_low_confidence_observation(self):
        analyzer = Analyzer(Config(building_prefabs=[BUILDING_PREFAB]))
        for i in range(8):
            analyzer.process(spawn(i + 1, kind="scan_new", source="spatial_scan", prefab=BUILDING_PREFAB))
        result = analyzer.alerts[0]
        self.assertEqual(result["rule"], "spatial_changes")
        self.assertEqual(result["severity"], "low")
        self.assertEqual(result["metrics"]["object_kind"], "BUILDING")
        self.assertEqual(result["reported_actors"], [])

    def test_watchlist_has_single_object_metrics(self):
        result = batch(Config(watch_prefabs=[ENTITY_PREFAB]), count=1).alerts[0]
        self.assertEqual(result["rule"], "watched_prefab")
        self.assertEqual(result["metrics"]["count"], 1)
        self.assertIsNone(result["metrics"]["window_s"])

    def test_classification_does_not_whitelist(self):
        result = batch(Config(building_prefabs=[BUILDING_PREFAB]), prefab=BUILDING_PREFAB)
        self.assertEqual(len(result.alerts), 1)
        allowed = batch(Config(building_prefabs=[BUILDING_PREFAB], allowed_prefabs=[BUILDING_PREFAB]), prefab=BUILDING_PREFAB)
        self.assertEqual(allowed.alerts, [])

    def test_invalid_classification_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            for values in [dict(building_prefabs="all"), dict(vehicle_prefabs=[12]),
                           dict(vehicle_prefabs=[VEHICLE_PREFAB], building_prefabs=[VEHICLE_PREFAB])]:
                path.write_text(json.dumps(values))
                with self.subTest(values=values), self.assertRaises(ValueError):
                    Config.load(path)


class SpawnMessageTests(unittest.TestCase):
    def test_screenshot_order_type_actor_uid_then_detection_and_metrics(self):
        result = batch(category="VEHICLE", prefab=VEHICLE_PREFAB, known=True).alerts[0]
        content = payload_for(result, "Server 1")["content"]
        self.assertLess(content.index("Server 1"), content.index("[VEHICLE] DemoSpawner"))
        self.assertLess(content.index("[synthetic-player-4]"), content.index("Підозрілий спавн"))
        for expected in ["count=12", "time_delta=1.1 s", "window=10 s", "VEHICLE=12", "Prefab:",
                         "route_unknown=0", "actor_unknown=0", "UTC:", "Alert:", "не доказ"]:
            self.assertIn(expected, content)

    def test_building_unknown_actor_and_generic_entity_have_same_layout(self):
        for config, prefab, kind in [(Config(building_prefabs=[BUILDING_PREFAB]), BUILDING_PREFAB, "BUILDING"),
                                    (Config(), ENTITY_PREFAB, "ENTITY")]:
            text = payload_for(batch(config, prefab=prefab).alerts[0], "Server 2")["content"]
            self.assertIn(f"[{kind}] Гравця не встановлено", text)
            self.assertIn("[UID невідомий]", text)
            self.assertIn("count=6; time_delta=0.5 s", text)
            self.assertIn("route_unknown=6", text)

    def test_long_names_keep_footer_and_cannot_ping(self):
        result = batch(known=True).alerts[0]
        result["reported_actors"] = [dict(name="@everyone\n" + "😀" * 1000, uid="🦊" * 1000)] * 2
        result["metrics"]["prefabs"] = [dict(prefab="@everyone" + "😀" * 1000, count=12)] * 8
        payload = payload_for(result, "😀" * 1000)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertLessEqual(len(payload["content"].encode("utf-16-le")) // 2, 1900)
        self.assertIn("Alert: " + result["alert_id"], payload["content"])
        self.assertIn("Це не доказ", payload["content"])
        self.assertNotIn("\n@everyone", payload["content"])

    def test_old_alert_without_object_metrics_is_supported(self):
        result = batch().alerts[0]
        del result["metrics"]
        text = payload_for(result, "Test")["content"]
        self.assertIn("count=6", text)
        self.assertIn("time_delta=н/д", text)

    def test_bad_object_metrics_fail_explicitly(self):
        original = batch().alerts[0]
        changes = [dict(object_kind=[]), dict(object_counts={"ENTITY": 6}), dict(count=9),
                   dict(unattributed_count=-1), dict(observed_rate_per_s=float("inf")),
                   dict(prefabs=[{"prefab": "x", "count": "six"}])]
        for change in changes:
            value = copy.deepcopy(original)
            value["metrics"].update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_alert(value)

    def test_scanner_message_does_not_claim_proven_spawn(self):
        result = batch().alerts[0]
        result["rule"] = "spatial_changes"
        text = payload_for(result, "Test")["content"]
        self.assertIn("можливі рух або streaming", text)

    def test_demo_covers_four_types_and_outbox_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events-spawn.jsonl"
            generate(path)
            analyzer = Analyzer(Config(building_prefabs=[BUILDING_PREFAB]))
            for line in path.read_text().splitlines():
                analyzer.process(json.loads(line))
            analyzer.finish()
            self.assertEqual([a["metrics"]["object_kind"] for a in analyzer.alerts], ["VEHICLE", "BUILDING", "ENTITY", "MIXED"])
            alerts = Path(tmp) / "alerts.jsonl"
            alerts.write_text("".join(json.dumps(a, ensure_ascii=False) + "\n" for a in analyzer.alerts), encoding="utf-8")
            box = Outbox(Path(tmp) / "state.sqlite3")
            try:
                self.assertEqual(box.ingest(alerts, "s1", "Server 1"), 4)
                self.assertEqual(box.ingest(alerts, "s1", "Server 1"), 0)
                payloads = [json.loads(row[0]) for row in box.db.execute("SELECT payload FROM alerts ORDER BY rowid")]
                self.assertIn("[MIXED]", payloads[-1]["content"])
            finally:
                box.close()
