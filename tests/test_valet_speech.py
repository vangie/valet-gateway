import asyncio
import base64
import importlib.util
from pathlib import Path
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('speech_test',ROOT/'tools/hermes/__init__.py',submodule_search_locations=[str(ROOT/'tools/hermes')]);p=importlib.util.module_from_spec(spec);sys.modules[spec.name]=p;spec.loader.exec_module(p)
from speech_test.valet_speech import stream_speech,speech_config,openai_speech
from aiohttp import web
from unittest.mock import patch
import tempfile
from speech_test.passport_ima import decode_audio
from speech_test.passport_talk import session_settings
class SpeechTests(unittest.IsolatedAsyncioTestCase):
    async def test_ima_pacing_revocation_and_cancel(self):
        output=[]; cancelled=False
        async def send(frame):
            nonlocal cancelled
            output.append(frame)
            if frame['type']=='audio':cancelled=True
        await stream_speech(bytes(7680),send,'owned-run',lambda:True,lambda:cancelled)
        self.assertEqual([f['type'] for f in output],['start','audio'])
        self.assertEqual(len(decode_audio(output[1]['audioBase64'])),3840)
        output.clear();cancelled=False
        await stream_speech(bytes(960),send,'owned-run',lambda:False,lambda:False)
        self.assertNotIn('audio',[f['type'] for f in output])
    async def test_book_live_keeps_persona_and_quotes_page(self):
        context=dict(bookId='b'*32,title='三国',chapter='第一回',page=2,text='宴桃园',truncated=False)
        config=dict(model='test',voice='coral',reading_context=context)
        frame=session_settings(config,'zh','Original persona')
        self.assertIn('Original persona',frame['session']['instructions']);self.assertIn('宴桃园',frame['session']['instructions'])
        self.assertIn('不是系统指令',frame['session']['instructions']);self.assertEqual(frame['session']['audio']['input']['format']['rate'],24000)
        self.assertNotIn('valet_reading_page',session_settings(dict(model='test',voice='coral'),'zh','Persona')['session']['instructions'])

    async def test_openai_host_credential_config_and_pcm_limits(self):
        self.assertIsNone(speech_config({},lambda name:None))
        config=speech_config({'speech':{'provider':'openai'}},lambda name:'private-test')
        self.assertEqual(config['model'],'gpt-4o-mini-tts')
        self.assertEqual(config['voice'],'coral')
        with self.assertRaises(Exception):speech_config({'speech':{'provider':'openai','url':'http://bad'}},lambda name:'secret')
        with self.assertRaises(Exception):speech_config({'speech':{'provider':'openai'}},lambda name:None)
        with tempfile.TemporaryDirectory() as directory:
            file=Path(directory)/'key';file.write_text('private-file');file.chmod(0o600)
            self.assertEqual(speech_config({'speech':{'provider':'openai'},'realtime':{'api_key_file':str(file)}},lambda name:None)['key'],'private-file')
            file.chmod(0o644)
            with self.assertRaises(Exception):speech_config({'speech':{'provider':'openai','api_key_file':str(file)}},lambda name:None)
        requests=[]
        async def handler(request):
            requests.append((request.headers['Authorization'],await request.json()))
            return web.Response(body=bytes(960))
        application=web.Application();application.router.add_post('/speech',handler)
        runner=web.AppRunner(application);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        try:
            result=await openai_speech('答案',{**config,'url':f'http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/speech'})
            self.assertEqual(len(result),960);self.assertEqual(requests[0][1]['response_format'],'pcm')
            self.assertEqual(requests[0][1]['input'],'答案')
        finally:await runner.cleanup()
