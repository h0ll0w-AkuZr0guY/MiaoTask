import os
import ssl
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler

from backend.core.plugins import networking
from backend.core.plugins.networking import (PROXY_ENV, fetch_with_retry, network_hint,
                                             normalize_proxy, official_opener, proxy_label, resolve_proxies)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise ValueError('no redirects')


class NormalizeProxyTests(unittest.TestCase):
    def test_direct_tokens_disable_proxy(self):
        for raw in ('', 'off', 'OFF', 'direct', 'none', '0', 'false'):
            self.assertEqual(normalize_proxy(raw), {})

    def test_bare_host_port_defaults_to_http(self):
        self.assertEqual(normalize_proxy('192.168.8.119:7893'),
                         {'http': 'http://192.168.8.119:7893',
                          'https': 'http://192.168.8.119:7893'})

    def test_full_url_applies_to_both_schemes(self):
        self.assertEqual(normalize_proxy('http://127.0.0.1:7890'),
                         {'http': 'http://127.0.0.1:7890',
                          'https': 'http://127.0.0.1:7890'})

    def test_https_proxy_url_supported(self):
        self.assertEqual(normalize_proxy('https://proxy.example.com:8443')['https'],
                         'https://proxy.example.com:8443')

    def test_uppercase_scheme_and_trailing_slash_normalized(self):
        self.assertEqual(normalize_proxy('HTTP://127.0.0.1:7890/'),
                         {'http': 'http://127.0.0.1:7890',
                          'https': 'http://127.0.0.1:7890'})

    def test_socks_rejected(self):
        with self.assertRaises(ValueError):
            normalize_proxy('socks5://127.0.0.1:1080')

    def test_missing_port_rejected(self):
        with self.assertRaises(ValueError):
            normalize_proxy('http://127.0.0.1')

    def test_bad_port_rejected(self):
        with self.assertRaises(ValueError):
            normalize_proxy('http://127.0.0.1:99999')

    def test_credentials_kept_but_never_logged(self):
        proxies = normalize_proxy('http://user:secret@127.0.0.1:7890')
        self.assertEqual(proxies, {'http': 'http://user:secret@127.0.0.1:7890',
                                   'https': 'http://user:secret@127.0.0.1:7890'})
        with patch.object(networking, 'resolve_proxies', return_value=proxies):
            with self.assertLogs('backend.core.plugins.networking', level='DEBUG') as logs:
                official_opener(_NoRedirect())
        self.assertIn('127.0.0.1:7890', '\n'.join(logs.output))
        self.assertNotIn('secret', '\n'.join(logs.output))


class ResolveProxiesTests(unittest.TestCase):
    def setUp(self):
        # The saved console preference would otherwise answer for "unset".
        preferences = patch('backend.core.preferences.load_preferences', return_value={})
        self.addCleanup(preferences.stop)
        preferences.start()

    def test_unset_returns_none_for_system_defaults(self):
        env = dict(os.environ)
        env.pop(PROXY_ENV, None)
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(resolve_proxies())

    def test_env_proxy_is_resolved(self):
        with patch.dict(os.environ, {PROXY_ENV: '127.0.0.1:7890'}, clear=False):
            self.assertEqual(resolve_proxies()['https'], 'http://127.0.0.1:7890')

    def test_env_off_forces_direct(self):
        with patch.dict(os.environ, {PROXY_ENV: 'off'}, clear=False):
            self.assertEqual(resolve_proxies(), {})

    def test_invalid_env_value_fails_loudly(self):
        with patch.dict(os.environ, {PROXY_ENV: 'socks5://127.0.0.1:1080'}, clear=False):
            with self.assertRaises(ValueError):
                resolve_proxies()


class OfficialOpenerTests(unittest.TestCase):
    def _proxy_handlers(self, opener):
        return [handler for handler in opener.handlers if isinstance(handler, ProxyHandler)]

    def test_default_opener_builds_with_redirect_guard(self):
        with patch.object(networking, 'resolve_proxies', return_value=None):
            opener = official_opener(_NoRedirect())
        self.assertTrue(any(isinstance(handler, _NoRedirect) for handler in opener.handlers))

    def test_explicit_proxy_is_installed_on_opener(self):
        mapping = {'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}
        with patch.object(networking, 'resolve_proxies', return_value=mapping):
            opener = official_opener(_NoRedirect())
        handlers = self._proxy_handlers(opener)
        self.assertEqual(handlers[-1].proxies, mapping)

    def test_direct_mapping_drops_proxy_handling(self):
        # An empty ProxyHandler registers no scheme openers and is discarded,
        # replacing the default system-proxy handler with a direct connection.
        with patch.object(networking, 'resolve_proxies', return_value={}):
            opener = official_opener(_NoRedirect())
        self.assertEqual(self._proxy_handlers(opener), [])

    def test_proxy_choice_is_not_logged_at_info(self):
        # One catalog refresh opens dozens of connections; INFO spam drowns the
        # console, so the choice stays on DEBUG and only failures are reported.
        mapping = {'http': 'http://192.0.2.10:7893', 'https': 'http://192.0.2.10:7893'}
        with patch.object(networking, 'resolve_proxies', return_value=mapping):
            with self.assertNoLogs('backend.core.plugins.networking', level='INFO'):
                official_opener(_NoRedirect())


class ProxyLabelTests(unittest.TestCase):
    def test_label_drops_credentials_and_stray_paths(self):
        self.assertEqual(proxy_label('http://user:secret@192.0.2.10:7893'), 'http://192.0.2.10:7893')
        self.assertEqual(proxy_label('192.0.2.10:7893'), 'http://192.0.2.10:7893')
        self.assertEqual(proxy_label('off'), '')


class FetchRetryTests(unittest.TestCase):
    """A LAN proxy drops the odd handshake; one hiccup must not kill a refresh."""

    def test_transient_tls_failure_is_retried(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise URLError(ssl.SSLEOFError('EOF occurred in violation of protocol'))
            return b'payload'

        self.assertEqual(fetch_with_retry(flaky, pause=0), b'payload')
        self.assertEqual(len(attempts), 3)

    def test_server_answer_is_not_retried(self):
        attempts = []

        def missing():
            attempts.append(1)
            raise HTTPError('https://api.github.com/x', 404, 'Not Found', {}, None)

        with self.assertRaises(HTTPError):
            fetch_with_retry(missing, pause=0)
        self.assertEqual(len(attempts), 1)

    def test_validation_errors_surface_immediately(self):
        attempts = []

        def rejected():
            attempts.append(1)
            raise ValueError('活动下载发生不受信任的重定向')

        with self.assertRaises(ValueError):
            fetch_with_retry(rejected, pause=0)
        self.assertEqual(len(attempts), 1)

    def test_failure_hint_names_the_saved_proxy_without_credentials(self):
        mapping = {'http': 'http://user:secret@192.0.2.10:7893',
                   'https': 'http://user:secret@192.0.2.10:7893'}
        with patch.object(networking, 'resolve_proxies', return_value=mapping):
            hint = network_hint(URLError('handshake timeout'))
        self.assertIn('192.0.2.10:7893', hint)
        self.assertIn('直连', hint)
        self.assertNotIn('secret', hint)

    def test_hint_is_silent_for_direct_mode_and_bad_input(self):
        with patch.object(networking, 'resolve_proxies', return_value={}):
            self.assertEqual(network_hint(URLError('boom')), '')
        self.assertEqual(network_hint(ValueError('活动下载内容过大')), '')


if __name__ == '__main__':
    unittest.main()
