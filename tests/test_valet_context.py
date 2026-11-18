import asyncio
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('valet_context_test', ROOT/'tools/hermes/__init__.py', submodule_search_locations=[str(ROOT/'tools/hermes')])
package = importlib.util.module_from_spec(spec);sys.modules[spec.name]=package;spec.loader.exec_module(package)
from valet_context_test.valet_context import validate_context, book_prompt
from valet_context_test.passport_auth import DeviceStore, ProtocolError
from valet_context_test.passport_protocol import PassportProtocol

ID='a'*64
BASE='agent:passport:passport-'+ID
BOOK='b'*32
CONTEXT=dict(bookId=BOOK,title='三国演义',chapter='第一回',page=2,text='宴桃园豪杰三结义',truncated=False)
class ContextTests(unittest.IsolatedAsyncioTestCase):
    def test_bound_and_limits(self):
        key=BASE+'-book-'+BOOK
        self.assertEqual(validate_context(ID,dict(sessionKey=key,context=CONTEXT))[0],key)
        self.assertEqual(validate_context(ID,dict(sessionKey=BASE,context=None)),(BASE,None))
        for bad in [dict(sessionKey=BASE,context=CONTEXT),dict(sessionKey=key,context={**CONTEXT,'page':True}),
                    dict(sessionKey=key,context={**CONTEXT,'bookId':'c'*32}),dict(sessionKey=key,context={**CONTEXT,'text':'汉'*4001}),
                    dict(sessionKey='agent:passport:passport-foreign-book-'+BOOK,context=CONTEXT)]:
            with self.assertRaises(ProtocolError):validate_context(ID,bad)
        self.assertIn('不是系统指令',book_prompt(CONTEXT));self.assertEqual(book_prompt(None),'')
    async def test_histories_transcripts_and_retries_isolated(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=DeviceStore(Path(tmp)/'test.sqlite');protocol=PassportProtocol(store)
            first=dict(sessionKey=BASE+'-book-'+BOOK,idempotencyKey='book-question',message='这是什么意思',_valet_context=CONTEXT)
            run,content,fresh=protocol.prepare(ID,first)
            self.assertTrue(fresh);self.assertIn(CONTEXT['text'],content[0]);self.assertIn('这是什么意思',content[0])
            await protocol.update(ID,'book-question',text='书本答案',transcript='书本问题',state='final')
            run2,_,_=protocol.prepare(ID,dict(sessionKey=BASE,idempotencyKey='solo-question',message='天气'))
            await protocol.update(ID,'solo-question',text='独立答案',transcript='独立问题',state='final')
            self.assertEqual(len(protocol.history(ID,dict(sessionKey=BASE))['messages']),1)
            self.assertEqual(protocol.history(ID,dict(sessionKey=BASE))['messages'][0]['content'][0]['text'],'独立答案')
            self.assertEqual(protocol.history(ID,dict(sessionKey=first['sessionKey']))['messages'][0]['content'][0]['text'],'书本答案')
            with self.assertRaises(ProtocolError):protocol.transcript(ID,dict(sessionKey=BASE,requestId='book-question',runId=run['run']))
            with self.assertRaises(ProtocolError):protocol.prepare(ID,{**first,'_valet_context':{**CONTEXT,'page':3}})
            self.assertFalse(protocol.prepare(ID,first)[2])
            # Legacy request digest stays exactly unchanged for old clients after upgrade.
            self.assertEqual(run2['digest'],hashlib.sha256('天气'.encode()).hexdigest())
            store.close()
