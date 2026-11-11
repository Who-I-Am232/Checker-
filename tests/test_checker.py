import json
from pathlib import Path
import tempfile
import unittest

from analyzer.checker import Analyzer, Config, TailReader, input_files, main, validate


def event(seq, kind="entity_init", t=70000, entity=None, session="test-session", **extra):
    value = dict(schema=1, session=session, seq=seq, t_ms=t, utc=1794387600 + t // 1000,
                 kind=kind, source="editable_component", entity=entity or f"object-{seq}",
                 parent="", prefab="{TEST}Prefabs/Structures/Example.et", category="GENERIC",
                 x=20, y=0, z=20, actor_id=0, actor_uid="", actor_name="", attribution="unknown",
                 source_entity="", amount=0, count=0, warmup=False)
    value.update(extra)
    return value


class AnalysisTests(unittest.TestCase):
    def rules(self, analyzer):
        return [a["rule"] for a in analyzer.alerts]

    def test_unlogged_object_burst_has_no_accused_player(self):
        a = Analyzer()
        for seq in range(1, 7):
            a.process(event(seq, t=70000 + seq * 100, nearby_player_id=99))
        a.finish()
        self.assertIn("unattributed_spawn_burst", self.rules(a))
        self.assertTrue(all(not x["reported_actors"] for x in a.alerts))

    def test_single_unknown_object_is_recorded_without_burst_alert(self):
        a = Analyzer()
        a.process(event(1))
        a.finish()
        self.assertEqual(a.alerts, [])

    def test_known_build_route_correlates_after_init(self):
        a = Analyzer()
        for i in range(6):
            a.process(event(i * 2 + 1, entity=f"b{i}", t=70000 + i * 100))
            a.process(event(i * 2 + 2, "placement", entity=f"b{i}", t=70005 + i * 100,
                            source="conflict_request", actor_id=4, actor_uid="uid-4", attribution="server_action_callback"))
        a.finish()
        self.assertEqual(a.alerts, [])

    def test_large_known_route_still_requires_review(self):
        a = Analyzer()
        for i in range(12):
            a.process(event(2 * i + 1, entity=f"b{i}", t=70000 + i * 100))
            a.process(event(2 * i + 2, "placement", entity=f"b{i}", t=70010 + i * 100,
                            source="conflict_request", actor_id=7, actor_uid="uid-7", attribution="server_action_callback"))
        a.finish()
        self.assertEqual(self.rules(a), ["spawn_burst"])
        self.assertEqual(a.alerts[0]["reported_actors"][0]["uid"], "uid-7")
        self.assertEqual(a.alerts[0]["verdict"], "needs_human_review")

    def test_placement_without_init_hook_is_still_observed(self):
        a = Analyzer(Config(spawn_threshold=2))
        for i in range(2):
            a.process(event(i + 1, "placement", source="editor_place", actor_id=3,
                            attribution="server_action_callback", entity=f"b{i}"))
        a.finish()
        self.assertEqual(self.rules(a), ["spawn_burst"])

    def test_composition_children_correlate_with_parent(self):
        a = Analyzer()
        a.process(event(1, "placement", entity="root", source="conflict_request", actor_id=4,
                        attribution="server_action_callback", category="SYSTEM"))
        for i in range(6):
            a.process(event(i + 2, entity=f"child{i}", parent="root", t=70010))
        a.finish()
        self.assertNotIn("unattributed_spawn_burst", self.rules(a))

    def test_stale_parent_action_does_not_whitelist_future_spawns(self):
        a = Analyzer()
        a.process(event(1, "placement", entity="root", source="conflict_request", category="SYSTEM"))
        for i in range(6):
            a.process(event(i + 2, entity=f"child{i}", parent="root", t=90000))
        a.finish()
        self.assertIn("unattributed_spawn_burst", self.rules(a))

    def test_startup_restore_is_not_spawn_attack(self):
        a = Analyzer()
        for i in range(30):
            a.process(event(i + 1, t=100 + i, warmup=True))
        a.finish()
        self.assertEqual(a.alerts, [])

    def test_scan_baseline_never_triggers(self):
        a = Analyzer()
        for i in range(30):
            a.process(event(i + 1, "scan_baseline", count=1500))
        self.assertEqual(a.alerts, [])

    def test_scanned_changes_are_low_confidence_and_not_authored(self):
        a = Analyzer()
        for i in range(8):
            a.process(event(i + 1, "scan_new", source="spatial_scan"))
        self.assertEqual(self.rules(a), ["spatial_changes"])
        self.assertEqual(a.alerts[0]["severity"], "low")
        self.assertEqual(a.alerts[0]["reported_actors"], [])

    def test_scanner_does_not_duplicate_init_observations(self):
        a = Analyzer()
        for i in range(8):
            a.process(event(i * 2 + 1, entity=f"b{i}", source="editable_component"))
            a.process(event(i * 2 + 2, "scan_new", entity=f"b{i}", source="spatial_scan"))
        a.finish()
        self.assertNotIn("spatial_changes", self.rules(a))

    def test_many_hitzones_are_not_many_victims(self):
        a = Analyzer()
        for i in range(40):
            a.process(event(i + 1, "explosive_damage", entity="one-victim", t=70000 + i,
                            amount=10, attribution="engine_instigator", actor_id=2))
        self.assertEqual(a.alerts, [])

    def test_damage_cluster_reports_instigator_as_hint(self):
        a = Analyzer()
        for i in range(8):
            a.process(event(i + 1, "explosive_damage", amount=12, actor_id=2, attribution="engine_instigator"))
        self.assertEqual(self.rules(a), ["explosive_damage_cluster"])
        self.assertEqual(a.alerts[0]["reported_actors"][0]["basis"], "engine_instigator")

    def test_distinct_trigger_devices_not_repeat_callback(self):
        a = Analyzer()
        for i in range(20):
            a.process(event(i + 1, "explosive_trigger", entity="same-charge"))
        self.assertEqual(a.alerts, [])
        for i in range(5):
            a.process(event(21 + i, "explosive_trigger", entity=f"charge-{i}"))
        self.assertEqual(self.rules(a), ["explosive_trigger_burst"])

    def test_sessions_cannot_mix_player_slots_or_rate_windows(self):
        a = Analyzer()
        for session in ("restart-a", "restart-b"):
            for seq in range(1, 5):
                a.process(event(seq, session=session, actor_id=2))
        a.finish()
        self.assertEqual(a.alerts, [])

    def test_finished_sessions_can_be_evicted_without_false_coverage_warning(self):
        a = Analyzer(Config(max_sessions=2))
        for i in range(10):
            a.process(event(1, "session_start", session=f"s-{i}"))
            a.process(event(2, "session_end", session=f"s-{i}"))
        self.assertEqual(a.alerts, [])
        self.assertLessEqual(len(a.sessions), 2)

    def test_silent_collector_is_an_operational_alert_without_accused_player(self):
        a = Analyzer()
        a.process(event(1, "heartbeat", source="runtime", entity=""))
        a.report_silence()
        self.assertEqual(self.rules(a), ["collector_silent"])
        self.assertEqual(a.alerts[0]["reported_actors"], [])

    def test_distant_events_do_not_form_one_burst(self):
        a = Analyzer()
        for seq in range(1, 7):
            a.process(event(seq, x=seq * 1000))
        a.finish()
        self.assertEqual(a.alerts, [])

    def test_time_window_expires(self):
        a = Analyzer()
        for seq in range(1, 7):
            a.process(event(seq, t=70000 + seq * 11000))
        a.finish()
        self.assertEqual(a.alerts, [])

    def test_duplicates_are_ignored(self):
        a = Analyzer()
        row = event(1, "explosive_trigger")
        for _ in range(20):
            a.process(row)
        self.assertEqual(a.processed, 1)
        self.assertEqual(a.duplicates, 19)

    def test_loss_and_missing_sequences_are_visible(self):
        a = Analyzer()
        a.process(event(1, "session_start"))
        a.process(event(3, "telemetry_loss", count=900))
        self.assertEqual(self.rules(a), ["sequence_gap", "coverage_gap"])

    def test_allowed_prefab_suppresses_only_alerts(self):
        name = "{TEST}Prefabs/Structures/Example.et"
        a = Analyzer(Config(allowed_prefabs=[name]))
        for seq in range(1, 20):
            a.process(event(seq))
        a.finish()
        self.assertEqual(a.alerts, [])
        self.assertEqual(a.processed, 19)

    def test_watchlist_flags_single_object(self):
        name = "{TEST}Prefabs/Structures/Example.et"
        a = Analyzer(Config(watch_prefabs=[name]))
        a.process(event(1))
        a.finish()
        self.assertEqual(self.rules(a), ["watched_prefab"])

    def test_memory_bounds_report_eviction(self):
        a = Analyzer(Config(max_entities=10, max_windows=5))
        for seq in range(1, 100):
            a.process(event(seq, x=seq * 1000, t=seq * 1000 + 70000))
        a.finish()
        state = a.sessions["test-session"]
        self.assertLessEqual(len(state.entities), 10)
        self.assertLessEqual(len(state.windows), 5)
        self.assertIn("coverage_gap", self.rules(a))

    def test_unattributed_init_cannot_claim_player_from_hint(self):
        a = Analyzer()
        for seq in range(1, 7):
            a.process(event(seq, actor_id=77, attribution="server_action_callback"))
        a.finish()
        self.assertEqual(a.alerts[0]["reported_actors"], [])

    def test_bad_schema_and_nonfinite_values_rejected(self):
        for extra in ({"schema": 2}, {"x": float("nan")}, {"x": float("inf")},
                      {"seq": True}, {"t_ms": -1}, {"warmup": "false"}, {"kind": "ban_player"}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                raw = event(1)
                raw.update(extra)
                validate(raw)

    def test_json_handles_newline_and_unicode_without_log_injection(self):
        a = Analyzer(Config(spawn_threshold=1))
        a.process(event(1, "placement", source="editor_place", actor_id=1,
                        actor_name='Тест\n{"kind":"fake"}', attribution="server_action_callback"))
        a.finish()
        line = json.dumps(a.alerts[0], ensure_ascii=False)
        self.assertEqual(len(line.splitlines()), 1)
        self.assertEqual(json.loads(line)["reported_actors"][0]["name"], 'Тест\n{"kind":"fake"}')


class InputTests(unittest.TestCase):
    def test_follow_preserves_partial_line_and_reads_completed_line_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events-x-0.jsonl"
            data = json.dumps(event(1)).encode()
            path.write_bytes(data[:50])
            reader = TailReader()
            self.assertEqual(list(reader.read(path, follow=True)), [])
            with path.open("ab") as out:
                out.write(data[50:] + b"\n")
            self.assertEqual(len(list(reader.read(path, follow=True))), 1)
            self.assertEqual(list(reader.read(path, follow=True)), [])

    def test_file_truncation_resets_cursor(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events-x-0.jsonl"
            path.write_text(json.dumps(event(1)) + "\n", encoding="utf-8")
            reader = TailReader()
            self.assertEqual(len(list(reader.read(path))), 1)
            path.write_text("{}\n", encoding="utf-8")
            self.assertEqual(list(reader.read(path)), [{}])

    def test_numeric_rotation_order(self):
        with tempfile.TemporaryDirectory() as folder:
            for part in (10, 2, 0):
                (Path(folder) / f"events-session-{part}.jsonl").touch()
            self.assertEqual([p.stem for p in input_files(Path(folder))],
                             ["events-session-0", "events-session-2", "events-session-10"])

    def test_no_artificial_10000_record_gap(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "events-x-0.jsonl"
            path.write_bytes(b'{}\n' * 10020)
            self.assertEqual(sum(1 for _ in TailReader().read(path)), 10020)

    def test_invalid_configuration_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            for cfg in ({"typo": 2}, {"spawn_threshold": 0}, {"cell_meters": True},
                        {"allowed_prefabs": "all"}, {"spawn_threshold": 9000}):
                path.write_text(json.dumps(cfg), encoding="utf-8")
                with self.subTest(cfg=cfg), self.assertRaises(ValueError):
                    Config.load(path)

    def test_existing_input_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.jsonl"
            original = json.dumps(event(1)) + "\n"
            path.write_text(original, encoding="utf-8")
            self.assertEqual(main(["--input", str(path), "--output", str(path)]), 2)
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_oversized_line_fails_explicitly(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.jsonl"
            path.write_bytes(b"x" * (TailReader.MAX_LINE + 2))
            with self.assertRaises(ValueError):
                list(TailReader().read(path))


if __name__ == "__main__":
    unittest.main()
