"""Local-only UI acceptance harness. No school credentials or real print jobs."""
import argparse
import base64
import io
from pathlib import Path
import sqlite3
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from flask import Flask, jsonify, request, send_from_directory, session
from pypdf import PdfReader
from campus_print import create_print_blueprint
from campus_print.common import PrintError


class PreviewAgent:
    def __init__(self):
        self.ready = True
        self.busy = False
        self.next_state = 'submitted'
        self.jobs = {}

    def health(self):
        return {'ready': self.ready, 'busy': self.busy, 'demo': True}

    def inspect(self, pdf):
        try:
            reader = PdfReader(io.BytesIO(base64.b64decode(pdf)))
            if reader.is_encrypted:
                raise PrintError('encrypted_pdf', 422)
            pages = len(reader.pages)
            if pages > 50:
                raise PrintError('too_many_pages', 422)
            if not pages:
                raise ValueError('empty')
            return {'pages': pages}
        except PrintError:
            raise
        except Exception:
            raise PrintError('invalid_pdf', 422) from None

    def submit(self, payload):
        state = self.next_state
        self.next_state = 'submitted'
        code = 'auth_failed' if state == 'rejected' else state
        row = {'id':payload['id'], 'state':state, 'code':code}
        self.jobs[row['id']] = row
        return row

    def job(self, ident):
        return self.jobs[ident]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=5019)
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='maxcourse-print-preview-') as folder:
        db=str(Path(folder)/'preview.sqlite3')
        with sqlite3.connect(db) as conn:
            conn.execute('CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,ispace_username TEXT,display_name TEXT)')
            conn.execute('INSERT INTO users VALUES (1,?,?,?)',('demo','t_demo001','演示同学'))
            conn.execute('INSERT INTO users VALUES (2,?,?,?)',('demo2','t_demo002','第二位演示同学'))
        app=Flask(__name__,static_folder=str(ROOT/'print'),static_url_path='/print')
        fake=PreviewAgent()
        app.config.update(TESTING=True,SECRET_KEY='local-preview-only-not-a-production-key',PRINT_ENABLED=True,PRINT_TEST_AGENT=fake,MAX_CONTENT_LENGTH=16*1024*1024)
        app.register_blueprint(create_print_blueprint(lambda:db))

        @app.before_request
        def preview_identity():
            if not session.get('demo_logged_out'):
                session['user_id']=session.get('demo_user_id',1)

        @app.get('/print/')
        def index():
            return send_from_directory(ROOT/'print','index.html')

        @app.get('/')
        def home():
            return '<a href="/print/">打开打印预览</a>'

        @app.get('/favicon.png')
        def favicon():
            return send_from_directory(ROOT,'favicon.png')

        @app.get('/vendor/<path:name>')
        def vendor(name):
            return send_from_directory(ROOT/'vendor',name)

        @app.post('/api/login/ispace')
        def login():
            data=request.get_json(silent=True) or {}
            if data.get('username') != 'demo' or data.get('password') != 'demo':
                return jsonify(error='本地演示仅使用 demo / demo，不会认证学校账号。'),401
            session.clear()
            session['user_id']=1
            return jsonify(success=True)

        @app.post('/__demo/state')
        def state():
            # This route exists only in this loopback acceptance harness.
            data=request.get_json(silent=True) or {}
            if data.get('reset_jobs'):
                with sqlite3.connect(db) as conn:
                    for table in ('campus_print_jobs','campus_print_limits'):
                        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
                            conn.execute('DELETE FROM '+table)
                fake.jobs.clear()
            if 'ready' in data: fake.ready=bool(data['ready'])
            if 'busy' in data: fake.busy=bool(data['busy'])
            if data.get('next_state') in ('submitted','unknown','rejected'):
                fake.next_state=data['next_state']
            if 'logged_out' in data:
                session.clear()
                session['demo_logged_out']=bool(data['logged_out'])
            if data.get('user_id') in (1,2):
                session['demo_user_id']=data['user_id']
                session['user_id']=data['user_id']
            return jsonify(ok=True)

        print(f'Local-only simulated print preview: http://127.0.0.1:{args.port}/print/',flush=True)
        app.run(host='127.0.0.1',port=args.port,use_reloader=False)


if __name__=='__main__':
    main()
