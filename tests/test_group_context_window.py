from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest

scope = runpy.run_path(str(Path(__file__).resolve().parents[1] / "plugins/chat_bot/conversation_scope.py"))
group_context = scope["group_context"]
group_history_tail = scope["group_history_tail"]
HEADER = "【同一用户的群聊上下文】"
ROLE_SEPARATOR = "："


def reference_group_context(histories, user_id, render):
    """Straightforward reference implementation for exact output comparison."""
    suffix = f":{user_id}"
    matching_values = [
        value
        for key, value in histories.items()
        if isinstance(key, str) and key.startswith("group:") and key.endswith(suffix)
    ]
    blocks = []
    for value in matching_values[-4:]:
        msgs = list(render(value))[-8:]
        lines = [
            str(msg.get("role", "user")) + ROLE_SEPARATOR + str(msg.get("content", ""))
            for msg in msgs
        ]
        body = "\n".join(lines)
        if body:
            blocks.append(HEADER + "\n" + body[-1800:])
    return "\n\n".join(blocks)[-5000:]


class _TrackingDict(dict):
    """Insertion-ordered dict that records reverse iteration and key lookups."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.yielded_in_reverse = []
        self.accessed_keys = []

    def __reversed__(self):
        for key in super().__reversed__():
            self.yielded_in_reverse.append(key)
            yield key

    def __getitem__(self, key):
        self.accessed_keys.append(key)
        return super().__getitem__(key)


class GroupContextWindowTests(unittest.TestCase):
    def test_excess_groups_selects_final_four_in_insertion_order(self):
        histories = {
            f"group:room-{idx}:acct-1": [{"role": "member", "content": f"msg-{idx}"}]
            for idx in range(1, 7)
        }
        rendered_payloads = []

        def tracking_render(value):
            rendered_payloads.append(value[0]["content"])
            return value

        actual = group_context(histories, "acct-1", tracking_render)
        expected = reference_group_context(histories, "acct-1", list)

        self.assertEqual(actual, expected)
        self.assertEqual(rendered_payloads, ["msg-3", "msg-4", "msg-5", "msg-6"])

    def test_nonmatching_users_and_nongroups_ignored(self):
        histories = {
            "dm:chan-a:acct-7": [{"role": "user", "content": "dm-skip"}],
            "group:chan-b:acct-70": [{"role": "user", "content": "prefix-trap"}],
            "group:chan-c:acct-7:extra": [{"role": "user", "content": "suffix-trap"}],
            999: [{"role": "user", "content": "non-str-key"}],
            "group:chan-d:acct-7": [{"role": "host", "content": "first-match"}],
            "thread:chan-e:acct-7": [{"role": "user", "content": "nongroup-skip"}],
            "group:chan-f:acct-7": [{"content": "missing-role-defaults"}],
            "group:chan-g:acct-8": [{"role": "user", "content": "other-user"}],
        }
        render_calls = []

        def tracking_render(value):
            render_calls.append(value)
            return value

        actual = group_context(histories, "acct-7", tracking_render)
        expected = reference_group_context(histories, "acct-7", list)

        self.assertEqual(actual, expected)
        self.assertEqual(len(render_calls), 2)
        self.assertIn("【同一用户的群聊上下文】\nhost：first-match", actual)
        self.assertIn("【同一用户的群聊上下文】\nuser：missing-role-defaults", actual)

    def test_reverse_iteration_stops_after_fourth_matching_key(self):
        histories = _TrackingDict(
            [
                ("group:g0:42", [{"role": "a", "content": "never-visited-0"}]),
                ("dm:ignored:42", [{"role": "a", "content": "never-visited-dm"}]),
                ("group:g1:42", [{"role": "a", "content": "never-visited-1"}]),
                ("group:g2:42", [{"role": "a", "content": "hit-2"}]),
                ("group:g3:42", [{"role": "a", "content": "hit-3"}]),
                ("group:g4:99", [{"role": "a", "content": "other-user"}]),
                ("group:g5:42", [{"role": "a", "content": "hit-5"}]),
                ("notes:tail", [{"role": "a", "content": "tail-nongroup"}]),
                ("group:g6:42", [{"role": "a", "content": "hit-6"}]),
            ]
        )
        render_order = []

        def tracking_render(value):
            render_order.append(value[0]["content"])
            return value

        actual = group_context(histories, 42, tracking_render)
        expected = reference_group_context(histories, 42, list)

        self.assertEqual(actual, expected)
        self.assertEqual(
            histories.yielded_in_reverse,
            [
                "group:g6:42",
                "notes:tail",
                "group:g5:42",
                "group:g4:99",
                "group:g3:42",
                "group:g2:42",
            ],
        )
        self.assertEqual(
            histories.accessed_keys,
            ["group:g6:42", "group:g5:42", "group:g3:42", "group:g2:42"],
        )
        self.assertEqual(render_order, ["hit-2", "hit-3", "hit-5", "hit-6"])

    def test_empty_groups_counted_in_selection_and_omitted_from_output(self):
        histories = {
            "group:old:u1": [{"role": "user", "content": "must-not-be-selected"}],
            "group:one:u1": [{"role": "user", "content": "kept-one"}],
            "group:empty-mid:u1": [],
            "group:two:u1": [{"role": "user", "content": "kept-two"}],
            "group:empty-end:u1": [],
        }
        rendered_count = 0

        def counting_render(value):
            nonlocal rendered_count
            rendered_count += 1
            return value

        actual = group_context(histories, "u1", counting_render)
        expected = reference_group_context(histories, "u1", list)

        self.assertEqual(actual, expected)
        self.assertEqual(rendered_count, 4)
        self.assertEqual(actual, "【同一用户的群聊上下文】\nuser：kept-one\n\n【同一用户的群聊上下文】\nuser：kept-two")

    def test_custom_render_and_last_eight_messages_limit(self):
        histories = {
            "group:custom:u9": SimpleNamespace(
                entries=[(f"speaker-{i}", f"line-{i}") for i in range(12)]
            )
        }

        def custom_render(bundle):
            return (
                {"role": speaker, "content": text}
                for speaker, text in bundle.entries
            )

        actual = group_context(histories, "u9", custom_render)
        expected = reference_group_context(histories, "u9", custom_render)

        self.assertEqual(actual, expected)
        self.assertNotIn("line-3", actual)
        self.assertIn("speaker-4：line-4", actual)
        self.assertIn("speaker-11：line-11", actual)

    def test_very_long_unicode_content_and_roles_against_reference(self):
        long_role = ("語" * 2500) + "役"
        long_content = ("é" * 1000) + ("🛰" * 1500) + "終"
        histories = {
            "group:r1:u5": [
                {"role": "bot", "content": "α" * 3000},
                {"role": long_role, "content": "tail-short"},
            ],
            "group:r2:u5": [
                {"role": "a", "content": "b"},
                {"role": "c", "content": long_content},
            ],
            "group:r3:u5": [
                {"role": long_role, "content": ""},
            ],
            "group:r4:u5": [
                {"role": "mod", "content": ("λ" * 1795) + "END"},
            ],
        }

        actual = group_context(histories, "u5", list)
        expected = reference_group_context(histories, "u5", list)

        self.assertEqual(actual, expected)
        self.assertEqual(len(actual), 5000)

    def test_very_long_unicode_strings_without_giant_reference(self):
        huge_role = ("Δ" * 250_000) + "R"
        huge_content = ("🚀" * 250_000) + "Z"
        histories = {
            "group:huge-role:u6": [{"role": huge_role, "content": "ok"}],
            "group:huge-content:u6": [{"role": "sys", "content": huge_content}],
        }

        actual = group_context(histories, "u6", list)
        expected_first = f"{HEADER}\n" + ("Δ" * 1796) + "R：ok"
        expected_second = f"{HEADER}\n" + ("🚀" * 1799) + "Z"
        self.assertEqual(actual, f"{expected_first}\n\n{expected_second}")

    def test_group_history_tail_skips_render_all_at_or_above_eight(self):
        recent = [f"m{i}" for i in range(10)]
        session = SimpleNamespace(recent=recent, archive=["a0", "a1"])

        def fail_render_all(_session):
            raise AssertionError("render_all must not be called when recent >= 8")

        self.assertEqual(group_history_tail(session, fail_render_all), recent[-8:])

        exact_eight = [f"e{i}" for i in range(8)]
        exact_session = SimpleNamespace(recent=exact_eight, archive=["a0"])
        self.assertEqual(group_history_tail(exact_session, fail_render_all), exact_eight)

    def test_group_history_tail_short_recent_uses_render_all_with_archive(self):
        session = SimpleNamespace(
            archive=["arch-1", "arch-2", "arch-3", "arch-4", "arch-5"],
            recent=["rec-1", "rec-2", "rec-3", "rec-4"],
        )
        calls = []

        def render_all(sess):
            calls.append(sess)
            return [f"rendered:{item}" for item in (sess.archive + sess.recent)]

        result = group_history_tail(session, render_all)

        self.assertEqual(calls, [session])
        self.assertEqual(
            result,
            [
                "rendered:arch-2",
                "rendered:arch-3",
                "rendered:arch-4",
                "rendered:arch-5",
                "rendered:rec-1",
                "rendered:rec-2",
                "rendered:rec-3",
                "rendered:rec-4",
            ],
        )


if __name__ == "__main__":
    unittest.main()
