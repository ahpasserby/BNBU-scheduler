import base64
import hashlib
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
import time
import unittest
from unittest import mock

from flask import Flask
import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from campus_print import create_print_blueprint
from campus_print.agent import PrintAgent, add_setup, device_features, handler_for
from campus_print.client import AgentClient
from campus_print.common import DEFAULT_OPTIONS, MAX_BYTES, MAX_REQUEST_BYTES, PrintError, decode_document, parse_options
from campus_print.limits import parse as parse_limits
from campus_print.store import Store

PDF = base64.b64encode(b'%PDF-1.4\nsynthetic-api-boundary-fixture\n%%EOF').decode()
DOCX = base64.b64encode(b'PK\x03\x04synthetic-docx-fixture').decode()


class FakeAgent:
    def __init__(self):
        self.ready = True
        self.busy = False
        self.fail = None
        self.calls = 0
        self.jobs = {}
        self.usernames = []
        self.options = []
        self.features = []
        self.pages = 2
        self.converted = []

    def health(self):
        return {'ready': self.ready, 'busy': self.busy, 'demo': True, 'features': self.features}

    def inspect(self, pdf):
        return {'pages': self.pages}

    def submit(self, data):
        self.calls += 1
        self.usernames.append(data['username'])
        self.options.append(data.get('options'))
        if self.fail:
            raise self.fail
        job = {'id': data['id'], 'state': 'submitted', 'code': 'submitted'}
        self.jobs[data['id']] = job
        return job

    def job(self, ident):
        return self.jobs[ident]

    def convert(self, payload):
        self.converted.append(payload['ext'])
        return {'pdf': PDF, 'pages': 3}


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
            conn.execute('UPDATE campus_print_jobs SET updated_at=updated_at-700 WHERE id=?', (record['id'],))
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
        self.assertEqual(self.post('inspect', {'pdf':'A'*MAX_REQUEST_BYTES}).status_code, 413)

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

    def test_output_options_wait_for_agent_features(self):
        caps = self.client.get('/api/print/session').json['capabilities']
        self.assertEqual((caps['color'], caps['sides'], caps['copies']['max']), (['grayscale'], ['one-sided'], 1))
        data = self.payload()
        data['options'] = {'color': 'color'}
        result = self.post('jobs', data)
        self.assertEqual((result.status_code, result.json['code']), (422, 'unsupported_option'))
        self.assertEqual((self.agent.calls, Store(self.db).list(1)), (0, []))

    def test_supported_options_are_forwarded_stored_and_bound_to_intent(self):
        self.agent.features = ['color', 'duplex', 'copies', 'future-feature']
        state = self.client.get('/api/print/session').json
        self.assertEqual(state['service']['features'], ['color', 'duplex', 'copies'])
        self.assertIn('two-sided-short-edge', state['capabilities']['sides'])
        self.assertEqual(state['limits']['max_impressions'], 30000)
        chosen = {'color': 'color', 'sides': 'two-sided-long-edge', 'copies': 3}
        data = self.payload()
        data['options'] = chosen
        job = self.post('jobs', data).json['job']
        self.assertEqual((job['state'], job['options']), ('submitted', chosen))
        self.assertEqual(self.agent.options, [chosen])
        self.assertEqual(self.client.get('/api/print/jobs').json['jobs'][0]['options'], chosen)
        data['options'] = dict(chosen, copies=4)
        self.assertEqual(self.post('jobs', data).status_code, 409)
        self.assertEqual(self.agent.calls, 1)
        plain = self.post('jobs', self.payload('test-idempotency-key-002')).json['job']
        self.assertEqual((plain['options'], self.agent.options[-1]), (DEFAULT_OPTIONS, None))

    def test_invalid_options_and_impression_limit(self):
        self.agent.features = ['color', 'duplex', 'copies']
        for options in ({'copies': 0}, {'copies': 101}, {'copies': '2'}, {'copies': True}, {'color': 'sepia'},
                        {'sides': 'two-sided'}, {'staple': True}, ['color']):
            data = self.payload()
            data['options'] = options
            self.assertEqual(self.post('jobs', data).status_code, 400, options)
        self.agent.pages = 50
        data = self.payload('test-idempotency-key-003')
        data['options'] = {'copies': 5}
        result = self.post('jobs', data)
        self.assertEqual((result.status_code, result.json['code']), (422, 'bulk_confirmation_required'))
        self.assertEqual(self.agent.calls, 0)

    def convert_request(self, name='report.docx', document=DOCX, token=None, ip='203.0.113.7'):
        # Requests arrive through the local proxy, which supplies the visitor address.
        return self.client.post('/api/print/convert', json={'document': document, 'name': name},
                                headers={'X-Print-CSRF': self.token if token is None else token, 'X-Real-IP': ip},
                                environ_base={'REMOTE_ADDR': '127.0.0.1'})

    def test_conversion_is_gated_by_agent_feature_and_page_token(self):
        with self.client.session_transaction() as session:
            session.pop('user_id', None)
        self.assertEqual(self.convert_request().json['code'], 'unsupported_format')
        self.agent.features = ['convert']
        self.assertEqual(self.client.get('/api/print/session').json['service']['features'], ['convert'])
        self.assertEqual(self.convert_request(token='forged').status_code, 403)
        result = self.convert_request()
        self.assertEqual((result.status_code, result.json['pages'], result.json['pdf']), (200, 3, PDF))
        self.assertEqual(self.agent.converted, ['docx'])
        self.assertEqual(self.convert_request(name='notes.txt').json['code'], 'unsupported_format')
        self.assertEqual(self.convert_request(name='slides.pptx', document=PDF).json['code'], 'unsupported_format')
        self.agent.ready = False
        self.assertEqual(self.convert_request().status_code, 503)

    def test_anonymous_conversion_is_rate_limited_per_address(self):
        self.agent.features = ['convert']
        with self.client.session_transaction() as session:
            session.pop('user_id', None)
        for _ in range(6):
            self.assertEqual(self.convert_request(ip='203.0.113.9').status_code, 200)
        self.assertEqual(self.convert_request(ip='203.0.113.9').status_code, 429)
        self.assertEqual(self.convert_request(ip='198.51.100.4').status_code, 200)
        Store(self.db)  # Pruning orphaned user rows must keep anonymous buckets.
        self.assertEqual(self.convert_request(ip='203.0.113.9').status_code, 429)

    def test_document_signatures_and_limits_parsing(self):
        self.assertEqual(decode_document(DOCX, 'A.DOCX')[1], 'docx')
        for name, value in (('a.doc', DOCX), ('a.exe', DOCX), ('noext', DOCX), ('a.png', 'not-base64!')):
            with self.assertRaises(PrintError):
                decode_document(value, name)
        self.assertEqual(parse_limits(['--memory', '1536', '--cpu', '100', '--', 'soffice']), (1536, 100, ['soffice']))
        self.assertEqual(parse_limits(['--', 'gs']), (384, 160, ['gs']))
        with self.assertRaises(SystemExit):
            parse_limits(['--memory', '9999', '--', 'gs'])

    def test_large_confirmation_is_bound_to_document_pages_and_options(self):
        self.agent.features = ['copies', 'duplex']
        self.agent.pages = 300
        data = self.payload()
        options = dict(DEFAULT_OPTIONS, copies=100, sides='two-sided-long-edge')
        data['options'] = options
        confirmation = {'sha256': hashlib.sha256(base64.b64decode(PDF)).hexdigest(), 'pages': 300, 'options': options}
        for invalid in (None, True, dict(confirmation, pages=299), dict(confirmation, sha256='wrong'),
                        dict(confirmation, options=dict(options, copies=99))):
            data['bulk_confirmation'] = invalid
            self.assertEqual(self.post('jobs', data).json['code'], 'bulk_confirmation_required')
        self.assertEqual((self.agent.calls, Store(self.db).list(1)), (0, []))
        data['bulk_confirmation'] = confirmation
        self.assertEqual(self.post('jobs', data).json['job']['state'], 'submitted')
        # A retry reconciles the accepted intent even after approval/ticket expires.
        data.pop('bulk_confirmation')
        data['inspection_token'] = 'expired'
        self.assertEqual(self.post('jobs', data).json['job']['state'], 'submitted')
        self.assertEqual(self.agent.calls, 1)

    def test_twenty_one_copies_and_large_pdf_pass_old_boundaries(self):
        self.agent.features = ['copies']
        data = self.payload()
        data['options'] = {'copies': 21}
        self.assertEqual(self.post('jobs', data).json['job']['options']['copies'], 21)
        # Raising print requests does not change Flask's global upload limit.
        self.app.config['MAX_CONTENT_LENGTH'] = 16 * 1024**2
        document = base64.b64encode(b'%PDF-' + b' ' * (17 * 1024**2)).decode()
        self.agent.pages = 300
        response = self.post('inspect', {'pdf': document})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['pages'], 300)
        self.assertEqual(self.app.config['MAX_CONTENT_LENGTH'], 16 * 1024**2)
        self.agent.pages = 301
        self.assertEqual(self.post('inspect', {'pdf': PDF}).status_code, 422)

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
        def convert(folder, ident, options):
            spool=folder/'job.ps'; spool.write_bytes(b'fixture'); return spool
        with mock.patch.object(self.agent,'health',return_value={'ready':True}), mock.patch.object(self.agent,'pdf_info',return_value=2), mock.patch.object(self.agent,'convert',side_effect=convert), mock.patch.object(self.agent,'send',return_value=('submitted','submitted')) as send:
            first=self.agent.submit(dict(data))
            second=self.agent.submit(dict(data))
            self.assertEqual(first['state'],'submitted')
            self.assertEqual(second['state'],'submitted')
            self.assertEqual(send.call_count,1)
        self.assertEqual(list(self.agent.runtime_dir.iterdir()), [])

    def test_agent_requires_bulk_confirmation_before_conversion(self):
        data = {'id': 'c'*32, 'owner': 1, 'username': 't100', 'password': 'example', 'pdf': PDF,
                'options': dict(DEFAULT_OPTIONS, copies=100)}
        with mock.patch.object(self.agent, 'health', return_value={'ready': True}), \
             mock.patch.object(self.agent, 'pdf_info', return_value=300), \
             mock.patch.object(self.agent, 'convert') as convert, mock.patch.object(self.agent, 'send') as send:
            with self.assertRaises(PrintError) as caught:
                self.agent.submit(dict(data))
            self.assertEqual(caught.exception.code, 'bulk_confirmation_required')
            self.assertTrue(caught.exception.unaccepted)
            convert.assert_not_called()
            send.assert_not_called()
        self.assertEqual(self.agent.store.list(1), [])
        self.assertEqual(list(self.agent.runtime_dir.iterdir()), [])

    def test_agent_page_limit_checks_real_pdfinfo_count(self):
        folder = self.agent.runtime_dir
        for pages in (51, 300, 301):
            output = subprocess.CompletedProcess([], 0, f'Pages: {pages}\nEncrypted: no\n'.encode(), b'')
            with mock.patch.object(self.agent, 'run_limited', return_value=output):
                if pages <= 300:
                    self.assertEqual(self.agent.pdf_info(b'%PDF-fixture', folder), pages)
                else:
                    with self.assertRaises(PrintError) as caught:
                        self.agent.pdf_info(b'%PDF-fixture', folder)
                    self.assertEqual(caught.exception.code, 'too_many_pages')

    def test_health_advertises_output_features(self):
        self.agent.last_probe=(float('inf'),True)
        self.agent.convert_ready=False
        self.assertEqual(self.agent.health()['features'],['color','duplex','copies'])
        self.agent.convert_ready=True
        self.assertEqual(self.agent.health()['features'],['color','duplex','copies','convert'])

    def test_conversion_runs_libreoffice_in_the_sandbox(self):
        self.agent.convert_ready=True
        seen={}
        def run(command, timeout, memory=None, cpu=None):
            seen.update(command=command, memory=memory, cpu=cpu)
            work=Path(command[command.index('--bind')+1])
            self.assertEqual(sorted(p.name for p in work.iterdir()),['input.docx','out'])
            (work/'out'/'input.pdf').write_bytes(b'%PDF-1.7 converted')
        with mock.patch.object(self.agent,'run_limited',side_effect=run), mock.patch.object(self.agent,'pdf_info',return_value=4):
            result=self.agent.convert_document({'document':DOCX,'ext':'docx'})
        self.assertEqual((result['pages'],base64.b64decode(result['pdf'])),(4,b'%PDF-1.7 converted'))
        self.assertIn('--unshare-all',seen['command'])
        self.assertIn('/usr/bin/soffice',seen['command'])
        self.assertEqual((seen['memory'],seen['cpu']),(1536,220))
        self.assertEqual(list(self.agent.runtime_dir.iterdir()),[])
        with mock.patch.object(self.agent,'run_limited'):
            with self.assertRaises(PrintError) as caught:
                self.agent.convert_document({'document':DOCX,'ext':'docx'})
        self.assertEqual(caught.exception.code,'convert_failed')
        with self.assertRaises(PrintError) as caught:
            self.agent.convert_document({'document':PDF,'ext':'docx'})
        self.assertTrue(caught.exception.unaccepted)
        self.agent.convert_ready=False
        with self.assertRaises(PrintError) as caught:
            self.agent.convert_document({'document':DOCX,'ext':'docx'})
        self.assertEqual((caught.exception.code,caught.exception.unaccepted),('unsupported_format',True))

    def fake_ghostscript(self, body):
        commands=[]
        def run(command, timeout):
            commands.append(command)
            output=next(arg for arg in command if arg.startswith('-sOutputFile=/work/')).removeprefix('-sOutputFile=/work/')
            (self.work/output).write_bytes(body)
        return commands, mock.patch.object(self.agent,'run_limited',side_effect=run)

    def test_conversion_applies_colour_duplex_and_collated_copies(self):
        self.work=Path(self.tmp.name)/'work'; self.work.mkdir()
        prolog=b'%!PS-Adobe-3.0\n%%EndComments\n%%BeginProlog\n/x 1 def\n%%EndProlog\n%%Page: 1 1\npage\n%%Trailer\n%%EOF\n'
        commands, patch=self.fake_ghostscript(prolog)
        with patch:
            plain=self.agent.convert(self.work,'d'*32).read_bytes()
            chosen=self.agent.convert(self.work,'d'*32,{'color':'color','sides':'two-sided-short-edge','copies':3}).read_bytes()
        self.assertEqual(plain,prolog)
        self.assertIn('-sColorConversionStrategy=Gray',commands[0])
        self.assertIn('-sColorConversionStrategy=RGB',commands[1])
        self.assertNotIn('-dProcessColorModel=/DeviceGray',commands[1])
        setup=chosen[chosen.index(b'%%EndProlog'):chosen.index(b'%%Page: 1 1')]
        for request in (b'<</Duplex true /Tumble true>> setpagedevice',b'<</Collate true>> setpagedevice',b'<</NumCopies 3>> setpagedevice',b'%%BeginSetup',b'%%EndSetup'):
            self.assertIn(request,setup)
        self.assertEqual(setup.count(b'stopped cleartomark'),3)
        self.assertEqual(sorted(p.name for p in self.work.iterdir()),['maxcourse-'+'d'*32+'.ps'])

    def test_oversized_conversion_stops_before_setup_copy(self):
        self.work = Path(self.tmp.name) / 'work'
        self.work.mkdir()
        def run(command, timeout):
            output = self.work / ('maxcourse-' + 'a'*32 + '.ps')
            with output.open('wb') as stream:
                stream.write(b'%!PS-Adobe')
                stream.truncate(128 * 1024**2 + 1)
        with mock.patch.object(self.agent, 'run_limited', side_effect=run), mock.patch('campus_print.agent.add_setup') as setup:
            with self.assertRaises(ValueError):
                self.agent.convert(self.work, 'a'*32, dict(DEFAULT_OPTIONS, copies=100))
            setup.assert_not_called()

    def test_existing_setup_section_is_extended_and_missing_position_fails(self):
        self.work=Path(self.tmp.name)/'work'; self.work.mkdir()
        spool=self.work/'job.ps'
        spool.write_bytes(b'%!PS-Adobe-3.0\n%%EndProlog\n%%BeginSetup\nsetup\n%%EndSetup\n%%Page: 1 1\n')
        add_setup(spool,device_features(dict(DEFAULT_OPTIONS,sides='two-sided-long-edge')))
        text=spool.read_bytes()
        self.assertEqual(text.count(b'%%BeginSetup'),1)
        self.assertLess(text.index(b'/Tumble false'),text.index(b'%%EndSetup'))
        self.assertEqual(device_features(DEFAULT_OPTIONS),b'')
        spool.write_bytes(b'%!PS-Adobe-3.0\n%%Page: 1 1\n%%EndProlog\n')
        with self.assertRaises(ValueError):
            add_setup(spool,device_features(dict(DEFAULT_OPTIONS,copies=2)))
        self.assertEqual(spool.read_bytes(),b'%!PS-Adobe-3.0\n%%Page: 1 1\n%%EndProlog\n')
        self.assertEqual([p.name for p in self.work.iterdir()],['job.ps'])

    def test_agent_validates_options_and_binds_them_to_the_job(self):
        data={'id':'e'*32,'owner':1,'username':'t100','password':'example','pdf':PDF}
        with self.assertRaises(PrintError) as caught:
            self.agent.submit(dict(data,options={'copies':101}))
        self.assertTrue(caught.exception.unaccepted)
        def convert(folder, ident, options):
            seen.append(options); spool=folder/'job.ps'; spool.write_bytes(b'fixture'); return spool
        seen=[]
        with mock.patch.object(self.agent,'health',return_value={'ready':True}), mock.patch.object(self.agent,'pdf_info',return_value=1), mock.patch.object(self.agent,'convert',side_effect=convert), mock.patch.object(self.agent,'send',return_value=('submitted','submitted')):
            job=self.agent.submit(dict(data,options={'color':'color','copies':2}))
            self.assertEqual(job['options'],{'color':'color','sides':'one-sided','copies':2})
            with self.assertRaises(PrintError) as caught:
                self.agent.submit(dict(data,options={'color':'color','copies':3}))
        self.assertEqual(caught.exception.code,'conflict')
        self.assertEqual(seen,[{'color':'color','sides':'one-sided','copies':2}])
        self.assertEqual(parse_options(None),DEFAULT_OPTIONS)

    def test_existing_job_table_gains_default_options(self):
        path=str(Path(self.tmp.name)/'legacy.sqlite3')
        with sqlite3.connect(path) as conn:
            conn.execute('CREATE TABLE campus_print_jobs (id TEXT PRIMARY KEY, owner INTEGER NOT NULL, idempotency_key TEXT NOT NULL, fingerprint TEXT NOT NULL, state TEXT NOT NULL, code TEXT NOT NULL, pages INTEGER NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, UNIQUE(owner, idempotency_key))')
            conn.execute("INSERT INTO campus_print_jobs VALUES ('%s',1,'legacy-key-000001','digest','submitted','submitted',1,strftime('%%s','now'),strftime('%%s','now'))" % ('f'*32))
        from campus_print.common import public_job
        self.assertEqual(public_job(Store(path).get('f'*32,1))['options'],DEFAULT_OPTIONS)

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
        handler = handler_for(self.agent, 't'*40)
        process_post = handler.process_post
        def slow_body(request):
            if request.path == '/v1/inspect':
                time.sleep(2.5)  # The SSH peer can take longer than TCP connect to drain an upload.
            return process_post(request)
        handler.process_post = slow_body
        server=ThreadingHTTPServer(('127.0.0.1',0),handler)
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
        with mock.patch.object(self.agent, 'inspect', return_value={'pages': 1}):
            self.assertEqual(AgentClient(url, 't'*40, str(cert_path)).inspect('A' * (8 * 1024**2)), {'pages': 1})


if __name__=='__main__':
    unittest.main()
