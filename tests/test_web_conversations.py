import json
from pathlib import Path
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from web_conversations import Conversations

class ConversationTests(unittest.TestCase):
    def test_legacy_is_preserved_and_new_conversations_are_independent(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'conversations.json'
            original={'history':[{'role':'user','content':'original'}],'n':1,'summary':'kept'}
            store=Conversations(path,original,'web',33)
            one=store.create('A',10,'prompt A');two=store.create('B',80,'prompt B')
            one['history'].append({'role':'user','content':'only A'})
            store.configure(one['id'],{'aff':25,'pre_prompt':'updated A','temp':1.2})
            restored=Conversations(path,{},'unused',0)
            self.assertEqual(restored.get()['history'],original['history'])
            self.assertEqual(restored.get()['summary'],'kept')
            self.assertEqual(restored.get(one['id'])['aff'],25)
            self.assertEqual(restored.get(two['id'])['aff'],80)
            self.assertEqual(restored.get(two['id'])['pre_prompt'],'prompt B')
            self.assertEqual(restored.get(two['id'])['history'],[])
            self.assertEqual(len(restored.listing()),3)

    def test_invalid_identifier_never_falls_back_to_another_conversation(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Conversations(Path(folder)/'c.json',{'history':[]},'web',0)
            with self.assertRaises(KeyError):store.get('unknown')

    def test_new_conversation_does_not_reset_older_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Conversations(Path(folder)/'c.json',{'history':[]},'web',0)
            first=store.create('first',42,'persist')
            store.create()
            self.assertEqual(store.get(first['id'])['pre_prompt'],'persist')
            self.assertEqual(store.get(first['id'])['aff'],42)

if __name__=='__main__':unittest.main()
