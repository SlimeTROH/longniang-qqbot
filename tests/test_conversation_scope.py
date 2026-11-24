import ast
import asyncio
from dataclasses import dataclass,field
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent
from nonebot.params import CommandStart
from nonebot.rule import Rule, TRIE_VALUE, TrieRule

STAGE=Path(__file__).parent/'stage/plugins/chat_bot'
if not STAGE.is_dir():
    STAGE=Path(__file__).resolve().parents[1]/'plugins/chat_bot'
def load(name,file):
    spec=importlib.util.spec_from_file_location(name,STAGE/file)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return module
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import shared_history
scope=load('scope_test','conversation_scope.py')
memory=load('memory_scope_test','memory.py')
affection=load('affection_scope_test','affection.py')

class VisibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.private=types.SimpleNamespace(user_id=7)
        self.group=types.SimpleNamespace(user_id=7,group_id=8)

    def tearDown(self): self.tmp.cleanup()

    def test_members_share_their_group_session_only(self):
        events=[self.private,self.group,types.SimpleNamespace(user_id=7,group_id=9),types.SimpleNamespace(user_id=9,group_id=8)]
        self.assertEqual([scope.session_key(e) for e in events], ["private:7", "group:8", "group:9", "group:8"])

    def test_daily_affection_draws_from_zero_to_point_four(self):
        store = affection.AffectionStore(self.root / "affection.json")
        for value in (0.0, 0.4):
            rng = Mock()
            rng.uniform.return_value = value
            award = shared_history.apply_affection(store, str(value), "none", now=100, rng=rng)
            rng.uniform.assert_called_once_with(0.0, 0.4)
            self.assertEqual(award.delta, value)

    def test_offense_skips_the_daily_draw(self):
        store = affection.AffectionStore(self.root / "affection.json")
        rng = Mock()
        award = shared_history.apply_affection(store, "7", "offense", now=100, rng=rng)
        rng.uniform.assert_not_called()
        self.assertEqual(award.daily_delta, 0.0)
        self.assertEqual(award.delta, -3.0)

    def history_env(self):
        tree=ast.parse((STAGE/'__init__.py').read_text(encoding='utf-8'))
        names = {'Session', '_load_histories', '_save_histories', '_refresh_history_scope',
                 '_history_messages', '_history_cut', '_maybe_compress_history',
                 '_save_problem_solution', '_deliver_problem'}
        nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        path=self.root/'history.json'
        env={'dataclass':dataclass,'field':field,'histories':{},'HISTORY_FILE':path,'_HISTORY_BASELINE':{},'_read_shared_histories':shared_history.read_sessions,'_read_shared_history':shared_history.read_session,'_merge_shared_histories':shared_history.merge_sessions,'logger':Mock()}
        env.update(MessageEvent=MessageEvent, Bot=object, problem_solver=types.SimpleNamespace(Delivery=types.SimpleNamespace))
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'history-scope-test','exec'),env)
        return env, path

    def test_legacy_mixed_history_and_summaries_restore_as_private_only(self):
        env, path = self.history_env()
        path.write_text(json.dumps({'7':{'summary':'PRIVATE_SUMMARY','recent':[{'role':'user','content':'PRIVATE_SECRET'}]}}))
        env['_load_histories']()
        self.assertIn('private:7',env['histories'])
        self.assertNotIn('group:8:7',env['histories'])
        group=env['histories'].setdefault('group:8:7',env['Session']())
        self.assertEqual(env['_history_messages'](group),[])
        env['_save_histories']()
        self.assertFalse(env['logger'].warning.called,env['logger'].warning.call_args)
        saved=json.loads(path.read_text())
        self.assertIn('PRIVATE_SECRET',str(saved['private:7']))
        self.assertNotIn('7',saved)

    def test_saving_one_scope_does_not_publish_another_unfinished_turn(self):
        env, path = self.history_env()
        initial = {"private:7": [{"role": "user", "content": "old private"}],
                   "group:8": [{"role": "user", "content": "old group", "speaker_id": "7"}]}
        shared_history.atomic_json(path, initial)
        self.assertTrue(env["_load_histories"]())
        env["histories"]["private:7"].recent[0]["content"] = "web reply"
        env["histories"]["group:8"].recent[0]["content"] = "unfinished group turn"
        self.assertTrue(env["_save_histories"]("private:7"))
        self.assertEqual(shared_history.read_session(path, "group:8")["recent"], initial["group:8"])
        self.assertEqual(shared_history.read_session(path, "private:7")["recent"][0]["content"], "web reply")
        env["histories"].pop("private:7")
        self.assertTrue(env["_save_histories"]("private:7"))
        self.assertNotIn("private:7", shared_history.read_sessions(path))
        self.assertTrue(env["_save_histories"]("group:8"))
        self.assertEqual(shared_history.read_session(path, "group:8")["recent"][0]["content"], "unfinished group turn")

    def test_failed_reload_keeps_live_session_and_failed_save_can_be_retried(self):
        env, path = self.history_env()
        path.write_text('{"private:7": {"sections": ["archive"]}}', encoding="utf-8")
        self.assertTrue(env["_load_histories"]())
        session = env["histories"]["private:7"]
        self.assertEqual(session.archive_text(), "archive")
        session.meta_mode = True
        env["_refresh_history_scope"]("private:7")
        session = env["histories"]["private:7"]
        self.assertTrue(session.meta_mode)
        session.recent.append({"role": "assistant", "content": "reply"})
        path.write_text("broken JSON", encoding="utf-8")
        self.assertFalse(env["_load_histories"]())
        self.assertIs(env["histories"]["private:7"], session)
        self.assertFalse(env["_save_histories"]("private:7"))
        self.assertEqual(path.read_text(encoding="utf-8"), "broken JSON")
        self.assertEqual(env["_HISTORY_BASELINE"]["private:7"]["recent"], [])
        path.write_text('{"private:7": {"sections": ["archive"]}}', encoding="utf-8")
        self.assertTrue(env["_save_histories"]("private:7"))
        self.assertEqual(shared_history.read_session(path, "private:7")["recent"], session.recent)

    def test_group_supplement_is_one_way_and_excludes_other_users(self):
        histories={'private:7':['PRIVATE_SECRET'],'group:8:7':['PUBLIC_INFO'],'group:8:9':['OTHER_USER']}
        render=lambda s:[{'role':'user','content':x} for x in s]
        context=scope.group_context(histories,'7',render)
        self.assertIn('PUBLIC_INFO',context)
        self.assertNotIn('PRIVATE_SECRET',context)
        self.assertNotIn('OTHER_USER',context)

    def test_group_quotes_fail_closed_for_private_other_group_or_unknown_source(self):
        for data in ({'message_type':'private'},{'message_type':'group','group_id':9},{'message_type':'group'},{}):
            self.assertFalse(scope.quote_allowed(self.group,data))
        self.assertTrue(scope.quote_allowed(self.group,{'message_type':'group','group_id':8}))

    def test_unknown_slash_command_follows_normal_addressing_rules(self):
        tree = ast.parse((STAGE / "__init__.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_should_reply")
        env = dict(MessageEvent=MessageEvent, CommandStart=CommandStart, _group_allowed=lambda _: True,
                   _in_cooldown=lambda _: False, _is_private=lambda _: False,
                   message_io=types.SimpleNamespace(addressed_to_bot=lambda event: event.is_tome()))
        exec(compile(ast.Module(body=[node], type_ignores=[]), "chat-rule-test", "exec"), env)
        rule = Rule(env["_should_reply"])

        async def match(text, mentioned=True):
            event = GroupMessageEvent(
                time=1, self_id=1, post_type="message", message_type="group", sub_type="normal",
                user_id=7, group_id=8, message_id=15, raw_message=text, font=0,
                message=Message(text), original_message=Message(text), sender={"user_id": 7}, to_me=mentioned,
            )
            state = {}
            TrieRule.get_value(None, event, state)
            return await rule(None, event, state)

        async def run():
            with patch.object(TrieRule, "prefix", type(TrieRule.prefix)()):
                TrieRule.add_prefix("/chat", TRIE_VALUE("/", ("chat",)))
                self.assertTrue(await match("/goal 帮我整理"))
                self.assertFalse(await match("/goal 帮我整理", mentioned=False))
                self.assertFalse(await match("/chat 模型"))
        asyncio.run(run())

    def test_completed_question_and_answer_survive_failed_send_and_retry_in_the_same_scope(self):
        env, path = self.history_env()
        env.update(time=time, _session_key=scope.session_key, _scope_lock=shared_history.scope_lock,
                   sender_name=scope.sender_name, HISTORY_KEEP_RECENT=40,
                   _send_problem_delivery=AsyncMock(side_effect=[RuntimeError("send failed"), None]))
        other = [{"role": "user", "content": "私聊消息"}]
        shared_history.atomic_json(path, {"private:7": {"recent": other}})
        delivery = types.SimpleNamespace(answer="因为 f 连续，可以使用介值定理。", question="证明存在零点。",
                                         history_saved=False)

        async def run():
            with self.assertRaises(RuntimeError):
                await env["_deliver_problem"](object(), self.group, delivery)
            await env["_deliver_problem"](object(), self.group, delivery)
        asyncio.run(run())
        records = shared_history.read_session(path, "group:8")["recent"]
        self.assertEqual(len(records), 1)
        self.assertIn(delivery.question, records[0]["content"])
        self.assertIn(delivery.answer, records[0]["content"])
        self.assertEqual(records[0]["speaker_id"], "7")
        self.assertEqual(shared_history.read_session(path, "private:7")["recent"], other)

    def test_legacy_unlabelled_memory_is_not_public(self):
        path=self.root/'memory.json';path.write_text(json.dumps({'7':[{'key':'秘密','value':'PRIVATE_SECRET'}]}))
        store=memory.MemoryStore(path)
        self.assertEqual(store.render('7',100,scope='group'),'')
        self.assertIn('PRIVATE_SECRET',store.render('7',100))

    def test_duplicate_public_memory_cannot_relabel_or_overwrite_private_memory(self):
        store=memory.MemoryStore(self.root/'memory.json')
        store.add('7','喜好','PRIVATE_SECRET',100,scope='private')
        store.add('7','喜好','PRIVATE_SECRET_EXTRA',100,scope='group')
        self.assertEqual(store.list('7','private')[0].value,'PRIVATE_SECRET')
        store.set('7','喜好','NEW_PUBLIC',100,scope='group')
        self.assertEqual(store.list('7','private')[0].value,'PRIVATE_SECRET')
        self.assertNotIn('PRIVATE_SECRET',store.render('7',100,scope='group'))

    def test_group_timeline_uses_timestamp_and_speaker_and_excludes_other_scopes(self):
        env, path = self.history_env()
        def turn(uid, at, content):
            speaker = {"speaker_id": uid, "speaker_name": "同名", "at": at}
            return [{"role": "user", "content": content, **speaker},
                    {"role": "assistant", "content": "收到", **speaker}]
        shared_history.atomic_json(path, {
            "group:8:7": turn("7", 2, "第二条"),
            "group:8:9": turn("9", 1, "第一条"),
            "group:9:7": turn("7", 1, "其他群"),
            "private:7": turn("7", 1, "私聊"),
        })
        self.assertTrue(env["_load_histories"]())
        self.assertEqual(set(shared_history.read_sessions(path)), {"group:8", "group:9", "private:7"})
        session = env["histories"]["group:8"]
        messages = [scope.attributed_message(m) for m in env["_history_messages"](session)]
        self.assertEqual(len(messages), 4)
        self.assertIn("同名（QQ 9）", messages[0]["content"])
        self.assertIn("第一条", messages[0]["content"])
        self.assertIn("同名（QQ 7）", messages[2]["content"])
        self.assertIn("第二条", messages[2]["content"])
        self.assertNotIn("其他群", str(messages))
        self.assertNotIn("私聊", str(messages))
        context = scope.group_context(env["histories"], "7", env["_history_messages"])
        self.assertIn("第二条", context)
        self.assertNotIn("第一条", context)

    def test_compaction_leaves_twenty_and_keeps_concurrent_turns(self):
        env, path = self.history_env()
        key = "group:8"
        original = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"line {i}"}
                    for i in range(36)]
        prior = {"items": [{"type": "compaction", "encrypted_content": "old"}], "tokens": 30, "model": "gpt-test"}
        new = {**prior, "items": [{"type": "compaction", "encrypted_content": "new"}]}
        shared_history.atomic_json(path, {key: {"recent": original, "compaction": prior}})

        async def run():
            started, finish = asyncio.Event(), asyncio.Event()

            async def compact(messages, api_key, model, previous):
                self.assertEqual(previous, prior)
                self.assertEqual(messages, original[:-20])
                started.set()
                await finish.wait()
                return new, ""

            env.update(OPENAI_KEY="fake", OPENAI_CHAT_MODEL="gpt-test", HISTORY_KEEP_RECENT=20,
                       HISTORY_RECENT_BYTES=12 * 1024, HISTORY_COMPRESS_AT=36, HISTORY_COMPRESS_BATCH=16,
                       _compressing_histories=set(), _scope_lock=shared_history.scope_lock,
                       attributed_message=scope.attributed_message,
                       llm=types.SimpleNamespace(compact_history=compact, clip_text=lambda text, limit: text))
            task = asyncio.create_task(env["_maybe_compress_history"](key))
            await asyncio.wait_for(started.wait(), 2)
            async with asyncio.timeout(2):
                async with shared_history.scope_lock(path, key):
                    env["_refresh_history_scope"](key)
                    session = env["histories"][key]
                    self.assertEqual(session.compaction, prior)
                    self.assertEqual(env["_history_messages"](session)[-20:], original[-20:])
                    session.recent.extend(original[:2])
                    self.assertTrue(env["_save_histories"](key))
            finish.set()
            await asyncio.wait_for(task, 2)
            saved = shared_history.read_session(path, key)
            self.assertEqual(saved["recent"], original[-20:] + original[:2])
            self.assertEqual(saved["compaction"], new)

        asyncio.run(run())

    def test_memory_group_sources_survive_updates_from_another_store(self):
        path = self.root / "memory.json"
        first = memory.MemoryStore(path)
        second = memory.MemoryStore(path)
        first.set("7", "喜好", "喜欢咖啡", 100, scope="group", group_id="8")
        second.set("7", "喜好", "喜欢茶", 100, scope="group", group_id="9")
        second.set("9", "喜好", "喜欢果汁", 100, scope="group", group_id="8")
        first.load()
        self.assertIn("喜欢咖啡", first.render("7", 100, scope="group", group_id="8"))
        self.assertNotIn("喜欢茶", first.render("7", 100, scope="group", group_id="8"))
        self.assertIn("喜欢茶", first.render("7", 100, scope="group", group_id="9"))
        self.assertIn("喜欢果汁", first.render("9", 100, scope="group", group_id="8"))

    def test_handler_uses_scoped_history_images_cache_and_public_only_commands(self):
        s=(STAGE/'__init__.py').read_text(encoding='utf-8')
        self.assertIn('histories.get(context_key)',s)
        self.assertIn('_take_pending_images(context_key)',s)
        self.assertIn('_pending_images.setdefault(_session_key(event, user_id)',s)
        self.assertIn('vertex_cache_scope=context_key',s)
        self.assertIn('_maybe_compress_history(context_key)',s)
        self.assertIn('memory_store.list(uid, scope=None if _is_private(event) else "group", group_id=_current_group(event))',s)
        self.assertIn('if mem_scope == "private":\n        public_context',s)
        self.assertIn('await bot.adapter.send_affection_history(bot, event, messages)',s)

if __name__=='__main__': unittest.main()
