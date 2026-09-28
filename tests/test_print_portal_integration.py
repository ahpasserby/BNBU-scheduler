import os
import tempfile
import unittest

os.environ.setdefault('MAXCOURSE_SECRET_KEY', 'test-secret-key')
import app as app_module


class PrintPortalIntegrationTests(unittest.TestCase):
    def test_portal_entrypoint_assets_and_private_source_boundary(self):
        previous_db = app_module.DB_PATH
        previous_testing = app_module.app.testing
        previous_enabled = app_module.app.config.get('PRINT_ENABLED')
        try:
            with tempfile.TemporaryDirectory() as folder:
                app_module.DB_PATH = os.path.join(folder, 'integration.sqlite3')
                app_module.init_db()
                app_module.app.config.update(TESTING=True, PRINT_ENABLED=False)
                client = app_module.app.test_client()
                headers = {'User-Agent': 'Mozilla/5.0 Print portal integration'}
                for path in ('/print/', '/print/index.html', '/print/print.css', '/print/print.js'):
                    self.assertEqual(client.get(path, headers=headers).status_code, 200, path)
                state = client.get('/api/print/session', headers=headers)
                self.assertEqual(state.status_code, 200)
                self.assertIsNone(state.json['user'])
                self.assertFalse(state.json['service']['enabled'])
                self.assertFalse(state.json['service']['ready'])
                self.assertIn('no-store', state.headers['Cache-Control'])
                for path in ('/campus_print/agent.py', '/campus_print/api.py', '/deploy/print-portal/install-agent.sh', '/tests/fixtures/print-portal.pdf'):
                    self.assertEqual(client.get(path, headers=headers).status_code, 404, path)
        finally:
            app_module.DB_PATH = previous_db
            app_module.app.testing = previous_testing
            if previous_enabled is None:
                app_module.app.config.pop('PRINT_ENABLED', None)
            else:
                app_module.app.config['PRINT_ENABLED'] = previous_enabled


if __name__ == '__main__':
    unittest.main()
