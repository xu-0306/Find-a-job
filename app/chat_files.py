"""Validate one transient chat attachment; only metadata enters message history."""
import asyncio
from base64 import b64encode
from io import BytesIO
import unicodedata
import warnings

from PIL import Image

from . import resume_pdf

EXTENSIONS = {'.pdf', '.png', '.jpg', '.jpeg', '.webp', '.md', '.markdown', '.txt'}
MAX_PIXELS = 20_000_000


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
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        raise resume_pdf.ResumeError('Markdown／文字檔須使用 UTF-8 編碼，請重新另存後附上。') from None
    if any(unicodedata.category(c) == 'Cc' and c not in '\n\r\t' for c in text):
        raise resume_pdf.ResumeError('文件含有二進位或無法讀取的內容，請附上 UTF-8 文字檔。')
    if not text.strip() or len(text) > resume_pdf.MAX_TEXT:
        raise resume_pdf.ResumeError('文件須含可讀文字，最多 30,000 字，請縮短後再附上。')
    context = {'kind':'text', 'filename':upload['filename'], 'text':text}
    return context, {'kind':'text', 'filename':upload['filename'], 'characters':len(text)}
