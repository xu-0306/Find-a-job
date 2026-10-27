"""Bounded text extraction; raw PDF and extracted text are never saved."""
import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import unicodedata

from .attachment_text import normalize_attachment_text

MAX_BYTES = 20 * 1024 * 1024
MAX_PAGES = 20
MAX_TEXT = 30000
MAX_STREAM = 2 * 1024 * 1024
PARSE_SECONDS = 15


class ResumeError(ValueError):
    pass


def extract_pdf(data):
    from pypdf import PdfReader, apply_configuration
    if not data or len(data) > MAX_BYTES:
        raise ResumeError('PDF 大小須介於 1 byte 與 20 MiB。')
    if not data.startswith(b'%PDF-'):
        raise ResumeError('檔案不是有效的 PDF，請重新匯出履歷。')
    try:
        with apply_configuration(
            maximum_declared_stream_length=MAX_BYTES, array_based_stream_maximum_output_length=MAX_STREAM,
            zlib_maximum_output_length=MAX_STREAM, lzw_maximum_output_length=MAX_STREAM,
            run_length_maximum_output_length=MAX_STREAM, jbig2dec_binary=None,
            page_tree_maximum_entries=1000, page_tree_maximum_depth=20,
            xform_maximum_invocations_per_extraction=100,
        ):
            reader = PdfReader(BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise ResumeError('請先移除 PDF 密碼，再上傳履歷。')
            if not 1 <= len(reader.pages) <= MAX_PAGES:
                raise ResumeError('履歷 PDF 最多 20 頁，請縮短後再上傳。')
            texts = []
            total_stream = 0
            for page in reader.pages:
                contents = page.get_contents()
                length = len(contents.get_data()) if contents else 0
                total_stream += length
                if length > MAX_STREAM or total_stream > 10 * 1024 * 1024:
                    raise ResumeError('PDF 內容過於複雜，請重新匯出較精簡的履歷。')
                text = normalize_attachment_text(page.extract_text() or '')
                text = ''.join(c for c in text if c in '\n\t' or not unicodedata.category(c).startswith('C')).strip()
                texts.append(text)
                if sum(map(len, texts)) > MAX_TEXT:
                    raise ResumeError('履歷文字最多 30,000 字，請縮短後再上傳。')
            text = '\n\n'.join(texts).strip()
            if len(text) > MAX_TEXT:
                raise ResumeError('履歷文字最多 30,000 字，請縮短後再上傳。')
            if not text:
                raise ResumeError('這份 PDF 沒有可讀取的文字（可能是掃描檔）；請改附可選取文字的 PDF 或 Word，或把每頁截圖成 PNG／JPG 附上（截圖無法逐字核對原句）。')
            return {'text': text, 'pages': len(reader.pages)}
    except ResumeError:
        raise
    except Exception:
        # Parser exceptions can contain private PDF content; never return or log them.
        raise ResumeError('無法解析這份 PDF，請重新匯出可選取文字的履歷。') from None


async def parse_pdf(data):
    # A killable worker bounds parser CPU time without blocking chat or retaining a file.
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-m', 'app.resume_pdf', cwd=Path(__file__).resolve().parents[1],
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(data), timeout=PARSE_SECONDS)
        if process.returncode:
            raise ResumeError('PDF 解析未完成，請稍後再試。')
        result = json.loads(output)
        if 'error' in result:
            raise ResumeError(result['error'])
        return result
    except TimeoutError:
        raise ResumeError('PDF 解析時間過長，請重新匯出較精簡的履歷。') from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


if __name__ == '__main__':
    try:
        result = extract_pdf(sys.stdin.buffer.read(MAX_BYTES + 1))
    except ResumeError as exc:
        result = {'error': str(exc)}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
