import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import threading
from urllib.parse import urlsplit

from flask import Blueprint, current_app, jsonify, request, session
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .client import AgentClient
from .common import DEFAULT_OPTIONS, FEATURES, JOB_ID, MAX_BYTES, MAX_IMPRESSIONS, MAX_PAGES, MESSAGES, USERNAME, PrintError, capabilities, decode_pdf, encode_options, fingerprint, parse_options, public_job
from .store import Store
from .maintenance import start_cleaner


def create_blueprint(db_path):
    bp = Blueprint('campus_print', __name__, url_prefix='/api/print')
    cleanup_lock = threading.Lock()

    @bp.before_request
    def metadata_cleanup():
        if current_app.testing:
            return
        with cleanup_lock:
            if 'campus_print_cleaner' not in current_app.extensions:
                current_app.extensions['campus_print_cleaner'] = start_cleaner(lambda: Store(db_path()).prune())

    def store():
        return Store(db_path())

    def current_user(required=True):
        uid = session.get('user_id')
        row = None
        if uid:
            conn = sqlite3.connect(db_path())
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute('SELECT id,username,ispace_username,display_name FROM users WHERE id=?', (uid,)).fetchone()
            finally:
                conn.close()
        if row is None:
            if required:
                raise PrintError('login_required', 401)
            return None
        sid = (row['ispace_username'] or '').strip()
        if required and not USERNAME.fullmatch(sid):
            raise PrintError('school_login_required', 403)
        return {'id': row['id'], 'display_name': row['display_name'] or row['username'], 'school_username': sid if USERNAME.fullmatch(sid) else ''}

    def agent():
        if current_app.testing and current_app.config.get('PRINT_TEST_AGENT') is not None:
            return current_app.config['PRINT_TEST_AGENT']
        return AgentClient(current_app.config.get('PRINT_AGENT_URL') or os.getenv('MAXCOURSE_PRINT_AGENT_URL', 'https://127.0.0.1:18765'),
                           current_app.config.get('PRINT_AGENT_TOKEN') or os.getenv('MAXCOURSE_PRINT_AGENT_TOKEN', ''),
                           current_app.config.get('PRINT_AGENT_CA') or os.getenv('MAXCOURSE_PRINT_AGENT_CA'))

    def service():
        enabled = current_app.config.get('PRINT_ENABLED', os.getenv('MAXCOURSE_PRINT_ENABLED', '0') == '1')
        result = dict(enabled=bool(enabled), online=False, ready=False, busy=False, demo=False, features=[], message='打印服务准备中，暂不接收任务。')
        if enabled:
            try:
                health = agent().health()
                if health.get('demo') and not current_app.testing:
                    return result
                features = health.get('features')
                # Agents before output options report none and keep the fixed defaults.
                result.update(online=True, ready=health.get('ready') is True, busy=health.get('busy') is True,
                              demo=health.get('demo') is True,
                              features=[f for f in FEATURES if isinstance(features, list) and f in features])
                result['message'] = '可以提交打印' if result['ready'] and not result['busy'] else MESSAGES['busy' if result['busy'] else 'offline']
            except Exception:
                result['message'] = MESSAGES['offline']
        return result

    def ready():
        status = service()
        if not status['ready']:
            raise PrintError('offline', 503)
        if status['busy']:
            raise PrintError('busy', 503)
        return status

    def ticket_signer():
        return URLSafeTimedSerializer(current_app.secret_key, salt='campus-print-inspection-v1')

    def mutation():
        user = current_user()
        if request.headers.get('X-Print-User') != str(user['id']):
            raise PrintError('csrf_failed', 403)
        token = session.get('print_csrf', '')
        provided = request.headers.get('X-Print-CSRF', '')
        origin = request.headers.get('Origin')
        if not token or not hmac.compare_digest(token.encode(), provided.encode()) or (origin and urlsplit(origin).netloc != request.host):
            raise PrintError('csrf_failed', 403)
        if request.mimetype != 'application/json':
            raise PrintError('bad_input', 415)
        if request.content_length and request.content_length > 15 * 1024 * 1024:
            raise PrintError('too_large', 413)
        return user

    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise PrintError('bad_input')
        return data

    def apply_result(row, result):
        if result.get('id') != row['id']:
            raise ValueError('Agent job ID mismatch')
        state = result.get('state')
        if state == 'sending':
            state = 'processing'
        if state not in ('processing','submitted','unknown','failed','rejected'):
            raise ValueError('Invalid agent job state')
        if row['state'] == 'unknown' and state == 'processing':
            return row
        code = result.get('code')
        if code not in MESSAGES:
            code = 'unknown'
        return store().update(row['id'], row['owner'], state, code)

    def reconcile(row):
        if row['state'] in ('processing', 'unknown'):
            try:
                updated = apply_result(row, agent().job(row['id']))
                if updated['state'] == 'processing' and time.time() - updated['updated_at'] > 180:
                    return store().update(row['id'], row['owner'], 'unknown', 'unknown')
                return updated
            except Exception:
                if row['state'] == 'processing' and time.time() - row['updated_at'] > 180:
                    return store().update(row['id'], row['owner'], 'unknown', 'unknown')
        return row

    @bp.after_request
    def private(response):
        response.headers['Cache-Control'] = 'no-store, private'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @bp.errorhandler(PrintError)
    def error(exc):
        extra = {'login_required': '请先登录学校账号。', 'school_login_required': '请使用已验证的学校账号登录后打印。',
                 'csrf_failed': '登录状态已更新，请刷新页面后重试。', 'inspection_expired': '文件检查已过期，请重新检查后提交。',
                 'password_required': '请输入本次打印使用的学校密码。'}
        return jsonify(error=extra.get(exc.code, str(exc)), code=exc.code), exc.status

    @bp.get('/session')
    def get_session():
        if 'print_csrf' not in session:
            session['print_csrf'] = secrets.token_urlsafe(32)
        status = service()
        return jsonify(csrf_token=session['print_csrf'], user=current_user(False), service=status,
                       limits={'max_bytes': MAX_BYTES, 'max_pages': MAX_PAGES, 'max_impressions': MAX_IMPRESSIONS},
                       capabilities=capabilities(status['features']))

    @bp.post('/inspect')
    def inspect():
        user = mutation()
        store().limit(user['id'], 'inspect', 12, 60)
        ready()
        data = body()
        content = decode_pdf(data.get('pdf'))
        try:
            result = agent().inspect(data['pdf'])
        except PrintError:
            raise
        except Exception:
            raise PrintError('offline', 503) from None
        pages = result.get('pages')
        if type(pages) is not int or not 1 <= pages <= MAX_PAGES:
            raise PrintError('invalid_pdf', 422)
        digest = hashlib.sha256(content).hexdigest()
        token = ticket_signer().dumps({'user': user['id'], 'username': user['school_username'], 'sha256': digest, 'pages': pages})
        return jsonify(pages=pages, bytes=len(content), sha256=digest, inspection_token=token)

    @bp.post('/jobs')
    def submit():
        user = mutation()
        data = body()
        key = data.get('idempotency_key', '')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', key):
            raise PrintError('bad_input')
        content = decode_pdf(data.get('pdf'))
        options = parse_options(data.get('options'))
        digest = fingerprint(content, user['school_username'], options)
        previous = store().get_key(key, user['id'])
        if previous:
            if previous['fingerprint'] != digest:
                raise PrintError('conflict', 409)
            return jsonify(job=public_job(reconcile(previous)))
        try:
            inspected = ticket_signer().loads(data.get('inspection_token', ''), max_age=600)
        except (BadSignature, SignatureExpired, TypeError):
            raise PrintError('inspection_expired', 409) from None
        if not isinstance(inspected, dict) or inspected.get('user') != user['id'] or inspected.get('username') != user['school_username'] or inspected.get('sha256') != hashlib.sha256(content).hexdigest():
            raise PrintError('inspection_expired', 409)
        password = data.get('password')
        if not isinstance(password, str) or not 1 <= len(password) <= 512 or any(c in password for c in '\r\n\x00'):
            raise PrintError('password_required')
        if inspected['pages'] * options['copies'] > MAX_IMPRESSIONS:
            raise PrintError('too_many_impressions', 422)
        # Only forward options the connected agent advertises; older agents ignore unknown fields.
        parse_options(options, ready()['features'])
        row, created = store().create(secrets.token_hex(16), user['id'], key, digest, inspected['pages'], encode_options(options))
        if not created:
            return jsonify(job=public_job(reconcile(row)))
        payload = {'id': row['id'], 'owner': user['id'], 'username': user['school_username'],
                   'password': password, 'pdf': data['pdf']}
        if options != DEFAULT_OPTIONS:
            payload['options'] = options
        try:
            result = agent().submit(payload)
            row = apply_result(row, result)
        except PrintError as exc:
            # Busy/offline rejects before accepting a job; other failures may be ambiguous.
            state = 'failed' if exc.unaccepted else 'unknown'
            row = store().update(row['id'], user['id'], state, exc.code if state == 'failed' else 'unknown')
        except Exception:
            row = store().update(row['id'], user['id'], 'unknown', 'unknown')
        finally:
            data.pop('password', None)
            payload.pop('password', None)
            password = None
        return jsonify(job=public_job(row)), 202 if row['state'] in ('unknown','processing') else 200

    @bp.get('/jobs')
    def jobs():
        user = current_user()
        return jsonify(jobs=[public_job(reconcile(row)) for row in store().list(user['id'])])

    @bp.get('/jobs/<ident>')
    def job(ident):
        user = current_user()
        row = store().get(ident, user['id']) if JOB_ID.fullmatch(ident) else None
        if row is None:
            return jsonify(error='没有找到这个打印任务。', code='not_found'), 404
        return jsonify(job=public_job(reconcile(row)))

    return bp
