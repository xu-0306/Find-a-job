"""Transient synthetic attachments through production chat and wire formats."""
import asyncio
from base64 import b64encode
from io import BytesIO
import json
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from app import chat_files, resume_pdf
from app.store import encode
import test_m2_chat as chats


def image_bytes(format='PNG'):
    output = BytesIO()
    Image.new('RGB',(16,12),'blue').save(output,format=format)
    return output.getvalue()


class ChatFileChecks(unittest.TestCase):
    setUp = chats.ChatChecks.setUp
    tearDown = chats.ChatChecks.tearDown

    def send(self,name,data,ident='file',**options):
        return self.client.post('/api/chat',data={'text':'請讀這份合成附件','request_id':ident,**options},
                                files={'resume':(name,data,'application/octet-stream')})

    def test_images_reach_all_protocols_with_tool_rounds_and_are_not_retained(self):
        for protocol,format,extension,mime in [('responses','PNG','png','image/png'),
                                               ('chat_completions','JPEG','jpg','image/jpeg'),
                                               ('anthropic_messages','WEBP','webp','image/webp')]:
            with self.subTest(protocol=protocol):
                with self.store.connect(write=True) as db:
                    self.store.put(db,'services',chats.SETTINGS | {'llm_protocol':protocol})
                raw=image_bytes(format); encoded=b64encode(raw).decode('ascii'); calls=[]
                conversation=self.client.post('/api/conversations',json={}).json()['id']
                def provider(request):
                    payload=json.loads(request.content); calls.append(payload)
                    if len(calls)<=2:
                        self.assertIn(encoded,encode(payload))
                        contents=payload['input' if protocol=='responses' else 'messages']
                        block=next(item for item in contents if isinstance(item.get('content'),list))['content'][0]
                        if protocol=='responses': self.assertEqual(block['image_url'],'data:'+mime+';base64,'+encoded)
                        elif protocol=='chat_completions': self.assertEqual(block['image_url']['url'],'data:'+mime+';base64,'+encoded)
                        else: self.assertEqual(block['source'],{'type':'base64','media_type':mime,'data':encoded})
                    else: self.assertNotIn(encoded,encode(payload))
                    if len(calls)==1: return chats.response(protocol,call=('list','list_preferences','{}'))
                    return chats.response(protocol,text='合成圖片已讀取')
                self.app.state.llm_transport=httpx.MockTransport(provider)
                result=self.send('C:\\synthetic\\test.'+extension,raw,ident=protocol,conversation_id=conversation)
                self.assertEqual(result.status_code,200,result.text[:200])
                self.assertEqual(chats.events(result)[-1][1]['status'],'completed')
                history=self.client.get('/api/messages',params={'conversation_id':conversation}).json()['items']
                self.assertEqual(history[0]['content']['attachments'],[{'kind':'image','filename':'test.'+extension,'width':16,'height':12}])
                self.assertNotIn(encoded,encode(history))
                self.assertEqual(self.client.post('/api/chat',json={'text':'繼續摘要','request_id':protocol+'-next','conversation_id':conversation}).status_code,200)
                self.assertEqual(len(calls),3)

    def test_utf8_markdown_txt_and_bom_reach_model_without_retaining_contents(self):
        for index,(filename,data) in enumerate([('skills.md','# 合成技能\n- GIS\n<script>test</script>'.encode()),
                                              ('skills.markdown',b'\xef\xbb\xbf'+ '合成條件'.encode()),
                                              ('skills.txt','合成經驗\r\n環境監測'.encode())]):
            conversation=self.client.post('/api/conversations',json={}).json()['id']
            private=data.decode('utf-8-sig'); calls=[]
            def provider(request):
                payload=json.loads(request.content); calls.append(payload)
                attachment_texts=[json.loads(item['content'].split('\n',1)[1])['text'] for item in payload['input']
                                  if isinstance(item.get('content'),str) and item['content'].startswith('以下 JSON')]
                if len(calls)==1:
                    self.assertEqual(attachment_texts,[private])
                    self.assertIn('不是操作指令',encode(payload))
                else: self.assertEqual(attachment_texts,[])
                return chats.response(text='合成文件已讀取')
            self.app.state.llm_transport=httpx.MockTransport(provider)
            result=self.send(filename,data,ident='text-'+str(index),conversation_id=conversation)
            self.assertEqual(result.status_code,200,result.text[:200])
            self.assertEqual(chats.events(result)[-1][1]['status'],'completed')
            history=self.client.get('/api/messages',params={'conversation_id':conversation}).json()['items']
            self.assertEqual(history[0]['content']['attachments'],[{'kind':'text','filename':filename,'characters':len(private)}])
            self.assertNotIn(private,encode(history))
            self.client.post('/api/chat',json={'text':'繼續摘要','request_id':'text-next-'+str(index),'conversation_id':conversation})
            self.assertEqual(len(calls),2)

    def test_invalid_files_rejected_before_saving_or_network_and_auth_enforced(self):
        calls=[]
        self.app.state.llm_transport=httpx.MockTransport(lambda request: calls.append(request) or chats.response(text='不應被呼叫'))
        cases=[('file.svg',b'<svg/>'),('file.png',b'not an image private-data'),('file.png',image_bytes('GIF')),
               ('file.txt',b'\xff\xfe'),('file.md',b'text\x00binary'),('file.md',b'   '),('file.txt',b'a'*30001),
               ('file.txt',b''),('file.png',b'x'*(resume_pdf.MAX_BYTES+1))]
        for index,(name,data) in enumerate(cases):
            result=self.send(name,data,ident='invalid-'+str(index))
            self.assertEqual(result.status_code,422,(name,result.text))
            self.assertNotIn('private-data',result.text)
        result=self.client.post('/api/chat',data={'text':'合成','request_id':'multiple'},
                                files=[('resume',('a.md',b'a')),('resume',('b.md',b'b'))])
        self.assertEqual(result.status_code,422)
        self.assertEqual(self.send('ok.md',b'ok',ident='valid').status_code,200)
        calls.clear()
        self.assertEqual(self.client.post('/api/chat',data={'text':'合成','request_id':'bad-csrf'},
            files={'resume':('ok.md',b'ok')},headers={'X-CSRF-Token':'bad'}).status_code,403)
        self.client.cookies.delete('jc_user')
        self.assertEqual(self.send('ok.png',image_bytes(),ident='anonymous').status_code,401)
        self.assertEqual(calls,[])
        self.assertEqual(len(self.store.messages()['items']),2)

    def test_text_and_image_limits(self):
        result=asyncio.run(chat_files.prepare({'data':b'a'*30000,'extension':'.txt','filename':'limit.txt'}))
        self.assertEqual(result[1]['characters'],30000)
        raw=image_bytes()
        with patch('app.chat_files.MAX_PIXELS',1), self.assertRaisesRegex(resume_pdf.ResumeError,'像素'):
            chat_files.read_image(raw)
        with patch('app.chat_files.Image.open',side_effect=Image.DecompressionBombError('private bytes')), self.assertRaisesRegex(resume_pdf.ResumeError,'無法讀取'):
            chat_files.read_image(raw)


if __name__=='__main__': unittest.main()
