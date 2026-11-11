from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib import error

from analyzer.checker import Analyzer
from examples.make_movement_demo import movement
from notifier.discord_bot import (DeliveryStopped, NoRedirect, Outbox, Reply, Webhook,
                                  Worker, main, payload_for, webhook_url)


def alert(identifier=None):
    analyzer = Analyzer()
    analyzer.process(movement())
    value = analyzer.alerts[0]
    if identifier:
        value["alert_id"] = identifier
    return value


def line(value):
    return json.dumps(value, ensure_ascii=False).encode() + b"\n"


class FakeTransport:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.payloads = []

    def post(self, payload):
        self.payloads.append(payload)
        result = self.replies.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


OK = Reply(200, {}, {"id": "123456789012345678"})


class FormattingTests(unittest.TestCase):
    def test_movement_message_has_requested_fields(self):
        payload = payload_for(alert(), "Server 1")
        text = payload["content"]
        for expected in ["Server 1", "[VEHICLE]", "DemoDriver", "synthetic-player-7",
                         "distance=702.195 m", "time_delta=1 s", "AVG_SPEED=702.195 m/s",
                         "INST_SPEED=145.189 m/s", "не доказ", "UTC:", "Alert:"]:
            self.assertIn(expected, text)
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_names_cannot_add_mentions_or_inject_new_lines(self):
        value = alert()
        value["reported_actors"][0]["name"] = "@everyone\n**ADMIN**\u202e<script>"
        payload = payload_for(value, "Test")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})
        self.assertNotIn("\u202e", payload["content"])
        self.assertNotIn("\n**ADMIN**", payload["content"])
        self.assertIn("\\@everyone", payload["content"])

    def test_long_unicode_payload_fits_discord_limit(self):
        value = alert("a" * 128)
        value["reported_actors"] = [dict(name="😀" * 1000, uid="🦊" * 1000)] * 32
        payload = payload_for(value, "🐱" * 1000)
        self.assertLessEqual(len(payload["content"].encode("utf-16-le")) // 2, 1900)

    def test_missing_actor_and_physics_are_not_fabricated(self):
        value = alert()
        value["reported_actors"] = []
        value["metrics"]["inst_speed_mps"] = None
        text = payload_for(value, "Test")["content"]
        self.assertIn("Гравця не встановлено", text)
        self.assertIn("INST_SPEED=н/д", text)

    def test_all_other_rules_can_be_rendered(self):
        from notifier.discord_bot import LABELS
        for rule in LABELS:
            value = alert()
            value["rule"] = rule
            payload = payload_for(value, "Test")
            self.assertIn(LABELS[rule], payload["content"])

    def test_invalid_movement_metric_is_rejected(self):
        value = alert()
        value["metrics"]["motion_mode"] = []
        with self.assertRaises(ValueError):
            payload_for(value, "Test")
        value = alert()
        value["metrics"]["distance_m"] = float("inf")
        with self.assertRaises(ValueError):
            payload_for(value, "Test")

    def test_webhook_requires_official_https_host_and_confirmed_delivery(self):
        url = "https://discord.com/api/webhooks/123456789/" + "T" * 24
        self.assertEqual(webhook_url(url), url + "?wait=true")
        self.assertIn("wait=true", webhook_url(url + "?wait=false&thread_id=123456"))
        for bad in [url.replace("https", "http"), url.replace("discord.com", "example.org"),
                    url.replace("discord.com", "discord.com.evil.test"), url + "#bad",
                    url + "?arbitrary=true", url.replace("discord.com", "user@discord.com"),
                    url.replace("discord.com", "discord.com:443")]:
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "^Invalid Discord webhook URL$"):
                webhook_url(bad)

    def test_redirects_are_not_followed(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.org"))

    def test_http_transport_uses_json_wait_and_bounded_read(self):
        url = "https://discord.com/api/webhooks/123456789/" + "T" * 24
        webhook = Webhook(url)
        response = io.BytesIO(b'{"id":"123456"}')
        response.code = 200
        response.headers = {"X-RateLimit-Remaining": "0"}
        with patch.object(webhook.opener, "open", return_value=response) as mocked:
            result = webhook.post(payload_for(alert(), "Test"))
        req = mocked.call_args.args[0]
        self.assertTrue(req.full_url.endswith("?wait=true"))
        self.assertEqual(json.loads(req.data)["allowed_mentions"], {"parse": []})
        self.assertEqual(result.headers["x-ratelimit-remaining"], "0")


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "alerts.jsonl"
        self.state = self.root / "outbox.sqlite3"
        self.path.write_bytes(line(alert()))
        self.box = Outbox(self.state)

    def tearDown(self):
        self.box.close()
        self.tmp.cleanup()

    def load(self):
        return self.box.ingest(self.path, "s1", "Server 1")

    def row(self):
        return self.box.db.execute("SELECT * FROM alerts WHERE server='s1'").fetchone()

    def test_sent_id_survives_restart_and_file_replay(self):
        self.assertEqual(self.load(), 1)
        transport = FakeTransport(OK)
        Worker(self.box, "s1", transport).once(100)
        self.assertEqual(self.box.counts("s1"), dict(sent=1, queued=0, failed=0))
        self.assertEqual(self.row()["payload"], "{}")
        self.box.close()
        self.box = Outbox(self.state)
        new_path = self.root / "replay.jsonl"
        new_path.write_bytes(self.path.read_bytes() * 2)
        self.assertEqual(self.box.ingest(new_path, "s1", "Server 1"), 0)
        self.assertFalse(Worker(self.box, "s1", transport).once(200))
        self.assertEqual(len(transport.payloads), 1)

    def test_partial_line_waits_for_newline_without_loss(self):
        data = line(alert())
        self.path.write_bytes(data[:-1])
        self.assertEqual(self.load(), 0)
        with self.path.open("ab") as stream:
            stream.write(b"\n")
        self.assertEqual(self.load(), 1)

    def test_duplicate_ids_within_file_deduplicated(self):
        self.path.write_bytes(line(alert()) * 3)
        self.assertEqual(self.load(), 1)

    def test_server_ids_namespace_deduplication(self):
        self.load()
        self.assertEqual(self.box.ingest(self.path, "s2", "Server 2"), 1)
        self.assertEqual(self.box.counts("s2")["queued"], 1)

    def test_replaced_and_truncated_file_reread(self):
        self.load()
        replacement = self.root / "replacement"
        replacement.write_bytes(line(alert("new-alert")))
        replacement.replace(self.path)
        self.assertEqual(self.load(), 1)
        self.path.write_bytes(line(alert("third")))
        self.assertEqual(self.load(), 1)

    def test_regrown_same_inode_detected_by_checkpoint(self):
        self.load()
        self.path.write_bytes(line(alert("changed-alert")) * 2)
        self.assertEqual(self.load(), 1)

    def test_queue_full_does_not_advance_past_unstored_alert(self):
        self.path.write_bytes(line(alert("one")) + line(alert("two")))
        self.assertEqual(self.box.ingest(self.path, "s1", "Test", max_pending=1), 1)
        Worker(self.box, "s1", FakeTransport(OK)).once(100)
        self.assertEqual(self.box.ingest(self.path, "s1", "Test", max_pending=1), 1)
        self.assertEqual(self.box.counts("s1"), dict(queued=1, sent=1, failed=0))

    def test_malformed_batch_rolls_back_cursor_and_queue(self):
        self.path.write_bytes(line(alert()) + b'{bad json}\n')
        with self.assertRaises(ValueError):
            self.load()
        self.assertEqual(self.box.counts("s1")["queued"], 0)
        self.assertEqual(self.box.db.execute("SELECT COUNT(*) FROM cursors").fetchone()[0], 0)
        self.path.write_bytes(line(alert()))
        self.assertEqual(self.load(), 1)

    def test_oversize_line_fails_without_advancing(self):
        self.path.write_bytes(b"x" * 65537)
        with self.assertRaises(ValueError):
            self.load()
        self.assertEqual(self.box.counts("s1")["queued"], 0)

    def test_lease_blocks_second_worker_and_recovers_after_crash(self):
        self.load()
        self.assertIsNotNone(self.box.claim("s1", 100))
        other = Outbox(self.state)
        try:
            self.assertIsNone(other.claim("s1", 102))
            self.assertIsNotNone(other.claim("s1", 161))
        finally:
            other.close()

    def test_429_wait_is_persisted_and_honored(self):
        self.load()
        transport = FakeTransport(Reply(429, {"retry-after": "5"}, {"retry_after": 7.5}), OK)
        worker = Worker(self.box, "s1", transport)
        self.assertTrue(worker.once(100))
        self.assertFalse(worker.once(107))
        self.assertTrue(worker.once(107.5))
        self.assertEqual(self.box.counts("s1")["sent"], 1)

    def test_successful_last_slot_honors_reset_header(self):
        self.path.write_bytes(line(alert("one")) + line(alert("two")))
        self.load()
        transport = FakeTransport(Reply(200, {"x-ratelimit-remaining": "0", "x-ratelimit-reset-after": "4"}, {"id": "123"}), OK)
        worker = Worker(self.box, "s1", transport)
        worker.once(100)
        self.assertFalse(worker.once(103))
        self.assertTrue(worker.once(104))

    def test_network_errors_do_not_expose_secret(self):
        self.load()
        transport = FakeTransport(error.URLError("secret-token-and-url"), OK)
        worker = Worker(self.box, "s1", transport)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            worker.once(100)
        self.assertNotIn("secret-token", stderr.getvalue())
        self.assertEqual(self.box.counts("s1")["queued"], 1)
        worker.once(102)
        self.assertEqual(self.box.counts("s1")["sent"], 1)

    def test_server_error_and_unconfirmed_success_are_retried(self):
        self.load()
        worker = Worker(self.box, "s1", FakeTransport(Reply(503, {}, {}), Reply(204, {}, {}), OK))
        with redirect_stderr(io.StringIO()):
            worker.once(100)
            worker.once(102)
        self.assertEqual(self.box.counts("s1")["sent"], 0)
        worker.once(106)
        self.assertEqual(self.box.counts("s1")["sent"], 1)

    def test_invalid_webhook_stops_and_keeps_pending_alert(self):
        self.load()
        worker = Worker(self.box, "s1", FakeTransport(Reply(404, {}, {})))
        with self.assertRaises(DeliveryStopped):
            worker.once(100)
        self.assertEqual(self.box.counts("s1")["queued"], 1)
        self.assertEqual(self.row()["error"], "http_404")

    def test_bad_payload_is_retained_as_failed(self):
        self.load()
        worker = Worker(self.box, "s1", FakeTransport(Reply(400, {}, {})))
        with redirect_stderr(io.StringIO()):
            worker.once(100)
        self.assertEqual(self.box.counts("s1")["failed"], 1)
        self.assertNotEqual(self.row()["payload"], "{}")

    def test_default_preview_has_no_network_or_state_side_effects(self):
        state = self.root / "unused.sqlite3"
        with patch.object(Webhook, "post", side_effect=AssertionError("network called")), redirect_stdout(io.StringIO()) as output:
            code = main(["--input", str(self.path), "--server-id", "s1", "--server-name", "Test", "--state", str(state)])
        self.assertEqual(code, 0)
        self.assertFalse(state.exists())
        self.assertIn("distance=702.195", output.getvalue())

    def test_live_mode_needs_configured_secret_but_does_not_print_it(self):
        with patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "SECRET-invalid-url"}), redirect_stderr(io.StringIO()) as output:
            code = main(["--input", str(self.path), "--server-id", "s1", "--server-name", "Test", "--send"])
        self.assertEqual(code, 2)
        self.assertNotIn("SECRET", output.getvalue())
