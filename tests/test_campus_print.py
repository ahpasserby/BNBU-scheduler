import base64
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import ipaddress
from http.server import ThreadingHTTPServer
from pathlib import Path
import sqlite3
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

from flask import Flask
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from campus_print import create_print_blueprint
from campus_print.agent import PrintAgent, handler_for
from campus_print.client import AgentClient
from campus_print.common import PrintError
from campus_print.store import Store

PDF = base64.b64encode(b'%PDF-1.4\nsynthetic-api-boundary-fixture\n%%EOF').decode()


class FakeAgent:
    def __init__(self):
        self.ready = True
        self.busy = False
        self.fail = None
        self.calls = 0
        self.jobs = {}
        self.usernames = []

    def health(self):
        return {'ready': self.ready, 'busy': self.busy, 'demo': True}

    def inspect(self, pdf):
        return {'pages': 2}

    def submit(self, data):
        self.calls += 1
        self.usernames.append(data['username'])
        if self.fail:
            raise self.fail
        job = {'id': data['id'], 'state': 'submitted', 'code': 'submitted'}
        self.jobs[data['id']] = job
        return job

    def job(self, ident):
        return self.jobs[ident]


class PrintAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'db.sqlite3')
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,ispace_username TEXT,display_name TEXT)')
            conn.executemany('INSERT INTO users VALUES (?,?,?,?)', [(1,'alice','t100','Alice'),(2,'bob','t200','Bob'),(3,'unbound',None,'Unbound')])
        self.agent = FakeAgent()
        self.app = Flask(__name__)
        self.app.config.update(TESTING=True, SECRET_KEY='unit-test-secret', PRINT_ENABLED=True, PRINT_TEST_AGENT=self.agent)
        self.app.register_blueprint(create_print_blueprint(lambda: self.db))
        self.client = self.app.test_client()
        self.login(1)

    def login(self, uid):
        self.uid = uid
        with self.client.session_transaction() as session:
            session.clear()
            session['user_id'] = uid
        self.token = self.client.get('/api/print/session').json['csrf_token']

    def post(self, path, data, **kwargs):
        return self.client.post('/api/print/'+path, json=data, headers={'X-Print-CSRF':self.token, 'X-Print-User':str(self.uid), **kwargs})

    def payload(self, key='test-idempotency-key-001'):
        result = self.post('inspect', {'pdf': PDF})
        self.assertEqual(result.status_code, 200)
        return {'pdf': PDF, 'password':'test-only-password', 'inspection_token':result.json['inspection_token'], 'idempotency_key': key}

    def test_authentication_and_csrf_are_required(self):
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.post('/api/print/jobs', json={}).status_code, 401)
        self.login(1)
        self.assertEqual(self.client.post('/api/print/inspect', json={'pdf':PDF}).status_code, 403)
        self.assertEqual(self.post('inspect', {'pdf':PDF}, Origin='https://foreign.example').status_code, 403)
        self.login(3)
        self.assertEqual(self.post('inspect', {'pdf':PDF}).status_code, 403)

    def test_duplicate_submission_sends_once_and_keeps_no_secrets(self):
        data = self.payload()
        first = self.post('jobs', data)
        second = self.post('jobs', data)
        self.assertEqual(first.json['job']['id'], second.json['job']['id'])
        self.assertEqual(self.agent.calls, 1)
        raw = Path(self.db).read_bytes()
        self.assertNotIn(b'test-only-password', raw)
        self.assertNotIn(b'synthetic-api-boundary-fixture', raw)
        self.assertNotIn(PDF.encode(), raw)
        self.assertEqual(first.headers['Cache-Control'], 'no-store, private')

    def test_other_tab_identity_change_rejects_stale_form_before_inspection(self):
        with self.client.session_transaction() as session:
            session['user_id'] = 2
        with mock.patch.object(self.agent,'inspect') as inspect:
            self.assertEqual(self.post('inspect', {'pdf':PDF}).status_code,403)
            inspect.assert_not_called()

    def test_old_processing_state_becomes_unknown_without_retry(self):
        record, _ = Store(self.db).create('e'*32,1,'aged-browser-key-123','digest',1)
        with sqlite3.connect(self.db) as conn:
            conn.execute('UPDATE campus_print_jobs SET updated_at=updated_at-200 WHERE id=?', (record['id'],))
        self.agent.jobs[record['id']]={'id':record['id'],'state':'processing','code':'processing'}
        self.assertEqual(self.client.get('/api/print/jobs/'+record['id']).json['job']['state'],'unknown')
        self.assertEqual(self.client.get('/api/print/jobs/'+record['id']).json['job']['state'],'unknown')
        self.assertEqual(self.agent.calls,0)

    def test_job_ownership_and_school_identity_are_server_selected(self):
        data = self.payload()
        data['username'] = 't200'
        result = self.post('jobs', data).json['job']
        self.assertEqual(self.agent.usernames, ['t100'])
        self.login(2)
        self.assertEqual(self.client.get('/api/print/jobs/'+result['id']).status_code, 404)
        self.assertEqual(self.client.get('/api/print/jobs').json['jobs'], [])

    def test_inspection_binds_user_file_and_account(self):
        data = self.payload()
        data['pdf'] = base64.b64encode(b'%PDF-1.4\ndifferent').decode()
        self.assertEqual(self.post('jobs', data).status_code, 409)
        data = self.payload()
        self.login(2)
        self.assertEqual(self.post('jobs', data).status_code, 409)
        self.assertEqual(self.agent.calls, 0)

    def test_same_key_different_file_is_conflict(self):
        data = self.payload()
        self.post('jobs', data)
        data['pdf'] = base64.b64encode(b'%PDF-1.4\nother').decode()
        self.assertEqual(self.post('jobs', data).status_code, 409)
        self.assertEqual(self.agent.calls, 1)

    def test_network_timeout_is_unknown_not_retry(self):
        self.agent.fail = TimeoutError('untrusted transport error')
        data = self.payload()
        first = self.post('jobs', data)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json['job']['state'], 'unknown')
        self.post('jobs', data)
        self.assertEqual(self.agent.calls, 1)
        self.assertNotIn('untrusted', first.get_data(as_text=True))
        ident = first.json['job']['id']
        self.agent.jobs[ident] = {'id': ident, 'state':'submitted', 'code':'submitted'}
        self.assertEqual(self.client.get('/api/print/jobs/'+ident).json['job']['state'], 'submitted')

    def test_agent_error_without_nonacceptance_evidence_is_unknown(self):
        data = self.payload()
        self.agent.fail = PrintError('offline', 500)
        self.assertEqual(self.post('jobs', data).json['job']['state'], 'unknown')

    def test_offline_rejects_without_saving_job(self):
        data = self.payload()
        self.agent.ready = False
        self.assertEqual(self.post('jobs', data).status_code, 503)
        self.assertEqual(Store(self.db).list(1), [])
        self.assertEqual(self.agent.calls, 0)

    def test_bad_pdf_and_oversized_input(self):
        self.assertEqual(self.post('inspect', {'pdf':'not-base64!'}).status_code, 422)
        self.assertEqual(self.post('inspect', {'pdf':base64.b64encode(b'not pdf').decode()}).status_code, 422)
        self.assertEqual(self.post('inspect', {'pdf':'A'*(14*1024*1024)}).status_code, 413)

    def test_disabled_service_never_calls_agent(self):
        self.app.config['PRINT_ENABLED'] = False
        with mock.patch.object(self.agent, 'health') as health:
            self.assertFalse(self.client.get('/api/print/session').json['service']['ready'])
            health.assert_not_called()

    def test_retention_and_inspection_rate_limit(self):
        for _ in range(12):
            self.assertEqual(self.post('inspect', {'pdf':PDF}).status_code, 200)
        self.assertEqual(self.post('inspect', {'pdf':PDF}).status_code, 429)
        store = Store(self.db)
        store.create('f'*32,2,'old-task-key-12345','digest',1)
        with store.connect() as conn:
            conn.execute('UPDATE campus_print_jobs SET created_at=0')
        self.assertEqual(Store(self.db).list(2), [])

    def test_deleted_user_metadata_is_removed(self):
        self.post('jobs', self.payload())
        with sqlite3.connect(self.db) as conn:
            conn.execute('DELETE FROM users WHERE id=1')
        self.assertEqual(Store(self.db).list(1), [])


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with mock.patch.object(PrintAgent, 'check_sandbox', return_value=True):
            self.agent = PrintAgent(Path(self.tmp.name)/'state', Path(self.tmp.name)/'runtime', cleanup=False)

    def test_restart_never_resends_ambiguous_jobs(self):
        store=self.agent.store
        store.create('a'*32,1,'a'*32,'hash',1)
        store.update('a'*32,1,'sending','processing')
        store.create('b'*32,2,'b'*32,'hash',1)
        store.recover()
        self.assertEqual(store.get('a'*32,1)['state'], 'unknown')
        self.assertEqual(store.get('b'*32,2)['state'], 'failed')

    def test_password_uses_pipe_not_args_environment_or_file(self):
        spool=Path(self.tmp.name)/'maxcourse-test.ps'
        spool.write_bytes(b'fixture')
        seen={}
        def process(args, **kw):
            import os
            seen['password']=os.read(kw['pass_fds'][0],1024)
            self.assertNotIn('a-private-test-password',repr(args))
            self.assertNotIn('a-private-test-password',repr(kw['env']))
            self.assertNotIn('MAXCOURSE_PRINT_AGENT_TOKEN',kw['env'])
            return subprocess.CompletedProcess(args,0,b'',b'putting file maxcourse-test.ps as maxcourse-test.ps')
        with mock.patch('campus_print.agent.subprocess.run',side_effect=process):
            self.assertEqual(self.agent.send(spool,'t100','a-private-test-password'),('submitted','submitted'))
        self.assertEqual(seen['password'],b'a-private-test-password\n')
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()), ['maxcourse-test.ps','runtime','state'])

    def test_missing_receipt_and_auth_failure_are_distinguished(self):
        spool=Path(self.tmp.name)/'file.ps'
        with mock.patch('campus_print.agent.subprocess.run',return_value=subprocess.CompletedProcess([],0,b'',b'')):
            self.assertEqual(self.agent.send(spool,'t100','example'),('unknown','unknown'))
        with mock.patch('campus_print.agent.subprocess.run',return_value=subprocess.CompletedProcess([],1,b'NT_STATUS_LOGON_FAILURE',b'')):
            self.assertEqual(self.agent.send(spool,'t100','example'),('rejected','auth_failed'))

    def test_duplicate_agent_dispatch_and_cleanup(self):
        data={'id':'c'*32,'owner':1,'username':'t100','password':'example','pdf':PDF}
        def convert(folder, ident):
            spool=folder/'job.ps'; spool.write_bytes(b'fixture'); return spool
        with mock.patch.object(self.agent,'health',return_value={'ready':True}), mock.patch.object(self.agent,'pdf_info',return_value=2), mock.patch.object(self.agent,'convert',side_effect=convert), mock.patch.object(self.agent,'send',return_value=('submitted','submitted')) as send:
            first=self.agent.submit(dict(data))
            second=self.agent.submit(dict(data))
            self.assertEqual(first['state'],'submitted')
            self.assertEqual(second['state'],'submitted')
            self.assertEqual(send.call_count,1)
        self.assertEqual(list(self.agent.runtime_dir.iterdir()), [])

    def test_atomic_cloud_reservation(self):
        barrier=threading.Barrier(2)
        def create(number):
            barrier.wait()
            return self.agent.store.create(str(number)*32,9,'one-browser-key-001','same',1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(create,[1,2]))
        self.assertEqual(sum(fresh for _,fresh in results),1)
        self.assertEqual(results[0][0]['id'],results[1][0]['id'])

    def test_private_client_rejects_public_urls_and_empty_token(self):
        for url in ('https://example.org:18765','https://127.0.0.1.evil.example','http://127.0.0.1','https://user@127.0.0.1','https://127.0.0.1/path'):
            with self.assertRaises(ValueError): AgentClient(url,'x'*40)
        with self.assertRaises(ValueError): AgentClient('https://127.0.0.1:18765','')

    def test_http_agent_requires_token_and_uses_no_environment_proxy(self):
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'test-print-agent')])
        now=datetime.now(timezone.utc)
        certificate=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True,path_length=None),True)
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),False)
            .sign(key,hashes.SHA256()))
        cert_path=Path(self.tmp.name)/'agent.crt'
        key_path=Path(self.tmp.name)/'agent.key'
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        server=ThreadingHTTPServer(('127.0.0.1',0),handler_for(self.agent,'t'*40))
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path,key_path)
        server.socket=context.wrap_socket(server.socket,server_side=True,do_handshake_on_connect=False)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url=f'https://127.0.0.1:{server.server_port}'
        with requests.Session() as client:
            client.trust_env=False
            client.verify=str(cert_path)
            self.assertEqual(client.get(url+'/v1/health',timeout=2).status_code,403)
            response=client.post(url+'/v1/jobs',headers={'Authorization':'Bearer '+'t'*40,'Content-Type':'application/json'},data='{}',timeout=2)
            self.assertEqual(response.status_code,400)
            self.assertFalse(response.json()['accepted'])
        with mock.patch.dict('os.environ',{'HTTP_PROXY':'http://127.0.0.1:1','HTTPS_PROXY':'http://127.0.0.1:1'}), mock.patch.object(self.agent,'health',return_value={'ready':False,'busy':False,'demo':False}):
            self.assertFalse(AgentClient(url,'t'*40,str(cert_path)).health()['ready'])
            with self.assertRaises(requests.exceptions.SSLError):
                AgentClient(url,'t'*40).health()


if __name__=='__main__':
    unittest.main()
