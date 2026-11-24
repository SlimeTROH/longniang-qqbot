from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import web_conversations
from shared_history import atomic_json as _disk_atomic_json
from web_conversations import Conversations


class ConversationCommitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.store_path = Path(self._tmpdir.name) / "conversations.json"

    def test_versionless_collection_and_unknown_metadata_survive_managed_writes(self):
        value = {"custom": {"keep": 7}, "conversations": {"legacy": {"future_field": "keep"}}}
        self.store_path.write_text(json.dumps(value), encoding="utf-8")
        store = Conversations(self.store_path, {}, "unused", 0)
        root, rows = store.data, store.data["conversations"]
        store.create(title="  display name  ")
        store.configure("legacy", {"title": 123, "pre_prompt": 42, "aff": "15"})
        saved = json.loads(self.store_path.read_bytes())
        self.assertIs(store.data, root)
        self.assertIs(store.data["conversations"], rows)
        self.assertEqual(saved["custom"], value["custom"])
        self.assertNotIn("version", saved)
        self.assertEqual(saved["conversations"]["legacy"]["future_field"], "keep")
        self.assertEqual(store.get()["title"], "123")
        self.assertEqual(store.get()["pre_prompt"], "42")
        self.assertEqual(store.listing()[-1]["title"], "  display name  ")

    def test_invalid_existing_collection_is_left_intact(self):
        for blob in ('{"conversations":[]}', '{"conversations":{}}', 'broken'):
            with self.subTest(blob=blob):
                self.store_path.write_text(blob, encoding="utf-8")
                with self.assertRaises(ValueError):
                    Conversations(self.store_path, {}, "unused", 0)
                self.assertEqual(self.store_path.read_text(encoding="utf-8"), blob)

    def test_configure_preserves_identity_nested_lists_and_shallow_copies(self) -> None:
        captured_payloads: list[dict] = []

        def recording_writer(path: Path | str, payload: dict) -> None:
            captured_payloads.append(payload)
            _disk_atomic_json(path, payload)

        with mock.patch.object(
            web_conversations, "atomic_json", side_effect=recording_writer
        ):
            convs = Conversations(self.store_path, {"custom": "keep"}, "uid", 0.0)
            row = convs.create(title="First", aff=10.0, prompt="sys")
            legacy_row = convs.get("legacy")
            hist = row["history"]
            secs = row["sections"]
            row["stage_sent"] = 4
            row["unknown_field"] = {"nested": 1}

            updated = convs.configure(
                row["id"],
                {
                    "title": "Updated",
                    "pre_prompt": "new-sys",
                    "aff": 250.0,
                    "temp": 2.4,
                    "ignored_key": "should-not-appear",
                },
            )

        self.assertIs(updated, row)
        self.assertIs(convs.get(row["id"]), row)
        self.assertIs(row["history"], hist)
        self.assertIs(row["sections"], secs)
        self.assertEqual(row["title"], "Updated")
        self.assertEqual(row["pre_prompt"], "new-sys")
        self.assertEqual(row["aff"], 100.0)
        self.assertEqual(row["temp"], 1.5)
        self.assertEqual(row["stage_sent"], 0)
        self.assertEqual(row["unknown_field"], {"nested": 1})
        self.assertNotIn("ignored_key", row)

        configure_payload = captured_payloads[-1]
        self.assertIsNot(configure_payload["conversations"], convs.data["conversations"])
        self.assertIsNot(configure_payload["conversations"][row["id"]], row)
        self.assertIs(configure_payload["conversations"]["legacy"], legacy_row)
        self.assertIs(configure_payload["conversations"][row["id"]]["history"], hist)

    def test_create_failure_leaves_state_unchanged_and_retryable(self) -> None:
        convs = Conversations(self.store_path, {}, "uid", 0.0)
        before_disk = self.store_path.read_text(encoding="utf-8")

        with mock.patch.object(
            web_conversations, "atomic_json", side_effect=OSError("disk full")
        ):
            with self.assertRaises(OSError):
                convs.create(title="ShouldFail")

        self.assertEqual([item["id"] for item in convs.listing()], ["legacy"])
        self.assertEqual(self.store_path.read_text(encoding="utf-8"), before_disk)

        created = convs.create(title="Retried")

        self.assertEqual(
            [item["id"] for item in convs.listing()],
            ["legacy", created["id"]],
        )
        reopened = Conversations(self.store_path, {}, "uid", 0.0)
        self.assertEqual(reopened.get(created["id"])["title"], "Retried")

    def test_configure_failure_leaves_state_and_identity_unchanged_and_retryable(self) -> None:
        convs = Conversations(self.store_path, {}, "uid", 0.0)
        row = convs.create(title="Before", aff=5.0)
        row["stage_sent"] = 7
        convs.save()
        before_disk = self.store_path.read_text(encoding="utf-8")
        hist = row["history"]

        with mock.patch.object(
            web_conversations, "atomic_json", side_effect=RuntimeError("write error")
        ):
            with self.assertRaises(RuntimeError):
                convs.configure(row["id"], {"title": "FailedEdit", "aff": 42.0, "temp": 0.2})

        self.assertIs(convs.get(row["id"]), row)
        self.assertIs(row["history"], hist)
        self.assertEqual(row["title"], "Before")
        self.assertEqual(row["aff"], 5.0)
        self.assertEqual(row["temp"], 0.9)
        self.assertEqual(row["stage_sent"], 7)
        self.assertEqual(self.store_path.read_text(encoding="utf-8"), before_disk)

        convs.configure(row["id"], {"title": "After", "aff": 42.0, "temp": 0.2})

        self.assertIs(convs.get(row["id"]), row)
        self.assertEqual(row["title"], "After")
        self.assertEqual(row["aff"], 42.0)
        self.assertEqual(row["temp"], 0.2)
        self.assertEqual(row["stage_sent"], 0)
        reopened = Conversations(self.store_path, {}, "uid", 0.0)
        self.assertEqual(reopened.get(row["id"])["title"], "After")

    def test_reject_bad_numbers_including_nan_without_partial_edits(self) -> None:
        convs = Conversations(self.store_path, {}, "uid", 0.0)
        row = convs.create(title="Stable", aff=12.0)
        row["stage_sent"] = 3
        convs.save()

        bad_numbers = [
            float("nan"),
            float("inf"),
            float("-inf"),
            "not-a-number",
            10**1000,
            None,
        ]
        with mock.patch.object(
            web_conversations, "atomic_json", side_effect=_disk_atomic_json
        ) as writer:
            for bad in bad_numbers:
                with self.assertRaises(ValueError):
                    convs.create(title="Nope", aff=bad)  # type: ignore[arg-type]
                with self.assertRaises(ValueError):
                    convs.configure(row["id"], {"title": "Partial", "aff": bad})
                with self.assertRaises(ValueError):
                    convs.configure(row["id"], {"title": "Partial", "temp": bad})

        writer.assert_not_called()
        self.assertEqual(row["title"], "Stable")
        self.assertEqual(row["aff"], 12.0)
        self.assertEqual(row["temp"], 0.9)
        self.assertEqual(row["stage_sent"], 3)
        self.assertEqual(len(convs.listing()), 2)

    def test_two_threaded_creates_both_survive_reopening(self) -> None:
        convs = Conversations(self.store_path, {}, "uid", 0.0)
        barrier = threading.Barrier(2)
        created_ids: list[str] = []
        errors: list[BaseException] = []
        collect_lock = threading.Lock()

        def worker(idx: int) -> None:
            try:
                barrier.wait(timeout=5.0)
                row = convs.create(title=f"Thread-{idx}", aff=float(idx))
                with collect_lock:
                    created_ids.append(row["id"])
            except BaseException as exc:  # pragma: no cover - surfaced below
                with collect_lock:
                    errors.append(exc)

        t1 = threading.Thread(target=worker, args=(1,))
        t2 = threading.Thread(target=worker, args=(2,))
        t1.start()
        t2.start()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        self.assertEqual(errors, [])
        self.assertEqual(len(created_ids), 2)
        self.assertEqual(len(set(created_ids)), 2)

        reopened = Conversations(self.store_path, {}, "uid", 0.0)
        reopened_ids = {item["id"] for item in reopened.listing()}
        self.assertIn("legacy", reopened_ids)
        for cid in created_ids:
            self.assertIn(cid, reopened_ids)
            self.assertTrue(reopened.get(cid)["title"].startswith("Thread-"))
