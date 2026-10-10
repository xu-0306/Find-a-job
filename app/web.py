"""Read one public page through Tavily Extract; never fetch user URLs locally."""
import asyncio
import ipaddress
import json

import httpx
from pydantic import Field, HttpUrl, field_validator

from .store import Input, now

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CONTENT_CHARS = 30000


class ReadInput(Input):
    url: HttpUrl = Field(max_length=2048)

    @field_validator('url')
    @classmethod
    def public_url(cls, url):
        host = url.host.strip('[]').rstrip('.').lower()
        if url.username is not None or url.password is not None or url.port != (443 if url.scheme == 'https' else 80):
            raise ValueError('只接受不含登入資料的公開 HTTP／HTTPS 網址。')
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if '.' not in host or host.endswith(('.localhost', '.local', '.internal', '.home.arpa', '.onion')):
                raise ValueError('只接受公開網站。') from None
        else:
            if not address.is_global or address.is_multicast:
                raise ValueError('只接受公開網站。')
        return url


class WebReader:
    def __init__(self, transport=None):
        self.transport = transport

    async def read(self, body, key):
        result = dict(status='failed', url=str(body.url), content='', truncated=False, limitations=[])
        if not key:
            return result | {'status': 'unsupported', 'limitations': ['網頁讀取需在後台選擇自動或 Tavily 並配置 Tavily 金鑰。']}
        try:
            async with asyncio.timeout(30):
                async with httpx.AsyncClient(transport=self.transport, timeout=25, trust_env=False, follow_redirects=False) as client:
                    async with client.stream('POST', 'https://api.tavily.com/extract',
                            headers={'Authorization': 'Bearer ' + key},
                            json={'urls': [str(body.url)], 'extract_depth': 'basic', 'format': 'markdown',
                                  'include_images': False, 'timeout': 20}) as response:
                        if response.status_code != 200:
                            return result | {'limitations': [f'網頁讀取服務回傳 HTTP {response.status_code}，未取得內容。']}
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                                return result | {'limitations': ['網頁讀取結果過大，未交給模型。']}
                            data.extend(chunk)
            payload = json.loads(data)
            rows, failures = payload['results'], payload.get('failed_results', [])
            if not isinstance(rows, list) or not isinstance(failures, list) or len(rows) > 1:
                raise ValueError()
            if not rows:
                return result | {'limitations': ['未取得可閱讀內容；頁面可能需要登入、限制存取或無法擷取。']}
            row = rows[0]
            url = str(ReadInput(url=row['url']).url)
            content = row['raw_content']
            if not isinstance(content, str) or not content.strip():
                return result | {'limitations': ['頁面沒有可閱讀的文字，未將空結果當作成功。']}
            truncated = len(content) > MAX_CONTENT_CHARS
            limitations = ['只讀取此頁公開文字，未讀取所有連結、專案原始碼或登入內容。']
            if truncated:
                limitations.append('頁面文字超過 30,000 字，僅提供前段內容。')
            if failures:
                limitations.append('服務回報部分內容讀取失敗，請依已取得內容回答。')
            return dict(status='partial' if truncated or failures else 'success', url=url,
                        content=content[:MAX_CONTENT_CHARS], truncated=truncated,
                        fetched_at=now().isoformat(), limitations=limitations)
        except (httpx.HTTPError, TimeoutError):
            return result | {'limitations': ['網頁讀取連線失敗或等候時間已到，請稍後再試。']}
        except (ValueError, TypeError, KeyError):
            return result | {'limitations': ['網頁讀取服務的結果格式不符，未取得可核對內容。']}
