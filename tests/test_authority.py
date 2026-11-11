import json
from pathlib import Path
import tempfile
import unittest

from analyzer.checker import Analyzer, Config, validate
from examples.make_authority_demo import generate
from notifier.discord_bot import Outbox, payload_for
from test_spawn_alerts import spawn


def gm_fields(*, status="gm_authorized", basis="gm_editor", actor=4, role="ADMIN"):
    return dict(authorization=status, authorization_source=basis, authorization_id="test-gm-action",
                authorization_entity="gm-generator", authorized_actor_id=actor,
                authorized_actor_uid=f"synthetic-{actor}", authorized_actor_name="DemoAdmin",
                authorized_actor_role=role)


def gm_placement(seq, *, t=None, **extra):
    values = dict(source="editor_place", actor_id=4, actor_uid="synthetic-4", actor_name="DemoAdmin",
                  actor_role="ADMIN", attribution="server_action_callback", **gm_fields())
    values.update(extra)
    row = spawn(seq, kind="placement", t=t, **values)
    row["authorization_entity"] = row["entity"]
    return row


class AuthorityTests(unittest.TestCase):
    def test_role_alone_does_not_authorize_spawn(self):
        a = Analyzer()
        for seq in range(1, 13):
            a.process(spawn(seq, kind="placement", source="editor_place", actor_id=4,
                            actor_uid="synthetic-4", actor_role="ADMIN", attribution="server_action_callback"))
        a.finish()
        result = a.alerts[0]
        self.assertEqual(result["authorization"]["status"], "unverified")
        self.assertEqual(result["severity"], "medium")
        self.assertIn("role=ADMIN", payload_for(result, "Test")["content"])
        self.assertIn("Підозрілий спавн", payload_for(result, "Test")["content"])

    def test_valid_gm_placement_is_info_and_names_action_creator(self):
        a = Analyzer()
        for seq in range(1, 13):
            a.process(gm_placement(seq))
        a.finish()
        result = a.alerts[0]
        self.assertEqual(result["authorization"]["status"], "gm_authorized")
        self.assertEqual(result["severity"], "info")
        text = payload_for(result, "Test")["content"]
        self.assertIn("Підтверджена дія Game Master", text)
        self.assertIn("DemoAdmin", text)
        self.assertIn("authorization=GM_AUTHORIZED", text)
        self.assertNotIn("Підозрілий спавн", text)

    def test_delegated_game_master_need_not_be_server_admin(self):
        a = Analyzer(Config(spawn_threshold=1))
        a.process(gm_placement(1, actor_role="GAME_MASTER", authorized_actor_role="GAME_MASTER"))
        a.finish()
        self.assertEqual(a.alerts[0]["authorization"]["status"], "gm_authorized")

    def test_init_inherits_only_matching_verified_placement(self):
        a = Analyzer(Config(spawn_threshold=1))
        a.process(spawn(1, entity="created"))
        a.process(gm_placement(2, t=70001, entity="created"))
        a.finish()
        self.assertEqual(len(a.alerts), 1)
        self.assertEqual(a.alerts[0]["authorization"]["status"], "gm_authorized")

    def test_admin_activity_cannot_suppress_unknown_player_in_same_cell(self):
        a = Analyzer()
        for seq in range(1, 13):
            a.process(gm_placement(seq))
        for seq in range(13, 19):
            a.process(spawn(seq))
        a.finish()
        self.assertEqual([x["authorization"]["status"] for x in a.alerts], ["gm_authorized", "unverified"])
        self.assertEqual(a.alerts[1]["rule"], "unattributed_spawn_burst")
        self.assertEqual(a.alerts[1]["reported_actors"], [])

    def test_authorized_count_does_not_push_player_over_threshold(self):
        a = Analyzer()
        for seq in range(1, 7):
            a.process(gm_placement(seq))
        for seq in range(7, 13):
            a.process(spawn(seq, kind="placement", source="conflict_request", actor_id=9,
                            actor_uid="player-9", attribution="server_action_callback"))
        a.finish()
        self.assertEqual(a.alerts, [])

    def test_raw_admin_hint_without_route_is_not_trusted(self):
        a = Analyzer()
        for seq in range(1, 7):
            a.process(spawn(seq, actor_id=4, actor_role="ADMIN", is_admin=True))
        a.finish()
        self.assertEqual(a.alerts[0]["authorization"]["status"], "unverified")
        self.assertEqual(a.alerts[0]["reported_actors"], [])

    def test_gm_effect_link_preserves_original_identity_without_current_slot_lookup(self):
        a = Analyzer()
        for seq in range(1, 9):
            a.process(spawn(seq, kind="explosive_damage", amount=20, source="damage_manager",
                            source_entity="exact-projectile", actor_id=0,
                            **gm_fields(basis="gm_effect_link")))
        result = a.alerts[0]
        self.assertEqual(result["reported_actors"], [])
        self.assertEqual(result["authorization"]["sources"][0]["uid"], "synthetic-4")
        self.assertEqual(result["severity"], "info")
        self.assertIn("DemoAdmin", payload_for(result, "Test")["content"])

    def test_gm_object_origin_does_not_authorize_future_explosions(self):
        a = Analyzer()
        for seq in range(1, 9):
            a.process(spawn(seq, kind="explosive_damage", amount=20, source="damage_manager",
                            source_entity="previously-spawned-vehicle", **gm_fields(status="gm_origin")))
        result = a.alerts[0]
        self.assertEqual(result["authorization"]["status"], "gm_origin")
        self.assertEqual(result["severity"], "medium")
        self.assertIn("дозвіл на цей ефект не підтверджено", payload_for(result, "Test")["content"])

    def test_no_time_or_radius_exemption_for_unrelated_explosions(self):
        a = Analyzer()
        a.process(gm_placement(1, entity="legitimate-fx"))
        for seq in range(2, 10):
            a.process(spawn(seq, kind="explosive_damage", amount=20, source="damage_manager", source_entity="unrelated-projectile"))
        self.assertEqual(a.alerts[0]["authorization"]["status"], "unverified")

    def test_incomplete_or_invalid_permission_proof_rejected(self):
        base = gm_placement(1)
        for changes in [dict(authorization_id=""), dict(authorized_actor_id=8), dict(authorized_actor_uid="another-uid"),
                        dict(authorized_actor_role="PLAYER"), dict(authorization_source="client_claim"),
                        dict(source="conflict_request"), dict(kind="entity_init"), dict(authorization_entity="another-object")]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate({**base, **changes})
        with self.assertRaises(ValueError):
            validate(spawn(1, kind="explosive_damage", amount=20, **gm_fields()))

    def test_silence_alert_does_not_inherit_admin_authorization(self):
        a = Analyzer()
        a.process(gm_placement(1))
        a.report_silence()
        self.assertNotIn("authorization", a.alerts[-1])
        self.assertEqual(a.alerts[-1]["reported_actors"], [])

    def test_synthetic_authority_pipeline_and_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "events.jsonl"
            self.assertEqual(generate(raw), 44)
            a = Analyzer()
            for row in raw.read_text().splitlines():
                a.process(json.loads(row))
            a.finish()
            self.assertEqual([v["authorization"]["status"] for v in a.alerts],
                             ["gm_authorized", "unverified", "gm_authorized", "gm_origin", "unverified"])
            self.assertEqual([v["severity"] for v in a.alerts], ["info", "medium", "info", "medium", "medium"])
            path = Path(tmp) / "alerts.jsonl"
            path.write_text("".join(json.dumps(v) + "\n" for v in a.alerts))
            box = Outbox(Path(tmp) / "queue.sqlite3")
            try:
                self.assertEqual(box.ingest(path, "demo", "Test"), 5)
                self.assertEqual(box.ingest(path, "demo", "Test"), 0)
            finally:
                box.close()
