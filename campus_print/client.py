from urllib.parse import urlsplit
import requests

from .common import PrintError


class AgentClient:
    def __init__(self, url, token, ca_file=None):
        parsed = urlsplit(url)
        # The only supported production path is a private SSH reverse tunnel.
        if parsed.scheme != 'https' or parsed.hostname not in ('127.0.0.1', '::1') or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('', '/'):
            raise ValueError('Print agent URL must be an HTTPS loopback endpoint')
        if len(token) < 32:
            raise ValueError('Print agent token must contain at least 32 characters')
        self.url = url.rstrip('/')
        self.token = token
        self.ca_file = ca_file or True

    def call(self, method, path, payload=None, timeout=3, max_bytes=65536):
        # Do not use proxy environment settings for the private tunnel.
        with requests.Session() as client:
            client.trust_env = False
            response = client.request(method, self.url + path, json=payload,
                headers={'Authorization': 'Bearer ' + self.token}, timeout=(2, timeout), allow_redirects=False, verify=self.ca_file)
            if len(response.content) > max_bytes:
                raise ValueError('Oversized agent response')
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError('Invalid agent response')
            if response.status_code >= 400:
                raise PrintError(data.get('code') if data.get('code') in ('busy','offline','invalid_pdf','encrypted_pdf','too_many_pages','too_large','conflict','rate_limited','auth_rate_limited','active_job','unsupported_format','convert_failed') else 'offline', response.status_code, unaccepted=data.get('accepted') is False)
            if response.status_code != 200:
                raise ValueError('Unexpected agent response')
            return data

    def health(self):
        return self.call('GET', '/v1/health')

    def inspect(self, pdf):
        return self.call('POST', '/v1/inspect', {'pdf': pdf}, 30)

    def submit(self, payload):
        return self.call('POST', '/v1/jobs', payload, 150)

    def convert(self, payload):
        # A converted PDF is returned inline, up to the 10 MiB document limit.
        return self.call('POST', '/v1/convert', payload, 130, max_bytes=16 * 1024 * 1024)

    def job(self, ident):
        return self.call('GET', '/v1/jobs/' + ident)
