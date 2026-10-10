"""Synthetic conversation CRUD, migration and isolated model context."""
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app import backups, security
from app.backup_verify import validate_backup
from app.main import create_app
from app.store import Job, Store, now
import test_m2_chat as existing


class ConversationChecks(unittest.TestCase):
    setUp = existing.ChatChecks.setUp
    tearDown = existing.ChatChecks.tearDown

    def create(self, title='新對話'):
        result = self.client.post('/api/conversations', json={'title':title})
        self.assertEqual(result.status_code, 201, result.text)
        return result.json()['id']

    def send(self, conversation_id, request_id, text):
        result = self.client.post('/api/chat', json={'conversation_id':conversation_id,'request_id':request_id,'text':text})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(existing.events(result)[-1][1]['status'], 'completed')

    def test_crud_search_paging_delete_scope_and_validation(self):
        self.app.state.llm_transport = httpx.MockTransport(lambda request: existing.response(text='合成回覆'))
        first, second = self.create('研究員'), self.create('資料工程師')
        self.send(first, 'alpha', '測試 phrase_一')
        self.send(second, 'beta', 'Different phrase two')
        job_id=self.store.save_job(Job(source='104',source_job_id='kept',title='保留職缺',company_name='合成公司',url='https://example.invalid/kept'))
        with self.store.connect(write=True) as db:
            db.execute('INSERT INTO favorites VALUES(?,?,?)',(job_id,'default',now().isoformat()))
        self.assertEqual([x['id'] for x in self.client.get('/api/conversations',params={'q':'phrase_一'}).json()['items']], [first])
        self.assertEqual([x['id'] for x in self.client.get('/api/conversations',params={'q':'PHRASE TWO'}).json()['items']], [second])
        self.assertEqual(self.client.patch('/api/conversations/'+first,json={'title':'新名稱'}).json()['title'], '新名稱')
        self.assertEqual(self.client.get('/api/conversations',params={'q':'研究員'}).json()['items'], [])
        page = self.client.get('/api/conversations?limit=1').json(); seen=[]
        while True:
            seen += [item['id'] for item in page['items']]
            if not page['next_cursor']: break
            page = self.client.get('/api/conversations',params={'limit':1,'cursor':page['next_cursor']}).json()
        self.assertEqual(len(set(seen)), 3)
        rows = self.client.get('/api/messages',params={'conversation_id':first,'limit':1}).json()
        self.assertEqual(len(rows['items']), 1)
        older = self.client.get('/api/messages',params={'conversation_id':first,'limit':1,'cursor':rows['next_cursor']}).json()
        self.assertEqual(older['items'][0]['content']['text'], '測試 phrase_一')
        self.assertEqual(self.client.delete('/api/conversations/'+first).status_code,204)
        self.assertEqual(self.client.get('/api/messages',params={'conversation_id':first}).status_code,404)
        self.assertEqual(len(self.client.get('/api/messages',params={'conversation_id':second}).json()['items']),2)
        with self.store.connect() as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM favorites').fetchone()[0],1)
        self.assertEqual(self.client.post('/api/chat',json={'conversation_id':first,'request_id':'deleted','text':'x'}).status_code,404)
        for title in ['', '   ', 'a'*81]:
            self.assertEqual(self.client.post('/api/conversations',json={'title':title}).status_code,422)
        self.assertEqual(self.client.patch('/api/conversations/'+second,json={}).status_code,422)
        self.assertEqual(self.client.get('/api/conversations?cursor=bad').status_code,422)
        self.client.headers['X-CSRF-Token']='wrong'
        self.assertEqual(self.client.delete('/api/conversations/'+second).status_code,403)
        with TestClient(self.app,base_url='http://localhost') as anonymous:
            self.assertEqual(anonymous.get('/api/conversations').status_code,401)
            self.assertEqual(anonymous.get('/api/messages',params={'conversation_id':second}).status_code,401)

    def test_model_context_stays_in_selected_conversation_and_survives_restart(self):
        requests=[]
        def provider(request):
            requests.append(json.loads(request.content))
            return existing.response(text='本段回答')
        self.app.state.llm_transport=httpx.MockTransport(provider)
        first, second = self.create('A'), self.create('B')
        self.send(first,'a1','只屬於A的背景')
        self.send(second,'b1','只屬於B的背景')
        self.send(first,'a2','A的追問')
        self.assertNotIn('只屬於A的背景',json.dumps(requests[1],ensure_ascii=False))
        self.assertIn('只屬於A的背景',json.dumps(requests[2],ensure_ascii=False))
        self.assertNotIn('只屬於B的背景',json.dumps(requests[2],ensure_ascii=False))
        restarted=create_app(self.directory.name,automatic_daily=False)
        with TestClient(restarted,base_url='http://localhost') as client:
            client.cookies.set('jc_user',self.user_token)
            self.assertEqual(len(client.get('/api/messages',params={'conversation_id':first}).json()['items']),4)

    def test_running_conversation_cannot_be_deleted_or_replaced(self):
        ident=self.create()
        # The API checks the same active-task slot used by the producer.
        self.app.state.active_chats={'default':{'conversation_id':ident}}
        try:
            self.assertEqual(self.client.delete('/api/conversations/'+ident).status_code,409)
            self.assertEqual(self.client.post('/api/conversations',json={}).status_code,409)
        finally: self.app.state.active_chats={}
        self.assertEqual(self.client.get('/api/messages',params={'conversation_id':ident}).status_code,200)

    def test_pdf_uses_selected_conversation_and_current_backup_roundtrip(self):
        from test_resume_chat import pdf
        ident=self.create('履歷討論')
        self.app.state.llm_transport=httpx.MockTransport(lambda request: existing.response(text='本段履歷摘要'))
        result=self.client.post('/api/chat',data={'text':'合成履歷問題','request_id':'pdf-selected','conversation_id':ident},files={'resume':('synthetic.pdf',pdf('合成技能'),'application/pdf')})
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(existing.events(result)[-1][1]['status'],'completed')
        self.assertEqual(len(self.store.messages(conversation_id=ident)['items']),2)
        self.assertEqual(self.store.messages(conversation_id='legacy')['items'],[])
        backup=backups.create_backup(self.store)
        with self.store.connect() as db: schema=backups.schema_catalog(db)
        with tempfile.TemporaryDirectory() as directory:
            validate_backup(self.store.path.parent/'backups'/(backup['id']+'.zip'),Path(directory),{6:schema})
            backups.restore_backup(self.store,directory)
        self.assertEqual(len(self.store.messages(conversation_id=ident)['items']),2)

    def test_schema4_migration_and_old_backup_restore_preserve_original_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Store(Path(directory)/'old',target_version=4)
            with source.connect(write=True) as db:
                db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',('original','old-request','user','{"text":"保留的舊訊息","status":"completed","events":[]}',now().isoformat()))
                before=[tuple(row) for row in db.execute('SELECT rowid,* FROM messages')]
                schema4=backups.schema_catalog(db)
            old_backup=backups.create_backup(source)
            upgraded=Store(Path(directory)/'old')
            with upgraded.connect() as db:
                after=[tuple(row) for row in db.execute('SELECT rowid,id,request_id,role,content_json,created_at FROM messages')]
                self.assertEqual(before,after)
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],6)
                schema6=backups.schema_catalog(db)
            self.assertEqual(upgraded.messages(conversation_id='legacy')['items'][0]['content']['text'],'保留的舊訊息')
            stage=Path(directory)/'stage';stage.mkdir()
            validate_backup(source.path.parent/'backups'/(old_backup['id']+'.zip'),stage,{4:schema4,6:schema6})
            Store(stage)
            backups.restore_backup(upgraded,stage)
            self.assertEqual(upgraded.messages(conversation_id='legacy')['items'][0]['id'],'original')
            # Exercise the HTTP restore path too: v4 validation precedes staged upgrade.
            archive=(source.path.parent/'backups'/(old_backup['id']+'.zip')).read_bytes()
            result=self.client.post('/api/admin/restore',data={'confirm':'true'},files={'file':('old.zip',archive,'application/zip')},headers={'X-CSRF-Token':security.csrf(self.admin_token)})
            self.assertEqual(result.status_code,200,result.text)
            with self.store.connect() as db: self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],6)
            self.assertEqual(self.store.messages(conversation_id='legacy')['items'][0]['id'],'original')
