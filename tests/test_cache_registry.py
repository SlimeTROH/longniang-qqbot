import asyncio
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock,Mock,patch
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import cache_registry
from test_network_stability import load_vertex


class RegistryTests(unittest.TestCase):
    def test_invalidating_old_cache_does_not_remove_another_workers_renewed_entry(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cache.json'
            old={'a':{'name':'cache/old','expire':100}}
            renewed={'a':{'name':'cache/new','expire':900}}
            cache_registry.save(path,old,{})
            cache_registry.save(path,renewed,old)
            cache_registry.save(path,{},old)
            self.assertEqual(cache_registry.load(path),renewed)

    def test_stale_worker_preserves_other_workers_new_cache_records(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cache.json'
            initial={'a':{'name':'cache/a','expire':100}}
            cache_registry.save(path,initial,{})
            cache_registry.save(path,{**initial,'b':{'name':'cache/b','expire':200}},initial)
            cache_registry.save(path,{'a':{'name':'cache/renewed','expire':300}},initial)
            self.assertEqual(set(cache_registry.load(path)),{'a','b'})


class CachePublicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_worker_observes_another_workers_published_persona_without_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cache.json'
            v=load_vertex();v._CACHE_FILE=path;v._REFS_CACHE=[]
            self.assertEqual(v._lookup_persona_cache('persona','m'),'')
            key=v._persona_cache_key('persona','m')
            cache_registry.save(path,{key:{'name':'cache/published','expire':time.time()+900}}, {})
            self.assertEqual(v._lookup_persona_cache('persona','m'),'cache/published')
            await v.close_clients()

    async def test_two_workers_build_the_same_persona_cache_only_once(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'cache.json';first=load_vertex();second=load_vertex()
            for v in (first,second):v._CACHE_FILE=path;v._REFS_CACHE=[]
            post=AsyncMock(return_value=httpx.Response(200,json={'name':'cache/shared'}))
            client=Mock(post=post)
            args=dict(project='p',location='global',model='m',persona='x'*600,token='fake',proxy='')
            with patch.object(first,'_get_client',return_value=client),patch.object(second,'_get_client',return_value=client):
                names=await asyncio.gather(first._get_or_create_cache(**args),second._get_or_create_cache(**args))
            self.assertEqual(names,['cache/shared','cache/shared'])
            self.assertEqual(post.await_count,1)
            await first.close_clients();await second.close_clients()


if __name__=='__main__':unittest.main()
