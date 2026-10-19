"""Context budget and compaction with synthetic providers and temporary databases only."""
import json
import unittest

import httpx

from app import context_budget, llm
from app.chat import SUMMARY_PREFIX, SUMMARY_SYSTEM
from app.llm_wire import decode_event
from app.store import encode
import test_m2_chat as chats

SMALL_WINDOW = 32_000
SUMMARY_TEXT = '使用者要找台南的軟體職缺，已排除工研院。'


class HttpErrorChecks(unittest.IsolatedAsyncioTestCase):
    async def test_admin_probe_reports_busy_and_mid_test_refusal_without_setting_advice(self):
        for status in [503, 403]:
            requests = []
            def provider(request):
                payload = json.loads(request.content); requests.append(payload)
                if status == 403 and len(requests) == 1:
                    nonce = payload['tools'][0]['parameters']['properties']['nonce']['enum'][0]
                    return chats.response(call=('probe', 'connection_probe', encode({'nonce': nonce})))
                return httpx.Response(status)
            with self.assertRaises(llm.LLMError) as caught:
                await llm.test_connection(chats.SETTINGS, 'test-key', httpx.MockTransport(provider))
            self.assertEqual(caught.exception.code, 'provider_unavailable' if status == 503 else 'provider_refused')
            self.assertNotIn('請在後台檢查設定', caught.exception.message)
            self.assertEqual(len(requests), 1 if status == 503 else 2)

    async def test_status_classification_and_safe_messages(self):
        cases = [(401, False, 'authentication_failed', False, False, True),
                 (403, False, 'authentication_failed', False, False, True),
                 (403, True, 'provider_refused', False, False, False),
                 (429, False, 'rate_limited', True, False, False),
                 (500, False, 'provider_unavailable', True, False, False),
                 (502, False, 'provider_unavailable', True, False, False),
                 (503, False, 'provider_unavailable', True, False, False),
                 (504, False, 'provider_unavailable', True, False, False),
                 (599, False, 'provider_unavailable', True, False, False),
                 (501, False, 'provider_http_error', False, True, True),
                 (404, False, 'provider_http_error', False, True, True),
                 (405, False, 'provider_http_error', False, True, True),
                 (422, True, 'provider_http_error', False, False, True)]
        for status, success, code, retryable, unsupported, setting_message in cases:
            with self.subTest(status=status, success=success):
                transport = httpx.MockTransport(lambda request: httpx.Response(status,
                    json={'error': {'type': 'service_unavailable', 'code': 'private-token!', 'message': 'private provider details'}}))
                async with httpx.AsyncClient(transport=transport) as client:
                    with self.assertRaises(llm.LLMError) as caught:
                        async for _ in llm.stream_model(client, chats.SETTINGS, 'test-key', 'responses', [], [], had_success=success):
                            pass
                error = caught.exception
                self.assertEqual((error.code, error.retryable, error.unsupported), (code, retryable, unsupported))
                self.assertEqual('請在後台檢查設定' in error.message, setting_message)
                self.assertNotIn('private', error.message)
                if code == 'provider_unavailable':
                    self.assertEqual(error.message, f'模型服務暫時無法回應（HTTP {status}），請稍後再試')
                if code == 'rate_limited':
                    self.assertEqual(error.message, '模型服務目前請求過多，請稍後再試')
                if code == 'provider_refused':
                    self.assertIn('內容或長度限制', error.message)
                    self.assertIn('已取得的內容保留', error.message)


def tool_round(index, ids):
    call = {'id': f'call-{index}', 'name': 'search_jobs', 'arguments': '{}'}
    return [{'role': 'assistant', 'content': '', 'tool_calls': [call]},
            {'role': 'tool', 'tool_call_id': call['id'], 'content': encode({'items': [{'id': ident, 'title': '職缺' * 500} for ident in ids]})}]


class BudgetUnits(unittest.TestCase):
    def test_estimate_counts_wide_characters_and_images(self):
        self.assertEqual(context_budget.text_tokens('職缺'), 2)
        self.assertEqual(context_budget.text_tokens('abcdefgh'), 2)
        image = {'role': 'user', 'content': [{'type': 'image', 'media_type': 'image/png', 'data': 'x' * 100000}, {'type': 'text', 'text': '圖'}]}
        self.assertEqual(context_budget.message_tokens(image), context_budget.IMAGE_TOKENS + 1 + context_budget.MESSAGE_OVERHEAD)

    def test_budget_uses_configured_window_or_default(self):
        self.assertEqual(context_budget.window({}), context_budget.DEFAULT_WINDOW)
        self.assertEqual(context_budget.window({'llm_context_window': 0}), context_budget.DEFAULT_WINDOW)
        self.assertLess(context_budget.budget({'llm_context_window': SMALL_WINDOW}), SMALL_WINDOW)
        self.assertLess(context_budget.budget({}, context_budget.TARGET_RATIO), context_budget.budget({}))

    def test_masking_keeps_recent_rounds_reasoning_and_job_ids(self):
        messages = [{'role': 'system', 'content': '系統'}, {'role': 'user', 'content': '找工作'}]
        for index in range(4):
            messages += tool_round(index, [f'job-{index}a', f'job-{index}b'])
        before = context_budget.estimate(messages)
        self.assertEqual(context_budget.mask_tool_results(messages), 2)
        tools = [message for message in messages if message['role'] == 'tool']
        self.assertTrue(all(message['content'].startswith(context_budget.MASKED_PREFIX) for message in tools[:2]))
        self.assertIn('job-0a、job-0b', tools[0]['content'])
        self.assertIn('search_jobs', tools[0]['content'])
        self.assertEqual([json.loads(message['content'])['items'][0]['id'] for message in tools[2:]], ['job-2a', 'job-3a'])
        self.assertTrue(all(message['tool_calls'] for message in messages if message['role'] == 'assistant'))
        self.assertLess(context_budget.estimate(messages), before)
        self.assertEqual(context_budget.mask_tool_results(messages), 0, 'masking is idempotent')
        self.assertEqual(context_budget.mask_tool_results(messages, keep_rounds=0), 2)

    def test_masking_below_trigger_keeps_history_above_target(self):
        messages = [{'role': 'system', 'content': '系統'}, {'role': 'user', 'content': '問' * 1000},
                    {'role': 'assistant', 'content': '答' * 1000}]
        messages += tool_round(1, ['old']) + tool_round(2, ['recent'])
        masked, dropped = context_budget.fit(messages, 1, 3, 3500, 1500, keep_rounds=1)
        self.assertEqual(masked, 1)
        self.assertEqual(dropped, [])
        self.assertEqual(messages[1]['content'], '問' * 1000)
        self.assertLessEqual(context_budget.estimate(messages), 3500)
        self.assertGreater(context_budget.estimate(messages), 1500)

    def test_trimming_drops_oldest_history_only(self):
        messages = [{'role': 'system', 'content': '系統'}]
        for index in range(10):
            messages += [{'role': 'user', 'content': f'Q{index} ' + '問' * 2000}, {'role': 'assistant', 'content': f'A{index} ' + '答' * 2000}]
        messages.append({'role': 'user', 'content': '現在的問題'})
        dropped = context_budget.trim_history(messages, 1, 21, 9_000)
        self.assertEqual(dropped[0]['content'][:2], 'Q0')
        self.assertEqual(messages[0]['content'], '系統'); self.assertEqual(messages[-1]['content'], '現在的問題')
        self.assertEqual(messages[1]['role'], 'user', 'history never starts with an orphaned assistant reply')
        self.assertLessEqual(context_budget.estimate(messages), 9_000)

    def test_overflow_is_a_distinct_stream_error(self):
        anthropic = decode_event('anthropic_messages', {'type': 'message_delta', 'delta': {'stop_reason': 'model_context_window_exceeded'}}, {})
        self.assertEqual(anthropic[0]['code'], 'context_overflow')
        failed = decode_event('responses', {'type': 'response.failed', 'response': {'error': {'code': 'context_length_exceeded'}}}, {})
        self.assertEqual(failed[0]['code'], 'context_overflow')
        other = decode_event('responses', {'type': 'response.failed', 'response': {'error': {'code': 'server_error'}}}, {})
        self.assertEqual(other[0]['code'], 'incomplete')


class CompactionChecks(unittest.TestCase):
    setUp = chats.ChatChecks.setUp
    tearDown = chats.ChatChecks.tearDown

    def configure(self, window=SMALL_WINDOW):
        with self.store.connect(write=True) as db:
            self.store.put(db, 'services', chats.SETTINGS | {'llm_context_window': window})

    def seed(self, pairs, size=300):
        for index in range(pairs):
            request = f'seed-{index}'
            self.store.start_chat(request, f'Q{index}：' + '問' * size)
            self.store.save_reply(request, {'text': f'A{index}：' + '答' * size, 'events': [], 'status': 'completed'})

    def provider(self, reply=lambda payload: chats.response(text='好的')):
        self.summary_calls, self.reply_calls = [], []

        def handle(request):
            payload = json.loads(request.content)
            if payload.get('instructions') == SUMMARY_SYSTEM:
                self.summary_calls.append(payload)
                return chats.response(text=SUMMARY_TEXT)
            self.reply_calls.append(payload)
            return reply(payload)

        self.app.state.llm_transport = httpx.MockTransport(handle)

    @staticmethod
    def texts(payload):
        return [item['content'] for item in payload['input'] if isinstance(item.get('content'), str)]

    def test_compact_budget_boundary_including_provider_overflow(self):
        from unittest.mock import Mock, patch
        from app import chat
        for index, (seconds, expected) in enumerate([(74.99, 0), (75, 0), (75.01, 1)]):
            # Each request gets an independent long conversation, so prior summaries cannot mask the gate.
            self.configure()
            self.seed(40)
            self.provider()
            clock = Mock(); clock.monotonic.return_value = 100
            with patch.object(chat, 'CHAT_SECONDS', seconds), patch.object(chat, 'time', clock):
                result = self.client.post('/api/chat', json={'request_id': f'compact-{index}', 'text': '繼續'})
                self.assertEqual(result.status_code, 200, result.text)
                output = chats.events(result)
            self.assertEqual(output[-1][1]['status'], 'completed', output)
            self.assertEqual(len(self.summary_calls), expected)
            # Remove only this test's synthetic conversation rows before the next boundary case.
            with self.store.connect(write=True) as db:
                db.execute('DELETE FROM messages')
        self.configure(256_000)
        self.seed(20, size=1000)
        overflow = httpx.Response(413, text='request entity too large')
        self.provider(lambda payload: overflow if len(self.reply_calls) == 1 else chats.response(text='完成'))
        with patch.object(chat, 'CHAT_SECONDS', 74.99), patch.object(chat, 'time', clock):
            output = chats.events(self.client.post('/api/chat', json={'request_id': 'overflow-low-time', 'text': '繼續'}))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        self.assertEqual(len(self.reply_calls), 2)
        self.assertEqual(self.summary_calls, [])

    def test_masking_alone_does_not_request_summary(self):
        from unittest.mock import patch
        self.configure()
        self.seed(1, size=1000)
        calls = 0
        def reply(payload):
            nonlocal calls
            calls += 1
            if calls <= 2:
                return chats.response(call=(f'web-{calls}', 'read_web_page', encode({'url': 'https://example.com'})))
            return chats.response(text='完成')
        async def read(*args):
            return {'text': '文' * 5000}
        self.app.state.web_reader.read = read
        self.provider(reply)
        fit = context_budget.fit
        def measured_fit(messages, start, end, trigger, target, tools, keep_rounds):
            import copy
            if sum(item.get('role') == 'tool' for item in messages) < 2:
                return fit(messages, start, end, 10**9, target, tools, 1)
            masked = copy.deepcopy(messages)
            context_budget.mask_tool_results(masked, 1)
            trigger = (context_budget.estimate(messages, tools) + context_budget.estimate(masked, tools)) // 2
            return fit(messages, start, end, trigger, 1, tools, 1)
        from app import chat
        from unittest.mock import Mock
        clock = Mock(); clock.monotonic.return_value = 100
        # Without F5 compact returns before measured_fit and the provider never sees MASKED_PREFIX.
        with patch('app.context_budget.fit', side_effect=measured_fit), \
                patch.object(chat, 'CHAT_SECONDS', 75), patch.object(chat, 'time', clock):
            output = chats.events(self.client.post('/api/chat', json={'request_id': 'mask-only', 'text': '讀網頁'}))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        self.assertTrue(any(context_budget.MASKED_PREFIX in json.dumps(payload, ensure_ascii=False) for payload in self.reply_calls))
        self.assertEqual(self.summary_calls, [])

    def test_long_history_is_summarised_once_and_reused(self):
        self.configure()
        self.seed(40)
        self.provider()
        output = chats.events(self.client.post('/api/chat', json={'request_id': 'turn-1', 'text': '繼續幫我找'}))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        self.assertEqual(len(self.summary_calls), 1)
        summarised = json.loads(self.summary_calls[0]['input'][0]['content'])['messages']
        texts = self.texts(self.reply_calls[0])
        self.assertEqual(texts[0], SUMMARY_PREFIX + SUMMARY_TEXT, 'the summary replaces the dropped history at the front')
        # The summary covers exactly the rows just before the first history message still sent verbatim.
        newest = int(summarised[-1]['text'][1:summarised[-1]['text'].index('：')])
        self.assertTrue(summarised[-1]['text'].startswith(f'A{newest}：'))
        self.assertTrue(texts[1].startswith(f'Q{newest + 1}：'), texts[1][:10])
        self.assertFalse(any(text.startswith('Q0：') for text in texts), 'the oldest history is no longer sent')
        self.assertTrue(any(text.startswith('A39：') for text in texts), 'the newest history is kept verbatim')
        self.assertEqual(texts[-1], '繼續幫我找')
        sent = [{'role': item.get('role', 'user'), 'content': item['content']} for item in self.reply_calls[0]['input']]
        self.assertLessEqual(context_budget.estimate(sent), context_budget.budget({'llm_context_window': SMALL_WINDOW}))
        stored = next(item for item in self.client.get('/api/messages').json()['items'] if item['request_id'] == 'turn-1')
        self.assertEqual(stored['content']['context_summary']['text'], SUMMARY_TEXT)
        # The next turn starts from the saved summary instead of asking the model again.
        output = chats.events(self.client.post('/api/chat', json={'request_id': 'turn-2', 'text': '再一個問題'}))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        self.assertEqual(len(self.summary_calls), 1)
        self.assertEqual(self.texts(self.reply_calls[1])[0], SUMMARY_PREFIX + SUMMARY_TEXT)

    def test_summary_is_still_used_after_its_rows_leave_the_loaded_history(self):
        self.configure()
        self.seed(40)
        self.provider()
        chats.events(self.client.post('/api/chat', json={'request_id': 'turn-1', 'text': '繼續幫我找'}))
        self.assertEqual(len(self.summary_calls), 1)
        # 220 short rows push both the summary's message and the rows it covers out of the last 200.
        for index in range(110):
            self.store.start_chat(f'short-{index}', f'短{index}')
            self.store.save_reply(f'short-{index}', {'text': f'好{index}', 'events': [], 'status': 'completed'})
        chats.events(self.client.post('/api/chat', json={'request_id': 'turn-2', 'text': '還記得條件嗎'}))
        self.assertEqual(len(self.summary_calls), 1)
        texts = self.texts(self.reply_calls[-1])
        self.assertEqual(texts[0], SUMMARY_PREFIX + SUMMARY_TEXT)
        self.assertFalse(any(text.startswith('A39：') for text in texts), 'rows the summary covers are not resent')
        self.assertTrue(any(text == '短109' for text in texts))

    def test_editing_a_summarised_message_drops_the_stale_summary(self):
        self.configure()
        self.seed(40)
        self.provider()
        chats.events(self.client.post('/api/chat', json={'request_id': 'turn-1', 'text': '繼續幫我找'}))
        self.assertEqual(len(self.summary_calls), 1)
        original = next(item for item in self.client.get('/api/messages').json()['items'] if item['request_id'] == 'seed-38')
        output = chats.events(self.client.post('/api/chat', json={'request_id': 'edited', 'text': 'Q38：改成只找台南',
            'conversation_id': 'legacy', 'edit_message_id': original['id'], 'edit_request_id': 'seed-38'}))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        with self.store.connect() as db:
            edited = db.execute("SELECT rowid FROM messages WHERE id=?", (original['id'],)).fetchone()[0]
            summaries = db.execute("""SELECT m.request_id, c.rowid FROM messages m
                JOIN messages c ON c.id=json_extract(m.content_json,'$.context_summary.through_id')""").fetchall()
        self.assertNotIn('turn-1', [request for request, _ in summaries], 'the summary covering the edited row is gone')
        self.assertTrue(all(through < edited for _, through in summaries), 'no remaining summary covers the edited row')
        calls = len(self.summary_calls)
        chats.events(self.client.post('/api/chat', json={'request_id': 'turn-2', 'text': '繼續'}))
        sent = self.texts(self.reply_calls[-1]) + [call['input'][0]['content'] for call in self.summary_calls[calls:]]
        self.assertTrue(any('Q38：改成只找台南' in text for text in sent), 'the corrected text reaches the model')
        self.assertFalse(any(text.startswith('Q38：' + '問') for text in sent))

    def test_short_conversation_is_sent_unchanged(self):
        self.configure()
        self.seed(3)
        self.provider()
        chats.events(self.client.post('/api/chat', json={'request_id': 'short', 'text': '你好'}))
        self.assertEqual(self.summary_calls, [])
        self.assertTrue(self.texts(self.reply_calls[0])[0].startswith('Q0：'))

    def test_failed_summary_still_replies_on_trimmed_history(self):
        self.configure()
        self.seed(40)
        self.provider()
        transport = self.app.state.llm_transport

        def handle(request):
            if json.loads(request.content).get('instructions') == SUMMARY_SYSTEM:
                return httpx.Response(500)
            return transport.handle_request(request)

        self.app.state.llm_transport = httpx.MockTransport(handle)
        output = chats.events(self.client.post('/api/chat', json={'request_id': 'no-summary', 'text': '繼續'}))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        texts = self.texts(self.reply_calls[0])
        self.assertFalse(any(text.startswith(SUMMARY_PREFIX) or text.startswith('Q0：') for text in texts))

    def test_provider_overflow_compacts_harder_and_resends_once(self):
        self.configure(256_000)
        self.seed(20, size=1000)
        overflow = httpx.Response(400, json={'error': {'code': 'context_length_exceeded', 'message': 'synthetic private detail'}})
        self.provider(lambda payload: overflow if len(self.reply_calls) == 1 else chats.response(text='精簡後完成'))
        output = chats.events(self.client.post('/api/chat', json={'request_id': 'overflow', 'text': '繼續'}))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        self.assertEqual(len(self.reply_calls), 2)
        self.assertEqual(len(self.summary_calls), 1)
        self.assertFalse(any(name == 'retry' for name, _ in output), 'overflow is not a generic retry')
        self.assertTrue(self.texts(self.reply_calls[1])[0].startswith(SUMMARY_PREFIX))
        self.assertLess(len(encode(self.reply_calls[1])), len(encode(self.reply_calls[0])))

    def test_parameter_errors_mentioning_the_window_are_not_overflow(self):
        self.configure()
        self.seed(3)
        for index, message in enumerate(('max_tokens: 300000 exceeds the model context window limit of 200000',
                                         'max_tokens: 8192 exceeds the context window of 4096',
                                         'max_tokens plus the context length must not exceed the context window')):
            with self.subTest(message=message):
                rejected = httpx.Response(400, json={'error': {'message': message}})
                self.provider(lambda payload, rejected=rejected: rejected)
                output = chats.events(self.client.post('/api/chat', json={'request_id': f'bad-parameter-{index}', 'text': '你好'}))
                error = next(data for name, data in output if name == 'error')
                self.assertEqual(error['code'], 'provider_http_error')
                self.assertEqual(len(self.reply_calls), 1, 'a parameter error is not compacted and resent')

    def test_repeated_overflow_reports_a_clear_error(self):
        self.configure(256_000)
        self.seed(20, size=1000)
        for response in (httpx.Response(400, json={'error': {'message': 'prompt is too long: 300000 tokens > 200000 maximum'}}),
                         httpx.Response(413, text='request entity too large')):
            with self.subTest(status=response.status_code):
                self.provider(lambda payload, response=response: response)
                output = chats.events(self.client.post('/api/chat', json={'request_id': f'too-long-{response.status_code}', 'text': '繼續'}))
                error = next(data for name, data in output if name == 'error')
                self.assertEqual(error['code'], 'context_overflow')
                self.assertNotIn('300000', error['message'])
                self.assertEqual(len(self.reply_calls), 2, 'one compacted resend, no generic retries')



if __name__ == '__main__':
    unittest.main()
