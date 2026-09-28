"""Run the actual MAXCOURSE application locally with an isolated database.

No simulated users, authentication or printer. The real print integration is
closed by default until MAXCOURSE_PRINT_* is configured for an actual agent.
"""
import argparse
import os
from pathlib import Path
import secrets
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=5019)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix='maxcourse-print-local-') as folder:
        # app.py initializes its database at import time, relative to cwd.
        os.chdir(folder)
        os.environ['MAXCOURSE_SECRET_KEY'] = secrets.token_hex(32)
        os.environ['SHARED_AUTH_DB'] = str(Path(folder) / 'shared-auth.sqlite3')
        import app as application
        application.DB_PATH = str(Path(folder) / 'maxcourse.db')
        application.app.config.update(SESSION_COOKIE_NAME='maxcourse_print_local')
        print(f'Local application: http://127.0.0.1:{args.port}/print/', flush=True)
        application.app.run(host='127.0.0.1', port=args.port, use_reloader=False)


if __name__ == '__main__':
    main()
