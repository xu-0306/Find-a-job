"""Synthetic providers through production routes; never reads real data or keys."""
import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from fastapi.testclient import TestClient

from app import chat, llm, security
from app.main import create_app
from app.store import Job, Store, encode, now

KEY = 'synthetic-provider-secret'
SETTINGS = {'llm_base_url': 'https://provider.invalid/v1', 'llm_model': 'synthetic', 'llm_protocol': 'responses'}
CRITERIA = {'keyword': '任意測試條件', 'area_codes': [], 'min_monthly_salary': None}


def stream_bytes(protocol, text='', call=None, complete=True):
    events = []
    if protocol == 'responses':
        if text:
            events.append({'type': 'response.output_text.delta', 'delta': text})
        if call:
            ident, name, args = call
            events += [{'type': 'response.output_item.added', 'output_index': 0, 'item':
                        {'type': 'function_call', 'id': 'item-'+ident, 'call_id': ident, 'name': name, 'arguments': ''}},
                       {'type': 'response.function_call_arguments.done', 'output_index': 0,
                        'item_id': 'item-'+ident, 'arguments': args}]
        if complete:
            events.append({'type': 'response.completed', 'response': {'status': 'completed'}})
    elif protocol == 'chat_completions':
        if text:
            events.append({'choices': [{'delta': {'content': text}}]})
        if call:
            ident, name, args = call
            events += [{'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': ident, 'function': {'name': name, 'arguments': args}}]}}]},
                       {'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]}]
    else:
        if text:
            events.append({'type': 'content_block_delta', 'index': 1, 'delta': {'type': 'text_delta', 'text': text}})
        if call:
            ident, name, args = call
            events += [{'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'tool_use', 'id': ident, 'name': name, 'input': {}}},
                       {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'input_json_delta', 'partial_json': args}},
                       {'type': 'content_block_stop', 'index': 0}]
        if complete:
            events.append({'type': 'message_stop'})
    result = ''.join('data: '+encode(event)+'\r\n\r\n' for event in events)
    if protocol == 'chat_completions' and complete:
        result += 'data: [DONE]\r\n\r\n'
    return result.encode()


def response(protocol='responses', **values):
    return httpx.Response(200, headers={'Content-Type': 'text/event-stream'}, content=stream_bytes(protocol, **values))


def events(response):
    return [(frame.splitlines()[0][7:], json.loads(frame.splitlines()[1][6:]))
            for frame in response.text.split('\n\n') if frame]


class HangingStream(httpx.AsyncByteStream):
    def __init__(self, started, text='部分內容'):
        self.started, self.text, self.closed = started, text, False

    async def __aiter__(self):
        yield stream_bytes('responses', text=self.text, complete=False)
        self.started.set()
        await asyncio.sleep(1000)

    async def aclose(self):
        self.closed = True


class ChatChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(self.directory.name, automatic_daily=False)
        self.store = self.app.state.store
        self.user_token, self.admin_token = 'u'*43, 'a'*43
        with self.store.connect(write=True) as db:
            for role, token in [('user', self.user_token), ('admin', self.admin_token)]:
                db.execute('INSERT INTO sessions VALUES(?,?,?,?,?)',
                           (security.digest(token), role, (now()+timedelta(days=1)).isoformat(), 'test-device', 'default' if role == 'user' else None))
            self.store.put(db, 'services', SETTINGS)
            self.store.put(db, 'llm_api_key', KEY)
        self.client = TestClient(self.app, base_url='http://localhost', client=('127.0.0.1', 1))
        self.client.__enter__()
        self.client.cookies.set('jc_user', self.user_token)
        self.client.cookies.set('jc_admin', self.admin_token)
        self.client.headers.update({'Origin': 'http://localhost', 'X-CSRF-Token': security.csrf(self.user_token)})

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def send(self, ident='one', text='不同語言也可聊天 hello'):
        return self.client.post('/api/chat', json={'request_id': ident, 'text': text})

    def admin_test(self):
        return self.client.post('/api/admin/llm-test', headers={'X-CSRF-Token': security.csrf(self.admin_token)})

    def history(self):
        return self.client.get('/api/messages').json()['items']

    def test_all_three_protocols_roundtrip_and_confirm_only_by_user(self):
        for protocol in llm.PROTOCOL_PATHS:
            with self.subTest(protocol=protocol):
                with self.store.connect(write=True) as db:
                    self.store.put(db, 'services', SETTINGS | {'llm_protocol': protocol})
                requests = []
                def provider(request):
                    payload = json.loads(request.content)
                    requests.append(payload)
                    if len(requests) == 1:
                        names = [tool.get('name') or tool['function']['name'] for tool in payload['tools']]
                        self.assertEqual(set(names), {'list_preferences','propose_preference_change','search_jobs','get_job_details','get_company_reviews','read_web_page','lookup_job_areas','present_job_recommendations','submit_resume_review'})
                        return response(protocol, text='先提出，等待你決定。', call=('c1', 'propose_preference_change', encode({'operation': 'create', 'after': CRITERIA})))
                    self.assertIn('pending', encode(payload))
                    return response(protocol, text='提案完成，尚未生效。')
                self.app.state.llm_transport = httpx.MockTransport(provider)
                result = self.send(protocol)
                self.assertEqual(result.status_code, 200)
                output = events(result)
                self.assertEqual(output[-1][1]['status'], 'completed')
                proposal = next(value for name, value in output if name == 'preference_proposal')
                preferences = self.client.get('/api/preferences').json()
                self.assertFalse(preferences['items'])
                self.assertFalse(preferences['user_has_chosen'])
                self.assertEqual(self.send(protocol).status_code, 409)
                self.assertEqual(len(requests), 2)
                confirmed = self.client.post(f"/api/preference-proposals/{proposal['id']}/confirm")
                self.assertEqual(confirmed.status_code, 200)
                with self.store.connect(write=True) as db:
                    db.execute('DELETE FROM preferences')
                    db.execute("UPDATE users SET has_chosen=0 WHERE id='default'")

    def test_invalid_tool_batch_stops_without_proposal_or_secret_echo(self):
        for ident, name, args in [('shell','shell','{}'), ('confirm','confirm_preference_change','{}'),
                                  ('source','search_jobs','{}'), ('bad','propose_preference_change','{broken}'),
                                  ('extra','list_preferences','{"api_key":"secret"}')]:
            self.app.state.llm_transport = httpx.MockTransport(lambda request: response(call=(ident,name,args)))
            output = events(self.send(ident))
            self.assertEqual(output[-1][1]['status'], 'interrupted')
            self.assertIn(('error'), [item[0] for item in output])
            self.assertNotIn(KEY, encode(output))
            self.assertFalse(self.client.get('/api/preferences').json()['proposals'])

    def test_eof_and_error_preserve_partial_history_and_release_slot(self):
        self.app.state.llm_transport = httpx.MockTransport(lambda request: response(text='保留 <script>任意文字</script>', complete=False))
        first = self.send()
        self.assertEqual(events(first)[-1][1]['status'], 'interrupted')
        self.assertIn('保留', self.history()[-1]['content']['text'])
        self.app.state.llm_transport = httpx.MockTransport(lambda request: httpx.Response(401, text=KEY))
        result = self.send('next')
        self.assertNotIn(KEY, result.text)
        self.assertEqual(self.app.state.active_chats, {})

    def test_cancel_busy_duplicate_and_provider_close(self):
        started = threading.Event()
        source = HangingStream(started)
        self.app.state.llm_transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=source))
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(self.send)
            self.assertTrue(started.wait(5))
            self.assertEqual(self.send().json()['error']['code'], 'duplicate_request')
            self.assertEqual(self.send('different').json()['error']['code'], 'chat_busy')
            cancelled = self.client.post('/api/chat/one/cancel')
            self.assertEqual(cancelled.json()['status'], 'interrupted')
            self.assertEqual(events(pending.result(5))[-1][1]['status'], 'interrupted')
        self.assertTrue(source.closed)
        self.assertEqual(self.app.state.active_chats, {})
        self.assertEqual(self.history()[-1]['content']['text'], '部分內容')
        self.assertEqual(len(self.history()), 2)

    def test_deadline_includes_stream_wait_and_closes_transport(self):
        source = HangingStream(threading.Event())
        self.app.state.llm_transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=source))
        with patch.object(chat, 'CHAT_SECONDS', 0.05):
            output = events(self.send())
        self.assertEqual(next(value['code'] for name,value in output if name=='error'), 'timeout')
        self.assertTrue(source.closed)
        self.assertEqual(self.history()[-1]['content']['status'], 'interrupted')

    def test_time_reserve_uses_last_round_to_finish_from_collected_data(self):
        requests = []
        clock = Mock(); clock.monotonic.return_value = 100
        async def provider(request):
            payload = json.loads(request.content); requests.append(payload)
            if len(requests) == 1:
                clock.monotonic.return_value = 100.2
                return response(call=('collected', 'list_preferences', '{}'))
            return response(text='根據已取得的資料完成整理，尚未查到其他地區。')
        self.app.state.llm_transport = httpx.MockTransport(provider)
        with patch.object(chat, 'CHAT_SECONDS', 2), patch.object(chat, 'FINISH_RESERVE_SECONDS', 1.9), \
                patch.object(chat, 'TOOL_MODEL_ALLOWANCE_SECONDS', 0), patch.object(chat, 'time', clock):
            output = events(self.send('finish-time'))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        self.assertEqual(len(requests), 2)
        self.assertNotIn('tool_choice', requests[0])
        self.assertEqual(requests[1]['tool_choice'], 'none')
        self.assertIn('時間將到', encode(requests[1]))
        self.assertIn('已取得', self.history()[-1]['content']['text'])
        self.assertFalse(any(event == 'error' for event, _ in output))

    def test_time_finish_tool_request_has_distinct_error_and_no_effect(self):
        self.app.state.llm_transport = httpx.MockTransport(lambda request: response(call=('late', 'propose_preference_change',
            encode({'operation': 'create', 'after': CRITERIA}))))
        with patch.object(chat, 'CHAT_SECONDS', 2), patch.object(chat, 'FINISH_RESERVE_SECONDS', 3):
            output = events(self.send('late-tool'))
        error = next(value for event, value in output if event == 'error')
        self.assertEqual(error['code'], 'time_limit')
        self.assertIn('時間', error['message'])
        self.assertNotIn('往返', error['message'])
        self.assertFalse(self.client.get('/api/preferences').json()['proposals'])

    def test_mid_turn_forbidden_is_not_a_configuration_error(self):
        calls = []
        def provider(request):
            calls.append(request)
            return response(text='已取得資料。', call=('ok', 'list_preferences', '{}')) if len(calls) == 1 else httpx.Response(403)
        self.app.state.llm_transport = httpx.MockTransport(provider)
        output = events(self.send('refused'))
        error = next(value for event, value in output if event == 'error')
        self.assertEqual(error['code'], 'provider_refused')
        self.assertFalse(error['retryable'])
        self.assertNotIn('請在後台檢查設定', error['message'])
        self.assertIn('已取得資料', self.history()[-1]['content']['text'])
        self.assertEqual(len(calls), 2)

    def test_unavailable_retry_stops_before_reserved_time(self):
        calls = []
        clock = Mock(); clock.monotonic.return_value = 100
        async def provider(request):
            calls.append(request)
            clock.monotonic.return_value = 100.2
            return httpx.Response(503, json={'error': {'type': 'service_unavailable', 'message': KEY}})
        self.app.state.llm_transport = httpx.MockTransport(provider)
        with patch.object(chat, 'CHAT_SECONDS', 2), patch.object(chat, 'FINISH_RESERVE_SECONDS', 1.9), \
                patch.object(chat, 'RETRY_DELAY_SECONDS', .01), patch.object(chat, 'time', clock):
            output = events(self.send('busy-time'))
        error = next(value for event, value in output if event == 'error')
        self.assertEqual(error['code'], 'provider_unavailable')
        self.assertTrue(error['retryable'])
        self.assertNotIn('請在後台檢查設定', error['message'])
        self.assertEqual(len(calls), 1)
        self.assertFalse(any(event == 'retry' for event, _ in output))
        self.assertNotIn(KEY, encode(output))

    def test_finish_round_retries_busy_rate_limit_empty_and_interrupted_stream(self):
        for index, failure in enumerate([httpx.Response(503), httpx.Response(429), response(), response(text='片段', complete=False)]):
            requests = []
            def provider(request):
                requests.append(json.loads(request.content))
                return failure if len(requests) == 1 else response(text='收尾完成')
            self.app.state.llm_transport = httpx.MockTransport(provider)
            with patch.object(chat, 'CHAT_SECONDS', 40), patch.object(chat, 'RETRY_DELAY_SECONDS', 0):
                output = events(self.send(f'finish-retry-{index}'))
            self.assertEqual(output[-1][1]['status'], 'completed', output)
            self.assertEqual(len(requests), 2)
            self.assertTrue(all(item['tool_choice'] == 'none' for item in requests))
            self.assertEqual(sum(name == 'retry' for name, _ in output), 1)
            self.assertEqual(self.history()[-1]['content']['text'], '收尾完成')

    def test_tool_retry_switches_to_finish_when_full_tool_retry_does_not_fit(self):
        requests = []
        clock = Mock(); clock.monotonic.return_value = 100
        def provider(request):
            requests.append(json.loads(request.content))
            # Initial remaining=70 permits tools; the failed request uses 4s.
            clock.monotonic.return_value = 104
            return httpx.Response(503) if len(requests) == 1 else response(text='用既有資料收尾')
        self.app.state.llm_transport = httpx.MockTransport(provider)
        async def no_wait(_): pass
        # Patch only the retry wait; fake monotonic leaves 66s on the next iteration,
        # proving the error path itself selects finishing rather than an elapsed sleep.
        with patch.object(chat, 'CHAT_SECONDS', 70), patch.object(chat, 'time', clock), \
                patch('app.chat.asyncio.sleep', side_effect=no_wait):
            output = events(self.send('retry-to-finish'))
        self.assertEqual(output[-1][1]['status'], 'completed', output)
        self.assertEqual(len(requests), 2)
        self.assertNotIn('tool_choice', requests[0])
        self.assertEqual(requests[1]['tool_choice'], 'none')
        self.assertIn('時間將到', encode(requests[1]))

    def test_finish_retry_stream_budget_boundary_and_retry_cap(self):
        for index, (remaining, expected_calls) in enumerate([(15, 1), (15.01, 2)]):
            requests = []
            def provider(request):
                requests.append(json.loads(request.content))
                return httpx.Response(503) if len(requests) == 1 else response(text='完成')
            self.app.state.llm_transport = httpx.MockTransport(provider)
            async def no_wait(_): pass
            clock = Mock(); clock.monotonic.return_value = 100
            with patch.object(chat, 'CHAT_SECONDS', remaining), patch.object(chat, 'time', clock), \
                    patch('app.chat.asyncio.sleep', side_effect=no_wait):
                result = self.send(f'boundary-{index}')
                self.assertEqual(result.status_code, 200, result.text)
                output = events(result)
            self.assertEqual(len(requests), expected_calls)
            self.assertEqual(output[-1][1]['status'], 'completed' if expected_calls == 2 else 'interrupted')
        self.app.state.llm_transport = httpx.MockTransport(lambda request: httpx.Response(503))
        with patch.object(chat, 'CHAT_SECONDS', 40), patch.object(chat, 'RETRY_DELAY_SECONDS', 0), \
                patch.object(chat, 'MAX_RETRIES', 2):
            output = events(self.send('retry-cap'))
        self.assertEqual(sum(name == 'retry' for name, _ in output), 2)
        self.assertEqual(next(data['code'] for name, data in output if name == 'error'), 'provider_unavailable')

    def test_tool_entry_budget_boundary(self):
        for index, (seconds, permits_tools) in enumerate([(64.99, False), (65, False), (65.01, True)]):
            requests = []
            def provider(request):
                requests.append(json.loads(request.content)); return response(text='完成')
            self.app.state.llm_transport = httpx.MockTransport(provider)
            clock = Mock(); clock.monotonic.return_value = 100
            with patch.object(chat, 'CHAT_SECONDS', seconds), patch.object(chat, 'time', clock):
                result = self.send(f'tool-entry-{index}')
                self.assertEqual(result.status_code, 200, result.text)
                output = events(result)
            self.assertEqual(output[-1][1]['status'], 'completed')
            self.assertEqual('tool_choice' not in requests[0], permits_tools)

    def test_round_limit_and_stale_revision_never_change_preferences(self):
        count = 0
        def provider(request):
            nonlocal count
            count += 1
            return response(call=(str(count), 'list_preferences', '{}'))
        self.app.state.llm_transport = httpx.MockTransport(provider)
        output = events(self.send())
        self.assertEqual(count, 8)
        self.assertEqual(next(value['code'] for name,value in output if name=='error'), 'round_limit')
        self.app.state.llm_transport = httpx.MockTransport(lambda request: response(call=('c', 'propose_preference_change', encode({'operation':'update','preference_id':'missing','base_revision':1,'after':CRITERIA}))))
        self.assertEqual(events(self.send('stale'))[-1][1]['status'], 'interrupted')
        self.assertFalse(self.client.get('/api/preferences').json()['items'])

    def test_last_round_forbids_tools_and_model_answers_from_collected_data(self):
        for protocol in llm.PROTOCOL_PATHS:
            with self.subTest(protocol=protocol):
                with self.store.connect(write=True) as db:
                    self.store.put(db, 'services', SETTINGS | {'llm_protocol': protocol})
                requests = []
                def provider(request):
                    payload = json.loads(request.content)
                    requests.append(payload)
                    if len(requests) < chat.MAX_ROUNDS:
                        return response(protocol, call=(f'{protocol}{len(requests)}', 'list_preferences', '{}'))
                    return response(protocol, text='根據已取得的資料：尚未查到其他職缺。')
                self.app.state.llm_transport = httpx.MockTransport(provider)
                output = events(self.send(protocol))
                self.assertEqual(output[-1][1]['status'], 'completed')
                self.assertEqual(len(requests), chat.MAX_ROUNDS)
                self.assertNotIn('error', [name for name, _ in output])
                self.assertTrue(all('tool_choice' not in p for p in requests[:-1]))
                self.assertEqual(requests[-1]['tool_choice'], {'type': 'none'} if protocol == 'anthropic_messages' else 'none')
                self.assertIn('工具使用次數已達本輪上限', encode(requests[-1]))
                self.assertNotIn('工具使用次數已達本輪上限', encode(requests[-2]))
                self.assertIn('尚未查到', self.history()[-1]['content']['text'])

    def test_last_round_tool_call_still_ends_with_round_limit_without_running_it(self):
        requests = []
        def provider(request):
            requests.append(json.loads(request.content))
            return response(call=(str(len(requests)), 'propose_preference_change', encode({'operation': 'create', 'after': CRITERIA})) if len(requests) == chat.MAX_ROUNDS else (str(len(requests)), 'list_preferences', '{}'))
        self.app.state.llm_transport = httpx.MockTransport(provider)
        output = events(self.send())
        self.assertEqual(next(value['code'] for name, value in output if name == 'error'), 'round_limit')
        self.assertEqual(requests[-1]['tool_choice'], 'none')
        self.assertFalse(self.client.get('/api/preferences').json()['proposals'])

    def test_routes_enforce_roles_csrf_and_input_before_network(self):
        requests = []
        self.app.state.llm_transport = httpx.MockTransport(lambda request: requests.append(request))
        with TestClient(self.app,base_url='http://localhost',client=('127.0.0.1',2)) as anonymous:
            self.assertEqual(anonymous.get('/api/messages').status_code,401)
            self.assertEqual(anonymous.post('/api/chat',json={'text':'x','request_id':'x'},headers={'Origin':'http://localhost'}).status_code,401)
            anonymous.cookies.set('jc_admin',self.admin_token)
            self.assertEqual(anonymous.get('/api/messages').status_code,401)
            self.assertEqual(anonymous.post('/api/chat',json={'text':'x','request_id':'x'},headers={'Origin':'http://localhost'}).status_code,401)
        self.assertEqual(self.send('bad id').status_code,422)
        self.assertEqual(self.send('empty',text='  ').status_code,422)
        self.assertEqual(self.client.post('/api/chat',json={'text':'x','request_id':'x'},headers={'X-CSRF-Token':'wrong'}).status_code,403)
        self.assertEqual(self.client.post('/api/chat/x/cancel',headers={'Origin':'https://foreign.invalid'}).status_code,403)
        self.assertEqual(self.client.post('/api/admin/llm-test').status_code,403)
        self.assertFalse(requests)

    def test_auto_probe_fallback_only_on_unsupported_endpoint_and_tool_return(self):
        with self.store.connect(write=True) as db:
            self.store.put(db,'services',SETTINGS | {'llm_protocol':'auto'})
        self.assertEqual(self.send().json()['error']['code'],'test_required')
        for status in (401,402,403,418,429,500,503,529,307):
            requests=[]
            def refused(request):
                requests.append(request)
                return httpx.Response(status,headers={'location':'https://evil.invalid'},text=KEY)
            self.app.state.llm_transport=httpx.MockTransport(refused)
            result=self.admin_test()
            self.assertEqual(result.status_code,503)
            if status == 429:
                expected = ('rate_limited', '模型服務目前請求過多，請稍後再試')
            elif 500 <= status < 600:
                expected = ('provider_unavailable', f'模型服務暫時無法回應（HTTP {status}），請稍後再試')
            else:
                expected = ('authentication_failed' if status in (401, 403) else 'provider_http_error',
                            f'模型服務回傳 HTTP {status}（responses），請在後台檢查設定。')
            self.assertEqual((result.json()['error']['code'], result.json()['error']['message']), expected)
            self.assertEqual(len(requests),1)
            self.assertNotIn(KEY,result.text)
        requests=[]
        def probe(request):
            requests.append(request.url.path)
            payload=json.loads(request.content)
            if request.url.path.endswith('/responses'):
                return httpx.Response(404)
            if len(requests)==2:
                function=payload['tools'][0]['function']
                nonce=function['parameters']['properties']['nonce']['enum'][0]
                self.assertEqual(function['name'],'connection_probe')
                return response('chat_completions',call=('probe','connection_probe',encode({'nonce':nonce})))
            return response('chat_completions',text='已確認工具結果。')
        self.app.state.llm_transport=httpx.MockTransport(probe)
        result=self.admin_test()
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json(),{'protocol':'chat_completions','reply':True,'tools':True})
        self.assertEqual(requests,['/v1/responses','/v1/chat/completions','/v1/chat/completions'])
        self.assertFalse(self.history())
        settings=self.client.get('/api/admin/settings').json()
        self.assertEqual(settings['connection_test_status'],'passed')
        self.assertNotIn('signature',result.text)
        with self.store.connect(write=True) as db:
            self.store.put(db,'llm_api_key','changed-key')
        self.assertEqual(self.send().json()['error']['code'],'test_required')

    def test_model_http_diagnostics_for_each_protocol_and_exhausted_auto_detection(self):
        for protocol in llm.PROTOCOL_PATHS:
            with self.subTest(protocol=protocol):
                with self.store.connect(write=True) as db:
                    self.store.put(db, 'services', SETTINGS | {'llm_protocol': protocol})
                self.app.state.llm_transport = httpx.MockTransport(lambda request: httpx.Response(
                    402, json={'error': {'message': KEY}}, headers={'x-secret': KEY}))
                result = self.admin_test()
                self.assertEqual(result.status_code, 503)
                self.assertIn(f'HTTP 402（{protocol}）', result.json()['error']['message'])
                self.assertNotIn(KEY, result.text)

        with self.store.connect(write=True) as db:
            self.store.put(db, 'services', SETTINGS | {'llm_protocol': 'auto'})
        statuses = [404, 405, 501]
        requests = []
        def unsupported(request):
            requests.append(request.url.path)
            return httpx.Response(statuses[len(requests) - 1], text=KEY)
        self.app.state.llm_transport = httpx.MockTransport(unsupported)
        result = self.admin_test()
        self.assertEqual(result.json()['error']['code'], 'unsupported_protocol')
        for protocol, status in zip(llm.PROTOCOL_PATHS, statuses):
            self.assertIn(f'HTTP {status}（{protocol}）', result.json()['error']['message'])
        self.assertEqual(len(requests), 3)
        self.assertNotIn(KEY, result.text)
        self.assertFalse(self.history())

    def test_plain_reply_is_not_tool_capability_success(self):
        self.app.state.llm_transport=httpx.MockTransport(lambda request: response(text='Hello'))
        result=self.admin_test()
        self.assertEqual(result.status_code,503)
        self.assertEqual(result.json()['error']['code'],'tool_test_failed')
        self.assertEqual(self.client.get('/api/admin/settings').json()['connection_test_status'],'pending')

    def test_provider_bom_bare_cr_and_split_unicode_preserve_reply(self):
        class Fragmented(httpx.AsyncByteStream):
            async def __aiter__(self):
                raw=b'\xef\xbb\xbf'+stream_bytes('responses',text='任意文字😀').replace(b'\r\n',b'\r')
                for value in raw:
                    yield bytes([value])
        self.app.state.llm_transport=httpx.MockTransport(lambda request: httpx.Response(200,headers={'Content-Type':'Text/Event-Stream; charset=utf-8'},stream=Fragmented()))
        output=events(self.send())
        self.assertEqual(output[-1][1]['status'],'completed')
        self.assertEqual(self.history()[-1]['content']['text'],'任意文字😀')

    def test_history_cursor_restart_and_failed_claim_rollback(self):
        self.app.state.llm_transport=httpx.MockTransport(lambda request: response(text='完成'))
        self.send('first')
        page=self.client.get('/api/messages?limit=1').json()
        seen=[page['items'][0]['id']]
        while page['next_cursor']:
            page=self.client.get('/api/messages',params={'limit':1,'cursor':page['next_cursor']}).json()
            seen.extend(item['id'] for item in page['items'])
        self.assertEqual(len(set(seen)),2)
        self.assertEqual(self.client.get('/api/messages?cursor=bad').status_code,422)
        self.store.start_chat('orphan','尚未完成')
        restarted=create_app(self.directory.name, automatic_daily=False)
        with TestClient(restarted,base_url='http://localhost',client=('127.0.0.1',2)) as client:
            client.cookies.set('jc_user',self.user_token)
            data=client.get('/api/messages').json()
            self.assertEqual(data['items'][-1]['content']['status'],'interrupted')
        with self.store.connect(write=True) as db:
            db.execute("CREATE TRIGGER fail_assistant BEFORE INSERT ON messages WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT,'test fault'); END")
        self.assertEqual(self.send('rollback').status_code,503)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages WHERE request_id='rollback'").fetchone()[0],0)

    def test_v1_migration_preserves_data_and_rejects_future_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            old=Store(directory,target_version=5)
            job_id=old.save_job(Job(source='104',source_job_id='original',title='既有職缺',company_name='原公司',url='https://job.invalid/original'))
            with old.connect(write=True) as db:
                old.put(db,'existing',{'kept':True})
                old.put(db,'llm_api_key','old-synthetic-key')
                old.put(db,'admin_password_hash',security.password_hash('old-password'))
                db.execute('INSERT INTO preferences VALUES(?,1,?,1)',('p',encode(CRITERIA)))
                db.execute('INSERT INTO favorites VALUES(?,?)',(job_id,now().isoformat()))
                db.execute("INSERT INTO preference_proposals VALUES(?,'delete',?,1,?,NULL,'pending')",('proposal','p',encode(CRITERIA)))
                # The v1 schema is the same M1 tables without messages.
                db.execute('DROP TABLE messages')
                for table in ('conversations','seen_jobs','preference_baselines','daily_jobs','runs',
                              'push_attempts','push_subscriptions','sprites','quotes'):
                    db.execute('DROP TABLE '+table)
                db.execute('PRAGMA user_version=1')
                # Accounts add user_id later; the original columns must survive unchanged.
                tables={'settings':'*','preferences':'id,revision,criteria_json,enabled','jobs':'*',
                        'favorites':'job_id,created_at',
                        'preference_proposals':'id,operation,preference_id,base_revision,before_json,after_json,status'}
                before={table:[tuple(row) for row in db.execute(f'SELECT {columns} FROM {table}')] for table,columns in tables.items()}
            store=Store(directory)
            with store.connect() as db:
                after={table:[tuple(row) for row in db.execute(f'SELECT {columns} FROM {table}')] for table,columns in tables.items()}
                self.assertEqual(before,after)
                self.assertEqual(store.get(db,'existing'),{'kept':True})
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],7)
                # Everything that existed belongs to the built-in account.
                self.assertEqual(db.execute("SELECT DISTINCT user_id FROM favorites").fetchall()[0][0],'default')
                self.assertEqual(db.execute("SELECT DISTINCT user_id FROM preferences").fetchall()[0][0],'default')
                db.execute('PRAGMA user_version=999')
            with self.assertRaises(RuntimeError):
                Store(directory)


if __name__ == '__main__':
    unittest.main()
