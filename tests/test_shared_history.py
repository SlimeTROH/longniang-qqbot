import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import shared_history as history


def row(*messages):return {'summary':'','sections':[],'recent':[{'role':'user','content':text} for text in messages]}


class HistoryTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.file=Path(self.temp.name)/'history.json'
    def tearDown(self):self.temp.cleanup()

    def test_stale_process_preserves_other_scopes_newer_private_and_group_data(self):
        initial={'private:a':row('initial'),'group:1:a':row('public')}
        history.atomic_json(self.file,initial)
        process_b=json.loads(json.dumps(initial))
        changed_a=json.loads(json.dumps(initial));changed_a['private:a']=row('initial','new private')
        history.merge_sessions(self.file,changed_a,initial)
        process_b['group:1:a']=row('public','new group')
        history.merge_sessions(self.file,process_b,initial)
        saved=history.read_sessions(self.file)
        self.assertEqual(saved['private:a'],changed_a['private:a'])
        self.assertEqual(saved['group:1:a'],process_b['group:1:a'])

    def test_deletion_is_scoped_and_retains_other_process_data(self):
        baseline={'private:a':row('secret'),'group:1:a':row('public')}
        latest={**baseline,'private:b':row('other')};history.atomic_json(self.file,latest)
        history.merge_sessions(self.file,{'group:1:a':baseline['group:1:a']},baseline)
        saved=history.read_sessions(self.file)
        self.assertNotIn('private:a',saved);self.assertIn('private:b',saved)

    def test_legacy_and_scoped_collision_preserves_both_in_private_only(self):
        history.atomic_json(self.file,{'a':row('legacy private'),'private:a':row('new private')})
        saved=history.read_sessions(self.file)
        self.assertEqual(list(saved),['private:a'])
        self.assertEqual([m['content'] for m in saved['private:a']['recent']],['legacy private','new private'])

    def test_scoped_read_respects_legacy_order_and_group_isolation(self):
        legacy = dict(row("shared", "legacy"), summary="old section")
        private = dict(row("shared", "new"), sections=["new section"])
        group = row("group only")
        for first, second, messages, sections in (
            (("a", legacy), ("private:a", private), ("shared", "legacy", "new"), ["old section", "new section"]),
            (("private:a", private), ("a", legacy), ("shared", "new", "legacy"), ["new section", "old section"]),
        ):
            with self.subTest(first=first[0]):
                history.atomic_json(self.file, dict([first, ("group:1:a", group), second]))
                expected = dict(row(*messages), sections=sections, summary="\n\n".join(sections))
                self.assertEqual(history.read_session(self.file, "private:a"), expected)
                self.assertEqual(history.read_session(self.file, "group:1:a"), group)
                self.assertIsNone(history.read_session(self.file, "private:missing"))

    def test_independent_single_scope_snapshots_preserve_both_writers_and_bystander(self):
        initial = {"private:a": row("private"), "group:1:a": row("group"), "private:b": row("other")}
        history.atomic_json(self.file, initial)
        private_base = {"private:a": history.read_session(self.file, "private:a")}
        group_base = {"group:1:a": history.read_session(self.file, "group:1:a")}
        private = row("private", "web reply")
        group = row("group", "QQ reply")
        history.merge_sessions(self.file, {"private:a": private}, private_base)
        history.merge_sessions(self.file, {"group:1:a": group}, group_base)
        self.assertEqual(history.read_sessions(self.file), {**initial, "private:a": private, "group:1:a": group})

    def test_unchanged_snapshot_does_not_replace_file_or_share_mutable_messages(self):
        initial = {"private:a": row("original")}
        history.atomic_json(self.file, initial)
        with patch.object(history, "atomic_json", wraps=history.atomic_json) as write:
            saved = history.merge_sessions(self.file, initial, initial)
            write.assert_not_called()
        initial["private:a"]["recent"][0]["content"] = "in memory only"
        self.assertEqual(saved["private:a"], row("original"))

    def test_first_writer_initializes_the_lock_marker_under_the_lock(self):
        path = self.file.with_suffix(".write.lock")
        with history._handle(path) as first, history._handle(path) as second:
            self.assertEqual(path.stat().st_size, 0)
            self.assertTrue(history._try_lock(first))
            try:
                self.assertEqual(path.stat().st_size, 1)
                self.assertFalse(history._try_lock(second))
            finally:
                history._unlock(first)
            self.assertTrue(history._try_lock(second))
            history._unlock(second)

    def test_concurrent_replace_retries_a_read_and_permanent_denial_is_reported(self):
        content = json.dumps({"private:a": row("reply")}).encode()
        with patch.object(Path, "read_bytes", side_effect=[PermissionError(), content]) as read:
            self.assertEqual(history.read_session(self.file, "private:a"), row("reply"))
            self.assertEqual(read.call_count, 2)
        with patch.object(Path, "read_bytes", side_effect=PermissionError()), patch.object(history.time, "sleep"):
            with self.assertRaises(PermissionError):
                history.read_session(self.file, "private:a")

    def test_malformed_disk_is_not_overwritten(self):
        self.file.write_text('broken JSON')
        with self.assertRaises(ValueError):history.merge_sessions(self.file,{'private:a':row('new')},{})
        self.assertEqual(self.file.read_text(),'broken JSON')

    def test_failed_replace_keeps_original_history(self):
        history.atomic_json(self.file,{'private:a':row('original')})
        with patch.object(history.os,'replace',side_effect=OSError('simulated')):
            with self.assertRaises(OSError):history.merge_sessions(self.file,{'private:a':row('changed')},{})
        self.assertEqual(history.read_sessions(self.file)['private:a'],row('original'))


class HistoryLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_scope_serializes_and_different_scope_is_not_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'history.json';events=[]
            entered=asyncio.Event();release=asyncio.Event()
            async def first():
                async with history.scope_lock(file,'private:a'):
                    events.append('first');entered.set();await release.wait()
            async def second():
                async with history.scope_lock(file,'private:a'):events.append('second')
            a=asyncio.create_task(first());await entered.wait();b=asyncio.create_task(second())
            async with history.scope_lock(file,'group:1:a'):events.append('group')
            await asyncio.sleep(.01);self.assertEqual(events,['first','group'])
            release.set();await asyncio.gather(a,b);self.assertEqual(events[-1],'second')

    async def test_cancelled_scope_waiter_does_not_leave_lock_held(self):
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/'history.json'
            async def waiter():
                async with history.scope_lock(file,'private:a'):pass
            async with history.scope_lock(file,'private:a'):
                task=asyncio.create_task(waiter());await asyncio.sleep(.01);task.cancel()
                await asyncio.gather(task,return_exceptions=True)
            async with history.scope_lock(file,'private:a'):pass


if __name__=='__main__':unittest.main()
