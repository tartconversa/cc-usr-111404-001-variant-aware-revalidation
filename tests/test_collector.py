"""核对采集编排：变体隔离、条件重验、证据追溯、跳转授权、单飞合并与取消。"""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from app.collector import CollectCancelled, Collector, SourceConfig
from app.service import collect

ZH_BODY = '中文摘要'.encode('utf-8')
EN_BODY = b'english summary'


class ScriptedServer:
    """按回调生成响应的回环来源；记录每个请求的方法、路径与头。"""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []
        self.lock = threading.Lock()

    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                with owner.lock:
                    owner.requests.append({'path': self.path,
                                           'headers': list(self.headers.items())})
                status, headers, body = owner.handler(
                    {k.lower(): v for k, v in self.headers.items()})
                self.send_response_only(status)
                for key, value in headers:
                    self.send_header(key, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        if self.thread.is_alive():
            raise RuntimeError('来源服务未停止')

    def header_of(self, index, name):
        for key, value in self.requests[index]['headers']:
            if key.lower() == name:
                return value
        return None


def variant_handler(request):
    """按Accept-Language协商两个变体；收到匹配ETag回304。"""
    lang = request.get('accept-language', 'zh')
    body, etag = (ZH_BODY, '"zh-v1"') if lang == 'zh' else (EN_BODY, '"en-v1"')
    headers = [('Content-Type', 'text/plain; charset=utf-8'), ('ETag', etag),
               ('Vary', 'Accept-Language'), ('Cache-Control', 'max-age=60')]
    if request.get('if-none-match') == etag:
        return 304, headers, b''
    return 200, headers, body


def config_for(url, lang='zh', scope='partner-a', extra_headers=(), **overrides):
    data = {'source_id': 'daily', 'credential_scope': scope, 'url': url,
            'headers': [['Accept-Language', lang]] + [list(pair) for pair in extra_headers]}
    data.update(overrides)
    return data


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_variant_isolation_and_conditional_revalidation(self):
        with ScriptedServer(variant_handler) as server:
            collector = Collector(self.tmp.name)
            zh = config_for(server.url + '/news', 'zh')
            en = config_for(server.url + '/news', 'en')
            first = collector.collect(zh, now=1000.0)
            self.assertTrue(first.ok)
            self.assertEqual(first.body, ZH_BODY)
            other = collector.collect(en, now=1000.0)
            self.assertEqual(other.body, EN_BODY)
            self.assertNotEqual(first.body_sha256, other.body_sha256)
            hit = collector.collect(zh, now=1030.0)
            self.assertEqual(hit.body, ZH_BODY)
            self.assertEqual(len(server.requests), 2)
            self.assertEqual(hit.evidence['cache']['decision'], 'hit')
            self.assertEqual(hit.evidence['freshness']['granted_by'], 'stored-response')
            self.assertEqual(hit.evidence['body']['origin'], 'stored-variant')
            self.assertEqual(hit.evidence['variant']['request_headers'],
                             {'accept-language': 'zh'})
            revalidated = collector.collect(zh, now=2000.0)
            self.assertEqual(revalidated.body, ZH_BODY)
            self.assertEqual(len(server.requests), 3)
            self.assertEqual(server.header_of(2, 'if-none-match'), '"zh-v1"')
            self.assertEqual(revalidated.evidence['cache']['decision'], 'revalidated')
            self.assertEqual(revalidated.evidence['freshness']['granted_by'],
                             'revalidation-304')
            self.assertEqual(revalidated.evidence['revalidation']['status'], 304)
            self.assertEqual(revalidated.evidence['body']['origin'], 'stored-variant')
            self.assertEqual(revalidated.body_sha256, first.body_sha256)

    def test_persistence_recovery_across_collectors(self):
        with ScriptedServer(variant_handler) as server:
            zh = config_for(server.url + '/news', 'zh')
            Collector(self.tmp.name).collect(zh, now=1000.0)
            recovered = Collector(self.tmp.name)
            hit = recovered.collect(zh, now=1030.0)
            self.assertTrue(hit.ok)
            self.assertEqual(hit.body, ZH_BODY)
            self.assertEqual(len(server.requests), 1)
            self.assertEqual(hit.evidence['cache']['decision'], 'hit')

    def test_304_without_entity_never_fabricates_body(self):
        state = {'phase': 'initial'}

        def handler(request):
            if state['phase'] == 'initial':
                return 200, [('ETag', '"zh-v1"'), ('Vary', 'Accept-Language'),
                             ('Cache-Control', 'max-age=60')], ZH_BODY
            if 'if-none-match' in request:
                return 304, [('ETag', '"zh-v1"'), ('Vary', 'Accept-Language'),
                             ('Cache-Control', 'max-age=60')], b''
            if state['phase'] == 'recover':
                return 200, [('ETag', '"zh-v1"'), ('Vary', 'Accept-Language'),
                             ('Cache-Control', 'max-age=60')], ZH_BODY
            return 304, [('ETag', '"zh-v1"')], b''

        with ScriptedServer(handler) as server:
            collector = Collector(self.tmp.name)
            zh = config_for(server.url + '/news', 'zh')
            collector.collect(zh, now=1000.0)
            self.assertEqual(len(server.requests), 1)
            for body_file in (Path(self.tmp.name) / 'bodies').iterdir():
                body_file.unlink()
            state['phase'] = 'recover'
            recovered = collector.collect(zh, now=1030.0)
            self.assertTrue(recovered.ok)
            self.assertEqual(recovered.body, ZH_BODY)
            self.assertEqual(recovered.evidence['cache']['unconditional_refetch'],
                             'entity-missing')
            self.assertEqual(len(server.requests), 3)
            self.assertIsNone(server.header_of(2, 'if-none-match'))
            refilled = collector.collect(zh, now=1031.0)
            self.assertEqual(refilled.evidence['cache']['decision'], 'hit')
            self.assertEqual(len(server.requests), 3)
            state['phase'] = 'broken'
            for body_file in (Path(self.tmp.name) / 'bodies').iterdir():
                if not body_file.name.endswith('.bad'):
                    body_file.unlink()
            broken = collector.collect(zh, now=1040.0)
            self.assertFalse(broken.ok)
            self.assertIsNone(broken.body)
            self.assertIn('304', broken.evidence['error']['message'])

    def test_vary_change_on_304_forces_unconditional_refetch(self):
        state = {'phase': 0}

        def handler(request):
            if state['phase'] == 0:
                return 200, [('ETag', '"v1"'), ('Vary', 'Accept-Language'),
                             ('Cache-Control', 'max-age=60')], b'old-body'
            if 'if-none-match' in request:
                return 304, [('ETag', '"v1"'), ('Vary', 'Accept'),
                             ('Cache-Control', 'max-age=60')], b''
            return 200, [('ETag', '"v2"'), ('Vary', 'Accept'),
                         ('Cache-Control', 'max-age=60')], b'new-body'

        with ScriptedServer(handler) as server:
            collector = Collector(self.tmp.name)
            zh = config_for(server.url + '/news', 'zh')
            self.assertEqual(collector.collect(zh, now=1000.0).body, b'old-body')
            state['phase'] = 1
            delivered = collector.collect(zh, now=2000.0)
            self.assertEqual(delivered.body, b'new-body')
            self.assertEqual(delivered.evidence['cache']['unconditional_refetch'],
                             'vary-changed')
            self.assertEqual(delivered.evidence['variant']['vary'], ['accept'])
            self.assertEqual(len(server.requests), 3)

    def test_no_store_on_304_drops_qualification(self):
        state = {'phase': 0}

        def handler(request):
            if state['phase'] == 0:
                return 200, [('ETag', '"v1"'), ('Cache-Control', 'max-age=60')], b'body-1'
            if 'if-none-match' in request:
                return 304, [('Cache-Control', 'no-store')], b''
            return 200, [('Cache-Control', 'no-store')], b'body-2'

        with ScriptedServer(handler) as server:
            collector = Collector(self.tmp.name)
            cfg = config_for(server.url + '/news', 'zh')
            self.assertEqual(collector.collect(cfg, now=1000.0).body, b'body-1')
            state['phase'] = 1
            delivered = collector.collect(cfg, now=2000.0)
            self.assertEqual(delivered.body, b'body-2')
            self.assertFalse(delivered.evidence['cache']['stored'])
            self.assertEqual(delivered.evidence['cache']['store_reason'], 'no-store')
            again = collector.collect(cfg, now=2001.0)
            self.assertEqual(again.body, b'body-2')
            self.assertEqual(len(server.requests), 4)

    def test_identity_scopes_isolated_by_default(self):
        with ScriptedServer(variant_handler) as server:
            collector = Collector(self.tmp.name)
            first = collector.collect(config_for(server.url + '/news', 'zh', 'partner-a'),
                                      now=1000.0)
            second = collector.collect(config_for(server.url + '/news', 'zh', 'partner-b'),
                                       now=1000.0)
            self.assertEqual(len(server.requests), 2)
            self.assertNotEqual(first.evidence['cache']['scope_key'],
                                second.evidence['cache']['scope_key'])
            self.assertNotEqual(first.evidence['cache']['record'],
                                second.evidence['cache']['record'])
            hit_a = collector.collect(config_for(server.url + '/news', 'zh', 'partner-a'),
                                      now=1030.0)
            self.assertEqual(hit_a.evidence['cache']['decision'], 'hit')
            self.assertEqual(len(server.requests), 2)

    def test_credentials_never_in_evidence_or_on_disk(self):
        secret = 'Bearer secret-zh-token'
        with ScriptedServer(variant_handler) as server:
            collector = Collector(self.tmp.name)
            cfg = config_for(server.url + '/news', 'zh',
                             extra_headers=(('Authorization', secret),))
            delivered = collector.collect(cfg, now=1000.0)
            self.assertTrue(delivered.ok)
            self.assertNotIn('secret-zh-token', json.dumps(delivered.evidence))
            self.assertEqual(delivered.evidence['sensitive_headers_redacted'],
                             ['authorization'])
            self.assertNotIn('authorization',
                             delivered.evidence['configured_request_headers'])
            for path in Path(self.tmp.name).rglob('*'):
                if path.is_file():
                    self.assertNotIn(b'secret-zh-token', path.read_bytes())
            self.assertEqual(server.header_of(0, 'authorization'), secret)

    def test_cross_origin_redirect_strips_authorization(self):
        seen = {}

        def final_handler(request):
            seen['authorization'] = request.get('authorization')
            return 200, [('Cache-Control', 'max-age=60')], b'final-body'

        with ScriptedServer(final_handler) as server_b:
            def start_handler(request):
                return 302, [('Location', server_b.url + '/final')], b''

            with ScriptedServer(start_handler) as server_a:
                collector = Collector(self.tmp.name)
                cfg = config_for(server_a.url + '/start', 'zh',
                                 extra_headers=(('Authorization', 'Bearer hop-token'),))
                delivered = collector.collect(cfg, now=1000.0)
                self.assertTrue(delivered.ok)
                self.assertEqual(delivered.body, b'final-body')
                self.assertIsNone(seen['authorization'])
                redirect = delivered.evidence['redirects'][0]
                self.assertFalse(redirect['credentials_forwarded'])
                self.assertEqual(delivered.evidence['effective_url'], server_b.url + '/final')

    def test_same_origin_redirect_keeps_authorization(self):
        seen = {}

        def handler(request):
            seen.setdefault('count', 0)
            seen['count'] += 1
            if seen['count'] == 1:
                seen['first_auth'] = request.get('authorization')
                return 302, [('Location', '/final')], b''
            seen['second_auth'] = request.get('authorization')
            return 200, [('Cache-Control', 'max-age=60')], b'local-final'

        with ScriptedServer(handler) as server:
            collector = Collector(self.tmp.name)
            cfg = config_for(server.url + '/start', 'zh',
                             extra_headers=(('Authorization', 'Bearer same-token'),))
            delivered = collector.collect(cfg, now=1000.0)
            self.assertEqual(delivered.body, b'local-final')
            self.assertEqual(seen['second_auth'], 'Bearer same-token')
            self.assertTrue(delivered.evidence['redirects'][0]['credentials_forwarded'])

    def test_parallel_collects_merge_into_one_transfer(self):
        release = threading.Event()

        def handler(request):
            release.wait(5)
            return 200, [('ETag', '"x"'), ('Vary', 'Accept-Language'),
                         ('Cache-Control', 'max-age=60')], b'shared-body'

        with ScriptedServer(handler) as server:
            collector = Collector(self.tmp.name)
            cfg = config_for(server.url + '/news', 'zh')
            results, errors = [], []

            def work():
                try:
                    results.append(collector.collect(cfg, now=1000.0))
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            threads = [threading.Thread(target=work) for _ in range(4)]
            for thread in threads:
                thread.start()
            threading.Event().wait(0.3)
            release.set()
            for thread in threads:
                thread.join(5)
            self.assertFalse(errors)
            self.assertEqual(len(results), 4)
            self.assertTrue(all(item.ok and item.body == b'shared-body' for item in results))
            self.assertEqual(len(server.requests), 1)
            roles = sorted(item.evidence['flight']['role'] for item in results)
            self.assertEqual(roles, ['follower', 'follower', 'follower', 'leader'])

    def test_cancelling_one_waiter_does_not_fail_others(self):
        release = threading.Event()

        def handler(request):
            release.wait(5)
            return 200, [('Cache-Control', 'max-age=60')], b'group-body'

        with ScriptedServer(handler) as server:
            collector = Collector(self.tmp.name)
            cfg = config_for(server.url + '/news', 'zh')
            cancels = [threading.Event() for _ in range(3)]
            outcomes = [None] * 3

            def work(index):
                try:
                    outcomes[index] = ('ok', collector.collect(
                        cfg, now=1000.0, cancel_event=cancels[index]))
                except CollectCancelled:
                    outcomes[index] = ('cancelled', None)

            threads = [threading.Thread(target=work, args=(i,)) for i in range(3)]
            for thread in threads:
                thread.start()
            threading.Event().wait(0.2)
            cancels[1].set()
            threading.Event().wait(0.2)
            release.set()
            for thread in threads:
                thread.join(5)
            states = sorted(outcome[0] for outcome in outcomes)
            self.assertEqual(states, ['cancelled', 'ok', 'ok'])
            for state, delivery in outcomes:
                if state == 'ok':
                    self.assertEqual(delivery.body, b'group-body')
            self.assertEqual(len(server.requests), 1)

    def test_late_revalidation_cannot_overwrite_newer_body(self):
        gate = threading.Event()

        def handler(request):
            if 'if-none-match' in request:
                gate.wait(5)
                return 304, [('ETag', '"v1"'), ('Cache-Control', 'max-age=60')], b''
            return 200, [('ETag', '"v1"'), ('Cache-Control', 'max-age=60')], b'body-v1'

        with ScriptedServer(handler) as server:
            collector = Collector(self.tmp.name)
            cfg = config_for(server.url + '/news', 'zh')
            self.assertEqual(collector.collect(cfg, now=1000.0).body, b'body-v1')
            outcome = []

            def revalidate():
                outcome.append(collector.collect(cfg, now=2000.0))

            thread = threading.Thread(target=revalidate)
            thread.start()
            threading.Event().wait(0.2)
            current = collector.cache.lookup('scope:partner-a', server.url + '/news',
                                             [('Accept-Language', 'zh')], 2100.0)
            newer = collector.cache.store('scope:partner-a', server.url + '/news',
                                          [('Accept-Language', 'zh')], status=200,
                                          headers=[('ETag', '"v2"'),
                                                   ('Cache-Control', 'max-age=3600')],
                                          body=b'body-v2', now=2100.0,
                                          base_variant=current.variant, shared=False,
                                          had_auth=False)
            self.assertTrue(newer.stored)
            gate.set()
            thread.join(5)
            self.assertEqual(outcome[0].body, b'body-v2')
            recovered = Collector(self.tmp.name)
            hit = recovered.collect(cfg, now=2200.0)
            self.assertEqual(hit.body, b'body-v2')
            self.assertEqual(hit.evidence['cache']['decision'], 'hit')

    def test_only_if_cached_never_touches_network(self):
        with ScriptedServer(variant_handler) as server:
            collector = Collector(self.tmp.name)
            cfg = config_for(server.url + '/news', 'zh',
                             extra_headers=(('Cache-Control', 'only-if-cached'),))
            missed = collector.collect(cfg, now=1000.0)
            self.assertFalse(missed.ok)
            self.assertIn('only-if-cached', missed.evidence['error']['message'])
            self.assertEqual(len(server.requests), 0)
            collector.collect(config_for(server.url + '/news', 'zh'), now=1000.0)
            hit = collector.collect(cfg, now=1030.0)
            self.assertTrue(hit.ok)
            self.assertEqual(hit.body, ZH_BODY)
            self.assertEqual(len(server.requests), 1)

    def test_request_no_store_bypasses_cache(self):
        with ScriptedServer(variant_handler) as server:
            collector = Collector(self.tmp.name)
            cfg = config_for(server.url + '/news', 'zh',
                             extra_headers=(('Cache-Control', 'no-store'),))
            first = collector.collect(cfg, now=1000.0)
            self.assertTrue(first.ok)
            self.assertEqual(first.evidence['cache']['lookup'], 'bypassed')
            self.assertFalse(first.evidence['cache']['stored'])
            collector.collect(cfg, now=1001.0)
            self.assertEqual(len(server.requests), 2)

    def test_config_validation(self):
        for bad in ({}, {'source_id': 'x'},
                    {'source_id': 'x', 'credential_scope': 's', 'url': 'ftp://x/y'},
                    {'source_id': 'x', 'credential_scope': 's', 'url': 'http://x/y',
                     'headers': [['A', 'bad\r\nB: c']]},
                    {'source_id': 'x', 'credential_scope': 's', 'url': 'http://x/y',
                     'max_redirects': -1}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    SourceConfig.from_dict(bad)

    def test_service_collect_entry(self):
        with ScriptedServer(variant_handler) as server:
            delivery = collect(config_for(server.url + '/news', 'en'), self.tmp.name, now=1000.0)
            self.assertTrue(delivery.ok)
            self.assertEqual(delivery.body, EN_BODY)
            self.assertEqual(delivery.evidence['cache']['decision'], 'fetched')


if __name__ == '__main__':
    unittest.main()
