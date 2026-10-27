"""HTTP transport and a side-effect-free capability test; no provider SDK needed."""
import codecs
import hashlib
import json
import re
import secrets
from urllib.parse import urlsplit, urlunsplit

import httpx

from .llm_wire import build_payload, decode_event
from .store import encode

# Providers report an over-long prompt with different codes and wording; these cover OpenAI-compatible,
# Anthropic and common relay responses. Admin tests may show a bounded, redacted error detail.
# Only wording that says the input itself is too long; "context window" alone also appears in max_tokens errors.
OVERFLOW_MARKERS = ('context_length_exceeded', 'model_context_window_exceeded', 'prompt is too long', 'input is too long',
                    'your messages resulted in', 'input token count', 'request_too_large')
OVERFLOW_MESSAGE = '對話內容超過模型可處理的長度，精簡後仍無法送出；請開新對話，或在後台確認上下文視窗設定。'
PROTOCOL_PATHS = {'responses': '/responses', 'chat_completions': '/chat/completions',
                  'anthropic_messages': '/messages'}
# Reasoning models may remain silent before their first token; connection failures still fail promptly.
MODEL_HTTP_TIMEOUT = httpx.Timeout(30, read=180)
MODEL_TEST_SECONDS = 600


class LLMError(Exception):
    def __init__(self, code, message, unsupported=False, retryable=False, diagnostic=''):
        self.code, self.message, self.unsupported = code, message, unsupported
        self.retryable = retryable
        self.diagnostic = diagnostic


def diagnostic_text(value, key):
    """Bound admin diagnostics and redact credentials before returning them."""
    text = str(value)
    if key:
        text = text.replace(key, '[REDACTED]')
    text = re.sub(r'(?i)(bearer\s+)[^\s,;"\']+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)((?:api[_-]?key|token|authorization|password)\s*["\']?\s*[:=]\s*["\']?)[^\s,"\';&]+', r'\1[REDACTED]', text)
    return ' '.join(text.split())[:1500]


def provider_diagnostic(event, key):
    if not isinstance(event, dict):
        return ''
    detail = event.get('error', event.get('response', {}).get('error') if isinstance(event.get('response'), dict) else None)
    if detail is None:
        detail = event
    if isinstance(detail, dict):
        detail = ' · '.join(f'{field}: {detail[field]}' for field in ('code', 'type', 'message') if isinstance(detail.get(field), (str, int)))
    return diagnostic_text(detail, key) if isinstance(detail, str) else ''


def signature(settings, key):
    return hashlib.sha256(encode([settings.get('llm_base_url'), settings.get('llm_model'),
                                 settings.get('llm_protocol'), key]).encode()).hexdigest()


def endpoint(base, protocol):
    parts = urlsplit(base)
    if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise LLMError('invalid_endpoint', '請檢查模型服務網址。')
    path = parts.path.rstrip('/')
    for suffix in PROTOCOL_PATHS.values():
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return urlunsplit((parts.scheme, parts.netloc, path + PROTOCOL_PATHS[protocol], '', ''))


async def sse_data(response):
    decoder = codecs.getincrementaldecoder('utf-8-sig')('strict')
    buffer, lines, size = '', [], 0
    async for chunk in response.aiter_bytes():
        buffer += decoder.decode(chunk)
        if len(buffer) > 1048576:
            raise ValueError('oversized stream')
        while match := re.search(r'\r\n|\r|\n', buffer):
            if match.group() == '\r' and match.end() == len(buffer):
                break
            line, buffer = buffer[:match.start()], buffer[match.end():]
            size += len(line.encode('utf-8'))
            if size > 1048576:
                raise ValueError('oversized frame')
            if not line:
                if lines:
                    yield '\n'.join(lines)
                lines, size = [], 0
            elif line.startswith('data:'):
                value = line[5:]
                lines.append(value[1:] if value.startswith(' ') else value)
    decoder.decode(b'', final=True)
    if buffer == '\r' and lines:
        yield '\n'.join(lines)
    # An unterminated frame is not a completed SSE event.


async def stream_model(client, settings, key, protocol, messages, tools, force_tool=False, no_tools=False, had_success=False, max_tokens=None):
    payload = build_payload(protocol, settings['llm_model'], messages, tools, max_tokens=max_tokens)
    if force_tool:
        payload['tool_choice'] = {'type': 'any'} if protocol == 'anthropic_messages' else 'required'
    elif no_tools:
        # Anthropic 要求含 tool_use 歷史的請求仍須定義 tools，三協定一律保留定義並以 tool_choice 禁用。
        payload['tool_choice'] = {'type': 'none'} if protocol == 'anthropic_messages' else 'none'
    headers = {'Accept': 'text/event-stream'}
    if protocol == 'anthropic_messages':
        headers.update({'x-api-key': key, 'anthropic-version': '2023-06-01'})
    else:
        headers['Authorization'] = f'Bearer {key}'
    state, completed = {}, False
    try:
        # Redirects stay disabled; the chat task owns retries on the same endpoint.
        async with client.stream('POST', endpoint(settings['llm_base_url'], protocol), json=payload, headers=headers) as response:
            if response.status_code != 200:
                status = response.status_code
                body = b''
                async for chunk in response.aiter_bytes():
                    body += chunk[:8192 - len(body)]
                    if len(body) >= 8192:
                        break
                try:
                    detail = provider_diagnostic(json.loads(body), key)
                except (ValueError, UnicodeError):
                    detail = diagnostic_text(body.decode('utf-8', 'replace'), key) if len(body) < 8192 else '錯誤回應超過診斷上限，未顯示內容。'
                diagnostic = f'HTTP {status}（{protocol}） · {detail or "供應商未提供錯誤訊息"}'
                if status in (400, 413):
                    if status == 413 or any(marker in body.decode('utf-8', 'replace').lower() for marker in OVERFLOW_MARKERS):
                        raise LLMError('context_overflow', OVERFLOW_MESSAGE, diagnostic=diagnostic)
                code = 'provider_http_error'
                message = f'模型服務回傳 HTTP {status}（{protocol}），請在後台檢查設定。'
                retryable = False
                if status == 401 or (status == 403 and not had_success):
                    code = 'authentication_failed'
                elif status == 403:
                    code = 'provider_refused'
                    message = '模型服務中途拒絕了這次請求，可能是內容或長度限制；已取得的內容保留，可以稍後重試或開新對話。'
                elif status == 429:
                    code, message, retryable = 'rate_limited', '模型服務目前請求過多，請稍後再試', True
                elif 500 <= status < 600 and status != 501:
                    code, message, retryable = 'provider_unavailable', f'模型服務暫時無法回應（HTTP {status}），請稍後再試', True
                raise LLMError(code, message, status in (404, 405, 501), retryable=retryable, diagnostic=diagnostic)
            if response.headers.get('content-type', '').split(';')[0].strip().lower() != 'text/event-stream':
                raise LLMError('invalid_stream', '模型服務未提供串流回覆。', retryable=True,
                               diagnostic=diagnostic_text(f'HTTP 200 · Content-Type: {response.headers.get("content-type", "未提供")}', key))
            async for raw in sse_data(response):
                event = raw if raw == '[DONE]' else json.loads(raw)
                for item in decode_event(protocol, event, state):
                    if item['kind'] == 'error':
                        raise LLMError(item['code'], item['message'], retryable=item['code'] in ('incomplete', 'provider_error'), diagnostic=provider_diagnostic(event, key))
                    if item['kind'] == 'done':
                        completed = True
                    yield item
                if completed:
                    return
            raise LLMError('incomplete', '連線已中斷，回覆尚未完成。', retryable=True)
    except (httpx.HTTPError, ValueError, UnicodeError, KeyError, TypeError) as exc:
        detail = diagnostic_text(f'{type(exc).__name__}: {exc}', key)
        if isinstance(exc, httpx.TimeoutException):
            code, message = 'connection_timeout', '模型連線逾時。'
        elif isinstance(exc, httpx.HTTPError):
            code, message = 'connection_failed', '無法完成模型連線。'
        else:
            code, message = 'invalid_response', '模型回覆格式無法解析。'
        raise LLMError(code, message, retryable=True, diagnostic=detail) from None


async def test_connection(settings, key, transport=None):
    if not settings.get('llm_base_url') or not settings.get('llm_model') or not key:
        raise LLMError('not_configured', '請先保存模型網址、名稱與金鑰。')
    requested = settings.get('llm_protocol', 'auto')
    protocols = list(PROTOCOL_PATHS) if requested == 'auto' else [requested]
    unsupported_errors = []
    nonce = secrets.token_hex(12)
    tools = [{'name': 'connection_probe', 'description': 'Call once with the supplied nonce to test tool support.',
              'parameters': {'type': 'object', 'properties': {'nonce': {'type': 'string', 'enum': [nonce]}},
                             'required': ['nonce'], 'additionalProperties': False}}]
    async with httpx.AsyncClient(transport=transport, timeout=MODEL_HTTP_TIMEOUT, follow_redirects=False, trust_env=False) as client:
        for protocol in protocols:
            stage = '工具呼叫'
            messages = [{'role': 'user', 'content': f'Call connection_probe with nonce {nonce}, then acknowledge its result in one short sentence.'}]
            calls, text = [], ''
            try:
                async for item in stream_model(client, settings, key, protocol, messages, tools, force_tool=True):
                    if item['kind'] == 'tool_call':
                        calls.append({key: item[key] for key in ('id', 'name', 'arguments')})
                    elif item['kind'] == 'text_delta':
                        text += item['text']
                    if len(calls) > 1 or len(text) > 10000:
                        raise LLMError('tool_test_failed', '模型未通過工具能力測試。')
                if len(calls) != 1 or calls[0]['name'] != 'connection_probe' or json.loads(calls[0]['arguments']) != {'nonce': nonce}:
                    raise LLMError('tool_test_failed', '模型未通過工具能力測試。')
                messages += [{'role': 'assistant', 'content': text, 'tool_calls': calls},
                             {'role': 'tool', 'tool_call_id': calls[0]['id'], 'content': encode({'ok': True, 'nonce': nonce})}]
                reply = ''
                stage = '工具結果後的文字回覆'
                async for item in stream_model(client, settings, key, protocol, messages, tools, had_success=True):
                    if item['kind'] == 'tool_call':
                        raise LLMError('tool_test_failed', '模型未完成工具往返。')
                    if item['kind'] == 'text_delta':
                        reply += item['text']
                    if len(reply) > 10000:
                        raise LLMError('tool_test_failed', '模型回覆超過測試上限。')
                if not reply.strip():
                    raise LLMError('reply_test_failed', '模型未提供文字回覆。')
                return {'protocol': protocol, 'reply': True, 'tools': True}
            except LLMError as exc:
                exc.diagnostic = f'{protocol} · {stage} · {exc.diagnostic or exc.message}'
                if requested == 'auto' and exc.unsupported:
                    unsupported_errors.append(exc.diagnostic)
                    continue
                raise
            except (ValueError, KeyError, TypeError) as exc:
                raise LLMError('tool_test_failed', '模型工具參數格式不符。', diagnostic=f'{protocol} · {stage} · {diagnostic_text(f"{type(exc).__name__}: {exc}", key)}') from None
    raise LLMError('unsupported_protocol', '找不到可用的連線格式，請手動指定後測試。 ' + ' '.join(unsupported_errors))
