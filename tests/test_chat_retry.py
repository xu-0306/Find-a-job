"""Bounded model retries and saved progress; synthetic providers and disposable data."""
import asyncio
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx

from app import chat
from app.store import encode
import test_m2_chat as chats


class RetryChecks(unittest.TestCase):
    setUp = chats.ChatChecks.setUp
    tearDown = chats.ChatChecks.tearDown
    send = chats.ChatChecks.send
    history = chats.ChatChecks.history

    def fast_retries(self, delays):
        sleep = asyncio.sleep
        async def advance(delay):
            if delay == chat.RETRY_DELAY_SECONDS:
                delays.append(delay)
                await sleep(0)
            else:
                await sleep(delay)
        return patch('app.chat.asyncio.sleep', advance)

    def test_five_retries_wait_five_seconds_without_duplicate_messages(self):
        calls, delays = [], []
        self.app.state.llm_transport = httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(503, text=chats.KEY))
        with self.fast_retries(delays):
            output = chats.events(self.send())
        retries = [value for name, value in output if name == 'retry']
        self.assertEqual(len(calls), 6)
        self.assertEqual(delays, [5] * 5)
        self.assertEqual([value['attempt'] for value in retries], list(range(1, 6)))
        self.assertTrue(all(value['max_attempts'] == 5 and value['delay_seconds'] == 5 for value in retries))
        self.assertEqual(output[-1][1]['status'], 'interrupted')
        self.assertEqual(len(self.history()), 2)
        self.assertNotIn(chats.KEY, encode(output))
        self.assertEqual(self.app.state.active_chats, {})

    def test_partial_retry_replaces_failed_fragment_and_keeps_last_partial_on_exhaustion(self):
        calls, delays = [], []
        def provider(request):
            calls.append(request)
            return chats.response(text='失敗片段', complete=False) if len(calls) == 1 else chats.response(text='新完整回覆')
        self.app.state.llm_transport = httpx.MockTransport(provider)
        with self.fast_retries(delays):
            output = chats.events(self.send())
        self.assertEqual(delays, [5])
        self.assertIn(('text_reset', {'text': ''}), output)
        self.assertEqual(self.history()[-1]['content']['text'], '新完整回覆')
        self.assertEqual(output[-1][1]['status'], 'completed')
        self.app.state.llm_transport = httpx.MockTransport(lambda request: chats.response(text='最後可用片段', complete=False))
        with self.fast_retries([]):
            output = chats.events(self.send('exhausted'))
        self.assertEqual(self.history()[-1]['content']['text'], '最後可用片段')
        self.assertEqual(output[-1][1]['status'], 'interrupted')

    def test_completed_tool_round_is_not_replayed(self):
        calls = []
        def provider(request):
            calls.append(json.loads(request.content))
            if len(calls) == 1:
                return chats.response(text='已提出，等你確認。', call=('proposal', 'propose_preference_change',
                    encode({'operation': 'create', 'after': chats.CRITERIA})))
            if len(calls) == 2:
                return chats.response(text='未完成後半段', complete=False)
            return chats.response(text='回覆完成。')
        self.app.state.llm_transport = httpx.MockTransport(provider)
        with self.fast_retries([]):
            output = chats.events(self.send())
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(self.client.get('/api/preferences').json()['proposals']), 1)
        self.assertEqual(sum(name == 'preference_proposal' for name, _ in output), 1)
        self.assertEqual(self.history()[-1]['content']['text'], '已提出，等你確認。回覆完成。')
        self.assertIn(('text_reset', {'text': '已提出，等你確認。'}), output)
        self.assertEqual(calls[1], calls[2])

    def test_rate_limit_retry_works_for_all_three_protocols(self):
        for protocol in ('responses', 'chat_completions', 'anthropic_messages'):
            with self.subTest(protocol=protocol):
                with self.store.connect(write=True) as db:
                    self.store.put(db, 'services', chats.SETTINGS | {'llm_protocol': protocol})
                payloads, delays = [], []
                def provider(request):
                    payloads.append(json.loads(request.content))
                    return httpx.Response(429) if len(payloads) == 1 else chats.response(protocol, text='已恢復')
                self.app.state.llm_transport = httpx.MockTransport(provider)
                with self.fast_retries(delays):
                    output = chats.events(self.send(protocol))
                self.assertEqual(output[-1][1]['status'], 'completed')
                self.assertEqual(delays, [5])
                self.assertEqual(payloads[0], payloads[1])

    def test_retry_budget_is_shared_across_tool_rounds(self):
        calls, delays = [], []
        def provider(request):
            calls.append(request)
            if len(calls) in (3, 6):
                return chats.response(call=(str(len(calls)), 'list_preferences', '{}'))
            return httpx.Response(500)
        self.app.state.llm_transport = httpx.MockTransport(provider)
        with self.fast_retries(delays):
            output = chats.events(self.send())
        self.assertEqual(len(calls), 8)
        self.assertEqual(delays, [5] * 5)
        self.assertEqual(output[-1][1]['status'], 'interrupted')
        self.assertEqual(sum(name == 'retry' for name, _ in output), 5)

    def test_auth_input_and_refusal_are_not_retried(self):
        for status in (400, 401, 403, 404, 501):
            calls, delays = [], []
            self.app.state.llm_transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(status))
            with self.fast_retries(delays):
                output = chats.events(self.send(f'status-{status}'))
            self.assertEqual(len(calls), 1)
            self.assertFalse(delays)
            self.assertFalse(any(name == 'retry' for name, _ in output))
        self.app.state.llm_transport = httpx.MockTransport(lambda request: chats.response(call=('invalid', 'shell', '{}')))
        with self.fast_retries([]):
            output = chats.events(self.send('invalid-tool'))
        self.assertFalse(any(name == 'retry' for name, _ in output))

        calls = []
        self.app.state.llm_transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(
            200, headers={'Content-Type': 'text/event-stream'},
            content=('data: ' + encode({'type': 'response.refusal.done'}) + '\n\n').encode()))
        with self.fast_retries([]):
            output = chats.events(self.send('refusal'))
        self.assertEqual(len(calls), 1)
        self.assertEqual(next(value['code'] for name, value in output if name == 'error'), 'refusal')

    def test_cancel_during_retry_wait_and_progress_auth(self):
        attempted = threading.Event()
        self.app.state.llm_transport = httpx.MockTransport(lambda request: attempted.set() or httpx.Response(500))
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(self.send)
            self.assertTrue(attempted.wait(5))
            progress = self.client.get('/api/chat/one').json()
            self.assertEqual(progress['conversation_id'], 'legacy')
            self.assertEqual([item['role'] for item in progress['items']], ['user', 'assistant'])
            self.assertEqual(progress['items'][1]['content']['status'], 'running')
            self.assertEqual(self.client.post('/api/chat/one/cancel').json()['status'], 'interrupted')
            self.assertEqual(chats.events(pending.result(5))[-1][1]['status'], 'interrupted')
        self.assertEqual(self.client.get('/api/chat/missing').status_code, 404)
        self.client.cookies.delete('jc_user')
        self.assertEqual(self.client.get('/api/chat/one').status_code, 401)
        self.client.cookies.set('jc_admin', self.admin_token)
        self.assertEqual(self.client.get('/api/chat/one').status_code, 401)

    def test_reader_disconnect_does_not_cancel_producer(self):
        async def exercise():
            released = asyncio.Event()
            async def producer():
                await released.wait()
            task = asyncio.create_task(producer())
            stream = chat.reply_stream(task, asyncio.Queue(), 'synthetic')
            await anext(stream)
            await stream.aclose()
            self.assertFalse(task.cancelled())
            self.assertFalse(task.done())
            released.set()
            await task
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
