"""Loopback-only print agent. Run as a dedicated unprivileged systemd user."""
import argparse
import base64
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time

from .common import CONVERT, DEFAULT_OPTIONS, FEATURES, JOB_ID, MAX_BYTES, MAX_COPIES, MAX_PAGES, USERNAME, PrintError, decode_document, decode_pdf, encode_options, fingerprint, parse_options, public_job
from .store import Store
from .maintenance import start_cleaner


class PrintAgent:
    def __init__(self, state_dir, runtime_dir, server='172.16.244.66', share='DP', domain='UIC', cleanup=True):
        import ipaddress
        ipaddress.IPv4Address(server)
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', share) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', domain):
            raise ValueError('Invalid printer configuration')
        self.server, self.share, self.domain = server, share, domain
        self.state_dir, self.runtime_dir = Path(state_dir), Path(runtime_dir)
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.store = Store(str(self.state_dir / 'jobs.sqlite3'))
        self.store.recover()
        self.cleanup_stop = start_cleaner(self.store.prune) if cleanup else None
        self.lock = threading.Lock()
        self.probe_lock = threading.Lock()
        self.last_probe = (0, False)
        self.sandbox_ready = self.check_sandbox()
        # Conversion is offered only when LibreOffice is installed for the sandbox.
        self.convert_ready = self.sandbox_ready and bool(shutil.which('soffice'))

    def sandbox(self, workdir, args, lang='C'):
        cmd = ['bwrap', '--unshare-all', '--die-with-parent', '--new-session']
        for path in ('/usr', '/bin', '/lib', '/lib64', '/etc/fonts', '/etc/ghostscript', '/etc/libreoffice',
                     '/etc/alternatives', '/var/cache/fontconfig'):
            if Path(path).exists():
                cmd += ['--ro-bind', path, path]
        cmd += ['--proc', '/proc', '--dev', '/dev', '--tmpfs', '/tmp', '--bind', str(workdir), '/work',
                '--chdir', '/work', '--clearenv', '--setenv', 'PATH', '/usr/bin:/bin', '--setenv', 'LANG', lang,
                '--setenv', 'HOME', '/tmp', '--setenv', 'TMPDIR', '/tmp', '--'] + args
        return cmd

    def check_sandbox(self):
        if not all(shutil.which(tool) for tool in ('bwrap', 'gs', 'pdfinfo', 'smbclient')):
            return False
        try:
            with tempfile.TemporaryDirectory(dir=self.runtime_dir) as folder:
                self.run_limited(self.sandbox(folder, ['/usr/bin/true']), 5)
            return True
        except (OSError, subprocess.SubprocessError):
            return False

    def run_limited(self, command, timeout, memory=None, cpu=None):
        env = {'PATH': '/usr/bin:/bin:/usr/sbin', 'LANG': 'C',
               'PYTHONPATH': str(Path(__file__).resolve().parent.parent)}
        budget = (['--memory', str(memory)] if memory else []) + (['--cpu', str(cpu)] if cpu else [])
        args = [sys.executable, '-m', 'campus_print.limits'] + budget + ['--'] + command
        # Regular, unlinked tmpfs files are bounded by the child's RLIMIT_FSIZE.
        # Do not accumulate attacker-controlled parser diagnostics in HTTP memory.
        with tempfile.TemporaryFile(dir=self.runtime_dir) as out, tempfile.TemporaryFile(dir=self.runtime_dir) as err:
            result = subprocess.run(args, stdout=out, stderr=err, timeout=timeout, env=env)
            out.seek(0)
            err.seek(0)
            stdout, stderr = out.read(65536), err.read(65536)
        if result.returncode:
            raise subprocess.CalledProcessError(result.returncode, args, output=stdout, stderr=stderr)
        return subprocess.CompletedProcess(args, result.returncode, stdout, stderr)

    def health(self):
        with self.probe_lock:
            if time.monotonic() - self.last_probe[0] > 10:
                self.store.prune()
                online = False
                if self.sandbox_ready:
                    try:
                        with socket.create_connection((self.server, 445), timeout=1):
                            online = True
                    except OSError:
                        pass
                self.last_probe = (time.monotonic(), online)
        return {'ready': self.sandbox_ready and self.last_probe[1], 'busy': self.lock.locked(), 'demo': False,
                'features': list(FEATURES) + ([CONVERT] if self.convert_ready else []), 'max_copies': MAX_COPIES}

    def pdf_info(self, content, folder):
        (folder / 'document.pdf').write_bytes(content)
        try:
            output = self.run_limited(self.sandbox(folder, ['/usr/bin/pdfinfo', '/work/document.pdf']), 12).stdout.decode('utf-8', 'replace')
        except subprocess.CalledProcessError as exc:
            diagnostic = (exc.stderr or b'').lower()
            raise PrintError('encrypted_pdf' if b'password' in diagnostic else 'invalid_pdf', 422) from None
        except subprocess.TimeoutExpired:
            raise PrintError('invalid_pdf', 422) from None
        if re.search(r'^Encrypted:\s+yes', output, re.M | re.I):
            raise PrintError('encrypted_pdf', 422)
        match = re.search(r'^Pages:\s+(\d+)\s*$', output, re.M)
        if not match or int(match[1]) < 1:
            raise PrintError('invalid_pdf', 422)
        pages = int(match[1])
        if pages > MAX_PAGES:
            raise PrintError('too_many_pages', 422)
        return pages

    def inspect(self, data):
        content = decode_pdf(data.get('pdf'))
        if not self.sandbox_ready:
            raise PrintError('offline', 503, True)
        if not self.lock.acquire(blocking=False):
            raise PrintError('busy', 503, True)
        try:
            with tempfile.TemporaryDirectory(dir=self.runtime_dir) as name:
                return {'pages': self.pdf_info(content, Path(name)), 'bytes': len(content)}
        finally:
            self.lock.release()

    def convert_document(self, data):
        """Convert one Office document or image to PDF inside the offline sandbox."""
        try:
            content, ext = decode_document(data.get('document'), 'input.' + str(data.get('ext', '')))
        except PrintError as exc:
            exc.unaccepted = True
            raise
        if not self.convert_ready:
            raise PrintError('unsupported_format', 422, True)
        if not self.lock.acquire(blocking=False):
            raise PrintError('busy', 503, True)
        try:
            with tempfile.TemporaryDirectory(dir=self.runtime_dir) as name:
                folder = Path(name)
                (folder / 'out').mkdir()
                (folder / ('input.' + ext)).write_bytes(content)
                command = ['/usr/bin/soffice', '--headless', '--norestore', '--nolockcheck', '--nodefault', '--nologo',
                           '-env:UserInstallation=file:///tmp/libreoffice', '--convert-to', 'pdf',
                           '--outdir', '/work/out', '/work/input.' + ext]
                try:
                    self.run_limited(self.sandbox(folder, command, 'C.UTF-8'), 110, memory=1536, cpu=100)
                except (OSError, subprocess.SubprocessError):
                    raise PrintError('convert_failed', 422) from None
                output = folder / 'out' / 'input.pdf'
                if not output.is_file() or output.stat().st_size == 0:
                    raise PrintError('convert_failed', 422)
                if output.stat().st_size > MAX_BYTES:
                    raise PrintError('too_large', 413)
                pdf = output.read_bytes()
                if not pdf.startswith(b'%PDF-'):
                    raise PrintError('convert_failed', 422)
                return {'pdf': base64.b64encode(pdf).decode(), 'pages': self.pdf_info(pdf, folder)}
        finally:
            self.lock.release()

    def convert(self, folder, job_id, options=None):
        options = options or DEFAULT_OPTIONS
        spool = folder / ('maxcourse-' + job_id + '.ps')
        if options['color'] == 'color':
            color = ['-sColorConversionStrategy=RGB', '-dProcessColorModel=/DeviceRGB']
        else:
            color = ['-sColorConversionStrategy=Gray', '-dProcessColorModel=/DeviceGray']
        command = ['/usr/bin/gs', '-q', '-dSAFER', '-dBATCH', '-dNOPAUSE', '-sDEVICE=ps2write',
                   *color, '-sPAPERSIZE=a4',
                   '-dFIXEDMEDIA', '-dPDFFitPage', '-dNumCopies=1', '-dDuplex=false',
                   '-sOutputFile=/work/' + spool.name, '/work/document.pdf']
        self.run_limited(self.sandbox(folder, command), 55)
        with spool.open('rb') as stream:
            if stream.read(10) != b'%!PS-Adobe':
                raise ValueError('Invalid converted PostScript')
        features = device_features(options)
        if features:
            add_setup(spool, features)
        return spool

    def send(self, spool, username, password):
        reader, writer = os.pipe()
        try:
            os.write(writer, password.encode('utf-8') + b'\n')
            os.close(writer)
            writer = None
            env = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'PASSWD_FD': str(reader), 'HOME': str(spool.parent)}
            command = ['smbclient', f'//{self.server}/{self.share}', '-p', '445', '-W', self.domain,
                       '-U', username, '-t', '15', '-d', '1', '--option=client min protocol=SMB2', '--use-kerberos=off',
                       '-c', 'print ' + spool.name]
            result = subprocess.run(command, cwd=spool.parent, env=env, pass_fds=(reader,),
                                    capture_output=True, timeout=60)
            output = (result.stdout + result.stderr).decode('utf-8', 'replace')
            # These session-setup failures occur before any file can be sent.
            if any(status in output for status in ('NT_STATUS_LOGON_FAILURE','NT_STATUS_WRONG_PASSWORD',
                    'NT_STATUS_ACCOUNT_LOCKED_OUT','NT_STATUS_PASSWORD_EXPIRED','NT_STATUS_ACCOUNT_DISABLED')):
                return 'rejected', 'auth_failed'
            if result.returncode == 0 and ('putting file ' + spool.name + ' as ' + spool.name) in output:
                return 'submitted', 'submitted'
            return 'unknown', 'unknown'
        except (OSError, subprocess.SubprocessError):
            return 'unknown', 'unknown'
        finally:
            os.close(reader)
            if writer is not None:
                os.close(writer)

    def submit(self, data):
        ident, owner = data.get('id', ''), data.get('owner')
        username, password = data.get('username', ''), data.get('password')
        if not isinstance(ident, str) or not JOB_ID.fullmatch(ident) or type(owner) is not int or owner < 1 or not isinstance(username, str) or not USERNAME.fullmatch(username):
            raise PrintError('bad_input', 400, True)
        content = decode_pdf(data.get('pdf'))
        try:
            options = parse_options(data.get('options'))
        except PrintError:
            raise PrintError('bad_input', 400, True) from None
        digest = fingerprint(content, username, options)
        old = self.store.get(ident, owner)
        if old:
            if old['fingerprint'] != digest:
                raise PrintError('conflict', 409, True)
            return public_job(old)
        if not isinstance(password, str) or not 1 <= len(password) <= 512 or any(c in password for c in '\r\n\x00'):
            raise PrintError('bad_input', 400, True)
        if not self.health()['ready']:
            raise PrintError('offline', 503, True)
        if not self.lock.acquire(blocking=False):
            raise PrintError('busy', 503, True)
        row = None
        try:
            with tempfile.TemporaryDirectory(dir=self.runtime_dir) as folder_name:
                folder = Path(folder_name)
                pages = self.pdf_info(content, folder)
                row, fresh = self.store.create(ident, owner, ident, digest, pages, encode_options(options))
                if not fresh:
                    return public_job(row)
                try:
                    spool = self.convert(folder, ident, options)
                except (OSError, ValueError, subprocess.SubprocessError):
                    return public_job(self.store.update(ident, owner, 'failed', 'conversion_failed'))
                # Persist dispatch intent before handing any bytes to SMB.
                self.store.update(ident, owner, 'sending', 'processing')
                state, code = self.send(spool, username, password)
                return public_job(self.store.update(ident, owner, state, code))
        except PrintError as exc:
            if row:
                return public_job(self.store.update(ident, owner, 'failed', exc.code))
            exc.unaccepted = True
            raise
        finally:
            data.pop('password', None)
            password = None
            self.lock.release()

    def job(self, ident):
        if not JOB_ID.fullmatch(ident):
            return None
        with self.store.connect() as conn:
            row = conn.execute('SELECT * FROM campus_print_jobs WHERE id=?', (ident,)).fetchone()
            return public_job(row)


def device_features(options):
    """PostScript device requests for duplex and collated copies, in DSC form."""
    features = []
    if options['sides'] != 'one-sided':
        tumble = options['sides'] == 'two-sided-short-edge'
        features.append(('BeginFeature: *Duplex ' + ('DuplexTumble' if tumble else 'DuplexNoTumble'),
                         '<</Duplex true /Tumble %s>> setpagedevice' % ('true' if tumble else 'false'), 'EndFeature'))
    if options['copies'] != 1:
        copies = int(options['copies'])
        features.append(('BeginFeature: *Collate True', '<</Collate true>> setpagedevice', 'EndFeature'))
        features.append(('BeginNonPPDFeature: NumCopies %d' % copies, '<</NumCopies %d>> setpagedevice' % copies, 'EndNonPPDFeature'))
    # Each request is guarded, as printer drivers do, so an unsupported key cannot abort the job.
    return ''.join('[{\n%%%%%s\n%s\n%%%%%s\n} stopped cleartomark\n' % (begin, code, end)
                   for begin, code, end in features).encode()


def add_setup(spool, features):
    """Insert the device requests into the document setup, before the first page."""
    target = spool.with_name(spool.stem + '-setup.ps')
    try:
        with spool.open('rb') as source, target.open('wb') as output:
            prolog_done = in_setup = False
            for line in source:
                if in_setup and line.startswith(b'%%EndSetup'):
                    output.write(features + line)
                    break
                if prolog_done and line.startswith(b'%%Page:'):
                    output.write(b'%%BeginSetup\n' + features + b'%%EndSetup\n' + line)
                    break
                prolog_done = prolog_done or line.startswith(b'%%EndProlog')
                in_setup = in_setup or (prolog_done and line.startswith(b'%%BeginSetup'))
                output.write(line)
            else:
                raise ValueError('Converted PostScript has no document setup position')
            shutil.copyfileobj(source, output)
        target.replace(spool)
    finally:
        target.unlink(missing_ok=True)


def handler_for(agent, token):
    incoming = threading.BoundedSemaphore(2)

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(12)
            super().setup()

        def log_message(self, *args):
            pass

        def reply(self, data, status=200):
            encoded = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(encoded)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(encoded)

        def authorized(self):
            supplied = self.headers.get('Authorization', '')
            if not hmac.compare_digest(supplied.encode(), ('Bearer ' + token).encode()):
                self.reply({'code': 'offline', 'accepted': False}, 403)
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path == '/v1/health':
                self.reply(agent.health())
            elif self.path.startswith('/v1/jobs/'):
                row = agent.job(self.path.removeprefix('/v1/jobs/'))
                self.reply(row or {'code': 'not_found'}, 200 if row else 404)
            else:
                self.reply({'code': 'not_found'}, 404)

        def do_POST(self):
            if not self.authorized():
                return
            if not incoming.acquire(blocking=False):
                self.reply({'code': 'busy', 'accepted': False}, 503)
                return
            try:
                self.process_post()
            finally:
                incoming.release()

        def process_post(self):
            data = {}
            try:
                if self.path not in ('/v1/inspect', '/v1/jobs', '/v1/convert'):
                    self.reply({'code': 'not_found', 'accepted': False}, 404)
                    return
                if agent.lock.locked():
                    raise PrintError('busy', 503, True)
                length = int(self.headers.get('Content-Length', '0'))
                if length < 1 or length > 15 * 1024 * 1024:
                    raise PrintError('too_large', 413, True)
                if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                    raise PrintError('bad_input', 415, True)
                self.connection.settimeout(12)
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise PrintError('bad_input', 400, True)
                handlers = {'/v1/inspect': agent.inspect, '/v1/jobs': agent.submit, '/v1/convert': agent.convert_document}
                result = handlers[self.path](data)
                self.reply(result)
            except PrintError as exc:
                self.reply({'code': exc.code, 'accepted': not exc.unaccepted}, exc.status)
            except (ValueError, OSError):
                self.reply({'code': 'bad_input'}, 400)
            except Exception:
                # Never expose subprocess output, request bodies or credentials.
                self.reply({'code': 'unknown'}, 500)
            finally:
                if isinstance(data, dict):
                    data.pop('password', None)
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=18765)
    args = parser.parse_args()
    token = os.environ.get('MAXCOURSE_PRINT_AGENT_TOKEN', '')
    if len(token) < 32:
        raise SystemExit('Set a dedicated MAXCOURSE_PRINT_AGENT_TOKEN with at least 32 characters.')
    os.umask(0o077)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(os.environ.get('PRINT_AGENT_CERT', '/var/lib/maxcourse-print-agent/agent.crt'),
                            os.environ.get('PRINT_AGENT_KEY', '/var/lib/maxcourse-print-agent/agent.key'))
    agent = PrintAgent(os.environ.get('PRINT_AGENT_STATE_DIR', '/var/lib/maxcourse-print-agent'),
                       os.environ.get('PRINT_AGENT_RUNTIME_DIR', '/run/maxcourse-print-agent'),
                       os.environ.get('PRINT_SMB_SERVER', '172.16.244.66'),
                       os.environ.get('PRINT_SMB_SHARE', 'DP'), os.environ.get('PRINT_SMB_DOMAIN', 'UIC'))
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler_for(agent, token))
    server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == '__main__':
    main()
