import json
from pathlib import Path
import tempfile
import unittest

from analyzer.checker import Analyzer, Config, validate
from examples.make_movement_demo import movement


class MovementTests(unittest.TestCase):
    def test_displacement_like_example_has_all_measurements(self):
        a = Analyzer()
        a.process(movement())
        self.assertEqual(len(a.alerts), 1)
        alert = a.alerts[0]
        self.assertEqual(alert["rule"], "movement_anomaly")
        self.assertEqual(alert["metrics"]["distance_m"], 702.195)
        self.assertEqual(alert["metrics"]["time_delta_s"], 1)
        self.assertEqual(alert["metrics"]["avg_speed_mps"], 702.195)
        self.assertEqual(alert["metrics"]["inst_speed_mps"], 145.189)
        self.assertEqual(alert["reported_actors"][0]["uid"], "synthetic-player-7")
        self.assertEqual(alert["verdict"], "needs_human_review")

    def test_distance_and_speed_must_both_cross_threshold(self):
        for args in [dict(distance=120), dict(distance=300, delta=2000),
                     dict(distance=70, mode="FOOT"), dict(distance=80, delta=2500, mode="FOOT")]:
            with self.subTest(args=args):
                a = Analyzer()
                a.process(movement(**args))
                self.assertEqual(a.alerts, [])

    def test_foot_rule_uses_own_threshold(self):
        a = Analyzer()
        a.process(movement(distance=90, mode="FOOT", inst_speed_valid=False))
        self.assertEqual(len(a.alerts), 1)
        self.assertIsNone(a.alerts[0]["metrics"]["inst_speed_mps"])

    def test_long_gap_short_interval_and_warmup_suppressed(self):
        for args in [dict(delta=5000), dict(delta=200), dict(warmup=True)]:
            with self.subTest(args=args):
                a = Analyzer()
                a.process(movement(**args))
                self.assertEqual(a.alerts, [])

    def test_cooldown_is_player_entity_and_session_scoped(self):
        a = Analyzer()
        a.process(movement())
        a.process(movement(2, t=71000))
        self.assertEqual(len(a.alerts), 1)
        a.process(movement(3, t=72000, actor_id=8, actor_uid="synthetic-player-8"))
        a.process(movement(4, t=73000, entity="other-car"))
        a.process(movement(5, t=100000))
        a.process(movement(1, session="another-session"))
        self.assertEqual(len(a.alerts), 5)

    def test_replay_keeps_stable_alert_id(self):
        a, b = Analyzer(), Analyzer()
        a.process(movement())
        a.process(movement())
        b.process(movement())
        self.assertEqual(len(a.alerts), 1)
        self.assertEqual(a.alerts[0]["alert_id"], b.alerts[0]["alert_id"])

    def test_inconsistent_or_nonfinite_data_rejected(self):
        for changes in [dict(distance_m=999), dict(avg_speed_mps=1), dict(inst_speed_mps=-1),
                        dict(x=float("nan")), dict(from_x=float("inf")), dict(delta_ms=True),
                        dict(delta_ms=0), dict(delta_ms=80000), dict(motion_mode="PASSENGER"),
                        dict(attribution="client_claim"), dict(actor_id=0), dict(entity=""),
                        dict(inst_speed_valid="yes")]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate(movement(**changes))

    def test_rounding_from_engine_serialization_tolerated(self):
        a = Analyzer()
        a.process(movement(distance_m=702.2, avg_speed_mps=702.2))
        self.assertEqual(len(a.alerts), 1)

    def test_instant_speed_does_not_override_displacement_evidence(self):
        a = Analyzer()
        a.process(movement(inst_speed_mps=0.161603))
        self.assertEqual(len(a.alerts), 1)

    def test_movement_reset_is_audit_only(self):
        a = Analyzer()
        a.process(dict(schema=1, session="reset-test", seq=1, utc=100, t_ms=70000,
                       kind="movement_reset", source="authorized_server_adapter"))
        self.assertEqual(a.alerts, [])

    def test_telemetry_outage_does_not_accuse_last_driver(self):
        a = Analyzer()
        a.process(movement())
        a.report_silence()
        self.assertEqual(a.alerts[-1]["rule"], "collector_silent")
        self.assertEqual(a.alerts[-1]["reported_actors"], [])

    def test_config_rejects_reversed_sample_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps(dict(movement_min_delta_ms=3000, movement_max_delta_ms=1000)))
            with self.assertRaises(ValueError):
                Config.load(path)
