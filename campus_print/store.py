from contextlib import contextmanager
import sqlite3
import time

from .common import RETENTION_SECONDS, PrintError


class Store:
    """Persist metadata only. No documents, passwords, titles or school accounts."""

    def __init__(self, path):
        self.path = path
        with self.connect() as conn:
            conn.executescript('''
                CREATE TABLE IF NOT EXISTS campus_print_jobs (
                    id TEXT PRIMARY KEY, owner INTEGER NOT NULL,
                    idempotency_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL, code TEXT NOT NULL,
                    pages INTEGER NOT NULL, created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(owner, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS campus_print_limits (
                    owner INTEGER NOT NULL, kind TEXT NOT NULL, at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS campus_print_limits_lookup
                    ON campus_print_limits(owner,kind,at);
            ''')
            try:
                # Output options only (colour, sides, copies); never file details.
                conn.execute("ALTER TABLE campus_print_jobs ADD COLUMN options TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
        self.prune()

    def prune(self):
        with self.connect() as conn:
            now = int(time.time())
            conn.execute('DELETE FROM campus_print_jobs WHERE created_at < ?', (now - RETENTION_SECONDS,))
            conn.execute('DELETE FROM campus_print_limits WHERE at < ?', (now - 3600,))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone():
                conn.execute('DELETE FROM campus_print_jobs WHERE owner NOT IN (SELECT id FROM users)')
                # Owners at or below zero are anonymous conversion buckets, not users.
                conn.execute('DELETE FROM campus_print_limits WHERE owner > 0 AND owner NOT IN (SELECT id FROM users)')

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA secure_delete=ON')
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def limit(self, owner, kind, count, seconds):
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            n = conn.execute('SELECT COUNT(*) FROM campus_print_limits WHERE owner=? AND kind=? AND at>?',
                             (owner, kind, int(time.time()) - seconds)).fetchone()[0]
            if n >= count:
                raise PrintError("rate_limited", 429)
            conn.execute('INSERT INTO campus_print_limits VALUES (?,?,?)', (owner, kind, int(time.time())))

    def create(self, ident, owner, key, digest, pages, options=''):
        now = int(time.time())
        with self.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT * FROM campus_print_jobs WHERE owner=? AND idempotency_key=?', (owner, key)).fetchone()
            if old:
                if old['fingerprint'] != digest:
                    raise PrintError('conflict', 409)
                return dict(old), False
            active = conn.execute("SELECT COUNT(*) FROM campus_print_jobs WHERE owner=? AND state IN ('processing','sending') AND updated_at>?", (owner, now - 180)).fetchone()[0]
            if active:
                raise PrintError('active_job', 409)
            count = conn.execute('SELECT COUNT(*) FROM campus_print_jobs WHERE owner=? AND created_at>?', (owner, now - 3600)).fetchone()[0]
            bad = conn.execute("SELECT COUNT(*) FROM campus_print_jobs WHERE owner=? AND code='auth_failed' AND created_at>?", (owner, now - 900)).fetchone()[0]
            if bad >= 3:
                raise PrintError('auth_rate_limited', 429)
            if count >= 10:
                raise PrintError('rate_limited', 429)
            conn.execute('INSERT INTO campus_print_jobs (id,owner,idempotency_key,fingerprint,state,code,pages,created_at,updated_at,options) '
                         'VALUES (?,?,?,?,?,?,?,?,?,?)',
                         (ident, owner, key, digest, 'processing', 'processing', pages, now, now, options))
        return self.get(ident, owner), True

    def get(self, ident, owner):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM campus_print_jobs WHERE id=? AND owner=?', (ident, owner)).fetchone()
            return dict(row) if row else None

    def get_key(self, key, owner):
        with self.connect() as conn:
            row = conn.execute('SELECT * FROM campus_print_jobs WHERE idempotency_key=? AND owner=?', (key, owner)).fetchone()
            return dict(row) if row else None

    def list(self, owner):
        with self.connect() as conn:
            return [dict(row) for row in conn.execute('SELECT * FROM campus_print_jobs WHERE owner=? ORDER BY created_at DESC LIMIT 20', (owner,))]

    def update(self, ident, owner, state, code, pages=None):
        with self.connect() as conn:
            # Never downgrade a confirmed receipt or overwrite a terminal result.
            conn.execute('''UPDATE campus_print_jobs SET state=?,code=?,pages=COALESCE(?,pages),updated_at=?
                WHERE id=? AND owner=? AND state IN ('processing','sending','unknown')
                AND (state!=? OR code!=?)''', (state, code, pages, int(time.time()), ident, owner, state, code))
        return self.get(ident, owner)

    def recover(self):
        with self.connect() as conn:
            conn.execute("UPDATE campus_print_jobs SET state='unknown',code='unknown' WHERE state='sending'")
            conn.execute("UPDATE campus_print_jobs SET state='failed',code='restarted' WHERE state='processing'")
