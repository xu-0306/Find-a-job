"""Validate one transient chat attachment; only metadata enters message history."""
import asyncio
from base64 import b64encode
from io import BytesIO
import re
import unicodedata
import warnings
from xml.etree import ElementTree
from zipfile import ZipFile

from PIL import Image

from . import resume_pdf

EXTENSIONS = {'.pdf', '.docx', '.png', '.jpg', '.jpeg', '.webp', '.md', '.markdown', '.txt'}
MAX_PIXELS = 20_000_000
MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
WORD_ERROR = '無法讀取這份 Word 檔；若有設定密碼請先移除，或另存為 PDF 後再附上。'


def read_docx(data):
    try:
        with ZipFile(BytesIO(data)) as archive:
            info = archive.getinfo('word/document.xml')
            if info.file_size > MAX_DOCUMENT_BYTES:
                raise ValueError
            with archive.open(info) as document:
                xml = document.read(MAX_DOCUMENT_BYTES + 1)
            if len(xml) > MAX_DOCUMENT_BYTES:
                raise ValueError
        if any('<!DOCTYPE'.encode(encoding) in xml for encoding in ('ascii', 'utf-16-le', 'utf-16-be')):
            raise ValueError
        root = ElementTree.fromstring(xml)
        word = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        mc = '{http://schemas.openxmlformats.org/markup-compatibility/2006}'
        parts = []
        # Explicit enter/exit events keep deeply nested documents off Python's call stack.
        stack = [(root, None, False)]
        while stack:
            element, parent, closing = stack.pop()
            if closing:
                if element.tag == word + 'p':
                    parts.append('\n')
                continue
            if element.tag == mc + 'Fallback' and parent == mc + 'AlternateContent':
                continue
            if element.tag == word + 't' and parent == word + 'r':
                parts.append(element.text or '')
            elif element.tag in (word + 'tab', word + 'ptab') and parent == word + 'r':
                parts.append('\t')
            elif element.tag == word + 'noBreakHyphen' and parent == word + 'r':
                parts.append('-')
            elif element.tag in (word + 'br', word + 'cr'):
                parts.append('\n')
            stack.append((element, parent, True))
            stack.extend((child, element.tag, False) for child in reversed(element))
        text = unicodedata.normalize('NFKC', ''.join(parts))
        text = ''.join(c for c in text if c in '\n\t' or not unicodedata.category(c).startswith('C'))
        return re.sub(r'\n{3,}', '\n\n', text).strip()
    except Exception:
        raise resume_pdf.ResumeError(WORD_ERROR) from None


def text_result(text, filename, *, word=False):
    if not text.strip():
        message = ('這份 Word 檔沒有可讀取的文字；若內容是圖片，請另存為 PDF 或改附圖片。' if word
                   else '文件須含可讀文字，最多 30,000 字，請縮短後再附上。')
        raise resume_pdf.ResumeError(message)
    if len(text) > resume_pdf.MAX_TEXT:
        raise resume_pdf.ResumeError('文件須含可讀文字，最多 30,000 字，請縮短後再附上。')
    context = {'kind':'text', 'filename':filename, 'text':text}
    return context, {'kind':'text', 'filename':filename, 'characters':len(text)}


def read_image(data):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format not in ('PNG', 'JPEG', 'WEBP'):
                    raise resume_pdf.ResumeError('圖片僅支援 PNG、JPEG 或 WebP。')
                if image.width * image.height > MAX_PIXELS or max(image.size) > 8000:
                    raise resume_pdf.ResumeError('圖片最多 2,000 萬像素，單邊最多 8,000 px，請縮小後再附上。')
                if getattr(image, 'is_animated', False):
                    raise resume_pdf.ResumeError('請附上靜態圖片，暫不支援動畫圖片。')
                image.load()
                return {'kind':'image', 'media_type':Image.MIME[image.format], 'width':image.width,
                        'height':image.height, 'data':b64encode(data).decode('ascii')}
    except resume_pdf.ResumeError:
        raise
    except Exception:
        raise resume_pdf.ResumeError('無法讀取這張圖片，請重新匯出 PNG、JPEG 或 WebP。') from None


async def prepare(upload):
    data, extension = upload['data'], upload['extension']
    if extension == '.pdf':
        context = await resume_pdf.parse_pdf(data)
        metadata = {'kind':'resume_pdf', 'filename':upload['filename'], 'pages':context['pages']}
        return context, metadata
    if extension in ('.png', '.jpg', '.jpeg', '.webp'):
        context = await asyncio.to_thread(read_image, data)
        context['filename'] = upload['filename']
        return context, {key:context[key] for key in ('kind','filename','width','height')}
    if extension == '.docx':
        text = await asyncio.to_thread(read_docx, data)
        return text_result(text, upload['filename'], word=True)
    try:
        text = unicodedata.normalize('NFKC', data.decode('utf-8-sig'))
    except UnicodeError:
        raise resume_pdf.ResumeError('Markdown／文字檔須使用 UTF-8 編碼，請重新另存後附上。') from None
    if any(unicodedata.category(c) == 'Cc' and c not in '\n\r\t' for c in text):
        raise resume_pdf.ResumeError('文件含有二進位或無法讀取的內容，請附上 UTF-8 文字檔。')
    return text_result(text, upload['filename'])
