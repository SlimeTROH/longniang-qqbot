import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock,Mock

ROOT=Path(__file__).resolve().parents[1]


class NormalChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session=types.SimpleNamespace(recent=[],summary='',sections=[])
        self.result=types.SimpleNamespace(ok=True,reply='a fresh reply',event='none',reason='',usage={},elapsed=.1)
        self.llm=types.SimpleNamespace(build_static_system=Mock(return_value='stable persona'),
            build_dynamic_block=Mock(return_value='dynamic state'),decide_length=AsyncMock(return_value=types.SimpleNamespace(max_chars=137,level='medium',reason='',has_daily=False)),
            chat=AsyncMock(return_value=self.result))
        @asynccontextmanager
        async def scope(key):
            self.scope_key=key;yield
        self.cb=types.SimpleNamespace(histories={},Session=lambda:self.session,_history_scope=scope,
            llm=self.llm,LENGTH_ENABLED=True,DEEPSEEK_KEY='fake',DEEPSEEK_MODEL='judge',WEB_ENABLED=False,
            LENGTH_MULTIPLIER=1.0,memory_store=types.SimpleNamespace(render=Mock(return_value='memory')),
            _group_context=Mock(return_value='public context'),_history_messages=lambda s:list(s.recent),
            _group_history_messages=lambda s:list(s.recent[-8:]),
            ARK_KEY='fake',ARK_MODEL='fallback',GEMINI_CHAT_KEY='fake',GEMINI_CHAT_MODEL='model',GEMINI_CHAT_BASE='local',
            VERTEX_AUTH='fake',VERTEX_PROXY='http://127.0.0.1:10808',VERTEX_THINKING='low',VERTEX_SESSION_CACHE=True,
            _save_histories=Mock(),HISTORY_COMPRESS_AT=80,_maybe_compress_history=AsyncMock(),
            store=types.SimpleNamespace(apply=Mock(return_value=types.SimpleNamespace(before=0,after=0,delta=0))))
        self.cb._apply_shared_affection=Mock(side_effect=lambda store,*args,**kwargs:store.apply(*args,**kwargs))
        functions=[n for n in ast.parse((ROOT/'erp_server.py').read_text(encoding='utf-8')).body
                   if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in ('_background','_normal_chat','_normal_chat_unlocked')]
        self.env={'asyncio':asyncio,'cb':self.cb,'ADMIN_UID':'test-admin','erp':types.SimpleNamespace(persona_obj=types.SimpleNamespace(base='persona',name='Nova')),
                  '_chat_aff':lambda:0,'_maybe_search':AsyncMock(return_value='search'),'_TASKS':set(),
                  'nonebot':types.SimpleNamespace(logger=Mock()),'random':__import__('random')}
        exec(compile(ast.Module(body=functions,type_ignores=[]),'nova-backend-test','exec'),self.env)

    async def asyncTearDown(self):
        for task in self.env['_TASKS']:task.cancel()
        await asyncio.gather(*self.env['_TASKS'],return_exceptions=True)

    async def test_normal_chat_uses_private_scope_cache_and_same_length_requirement(self):
        await self.env['_normal_chat']('hello',[b'image'])
        self.assertEqual(self.scope_key,'private:test-admin')
        self.cb._group_context.assert_called_once_with(self.cb.histories, 'test-admin', self.cb._group_history_messages)
        request=self.llm.chat.call_args
        self.assertEqual(request.kwargs['vertex_cache_scope'],'private:test-admin')
        self.assertEqual(request.kwargs['min_chars'],137)
        self.assertEqual(request.kwargs['images'],[b'image'])
        self.assertEqual(len(self.session.recent),2)
        self.cb._save_histories.assert_called_once_with("private:test-admin")

    async def test_archive_runs_in_background_after_saving_reply(self):
        self.session.recent=[{'role':'user','content':'test'} for _ in range(78)]
        release=asyncio.Event();entered=asyncio.Event()
        async def compress(key):entered.set();await release.wait()
        self.cb._maybe_compress_history=AsyncMock(side_effect=compress)
        await asyncio.wait_for(self.env['_normal_chat']('hello'),.5)
        self.cb._save_histories.assert_called_once_with("private:test-admin")
        await asyncio.sleep(0)
        self.assertTrue(entered.is_set())
        self.assertEqual(len(self.session.recent),80)
        release.set()

    async def test_failed_generation_does_not_change_memory_or_affection(self):
        self.result.ok=False;self.result.reply=''
        await self.env['_normal_chat']('hello')
        self.assertEqual(self.session.recent,[])
        self.cb._save_histories.assert_not_called()
        self.cb.store.apply.assert_not_called()

    async def test_repetition_retry_keeps_images_and_length_constraints(self):
        self.session.recent=[{'role':'assistant','content':'a fresh reply'}]
        second=types.SimpleNamespace(ok=True,reply='different reply',event='none',reason='')
        self.llm.chat.side_effect=[self.result,second]
        await self.env['_normal_chat']('hello',[b'image'])
        self.assertEqual(self.llm.chat.await_count,2)
        self.assertEqual(self.llm.chat.call_args.kwargs['images'],[b'image'])
        self.assertEqual(self.llm.chat.call_args.kwargs['min_chars'],137)

    async def test_length_judge_and_search_start_concurrently(self):
        search_started=asyncio.Event()
        async def search(text):search_started.set();return 'search'
        async def judge(*args,**kwargs):
            await asyncio.wait_for(search_started.wait(),.2)
            return types.SimpleNamespace(max_chars=100,level='medium',reason='',has_daily=False)
        self.env['_maybe_search']=search;self.llm.decide_length.side_effect=judge
        await self.env['_normal_chat']('hello')
        self.assertTrue(search_started.is_set())


if __name__=='__main__':unittest.main()
