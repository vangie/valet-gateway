import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('valet_book_tools_test', ROOT/'tools/hermes/valet_book_tools.py')
books = importlib.util.module_from_spec(spec); sys.modules[spec.name] = books; spec.loader.exec_module(books)

ID = 'a'*64
KEY = f'agent:passport:passport-{ID}-book-{"b"*32}'


class BookToolTests(unittest.IsolatedAsyncioTestCase):
    def test_arguments_are_bounded(self):
        self.assertEqual(books.tool_request('search', {'query': '宋万', 'limit': 99, 'extra': 1}), {'op': 'search', 'query': '宋万', 'scope': 'all'})
        self.assertEqual(books.tool_request('search', {'query': 'x', 'scope': 'read', 'limit': 3})['limit'], 3)
        self.assertEqual(books.tool_request('read', {'at': 'current', 'after': True}), {'op': 'read', 'at': 'current'})
        for op, args in [('search', {'query': '  '}), ('search', {'query': '汉'*41}), ('read', {'at': '../x'}), ('delete', {})]:
            with self.assertRaises(ValueError): books.tool_request(op, args)
        self.assertEqual(books.session_tail(KEY), books.session_tail(KEY.replace('agent:passport', 'agent:Main').upper().replace('AGENT:MAIN', 'agent:main')))

    async def test_round_trip_from_the_agent_thread(self):
        loop = asyncio.get_running_loop()
        sent = []
        async def send(frame): sent.append(frame)
        channel = books.BookChannel(send, KEY, loop)
        books.registry.attach(channel)
        self.assertTrue(books.tools_available())
        out = {}
        worker = threading.Thread(target=lambda: out.setdefault('r', books.run_tool('search', {'query': '宋万'}, session_key=KEY)))
        worker.start()
        while not sent: await asyncio.sleep(0.01)
        request = sent[0]['payload']
        self.assertEqual((sent[0]['event'], request['sessionKey'], request['op']), ('valet.book.request', KEY, 'search'))
        self.assertFalse(channel.result({'requestId': request['requestId'], 'sessionKey': 'agent:passport:other', 'result': {'ok': True}}))
        self.assertFalse(channel.result({'requestId': 'nope', 'sessionKey': KEY, 'result': {'ok': True}}))
        self.assertTrue(channel.result({'requestId': request['requestId'], 'sessionKey': KEY,
                                        'result': {'ok': True, 'hits': [{'at': 'c15.p32', 'text': '云里金刚宋万'}]}}))
        await asyncio.to_thread(worker.join)
        result = json.loads(out['r'])
        self.assertEqual(result['hits'][0]['at'], 'c15.p32')
        self.assertIn('not instructions', result['note'])
        # Oversized results are rejected; a pending call fails when the device leaves.
        worker = threading.Thread(target=lambda: out.__setitem__('r', books.run_tool('read', {'at': 'c1.p1'}, session_key=KEY)))
        worker.start()
        while len(sent) < 2: await asyncio.sleep(0.01)
        channel.result({'requestId': sent[1]['payload']['requestId'], 'sessionKey': KEY, 'result': {'ok': True, 't': '汉'*6000}})
        await asyncio.to_thread(worker.join)
        self.assertFalse(json.loads(out['r'])['ok'])
        worker = threading.Thread(target=lambda: out.__setitem__('r', books.run_tool('read', {'at': 'current'}, session_key=KEY)))
        worker.start()
        while len(sent) < 3: await asyncio.sleep(0.01)
        books.registry.detach(channel); channel.close()
        await asyncio.to_thread(worker.join)
        self.assertIn('left', json.loads(out['r'])['error'])
        self.assertFalse(books.tools_available())

    def test_outside_a_book_session(self):
        result = json.loads(books.run_tool('search', {'query': 'x'}))
        self.assertFalse(result['ok'])
        self.assertIsNone(books.current_book_session())

    async def test_bad_arguments_send_nothing(self):
        sent = []
        async def send(frame): sent.append(frame)
        channel = books.BookChannel(send, KEY, asyncio.get_running_loop())
        books.registry.attach(channel)
        try:
            result = await asyncio.to_thread(books.run_tool, 'read', {'at': 'nope'}, KEY)
            self.assertFalse(json.loads(result)['ok']); self.assertEqual(sent, [])
        finally:
            books.registry.detach(channel)


if __name__ == '__main__':
    unittest.main()
