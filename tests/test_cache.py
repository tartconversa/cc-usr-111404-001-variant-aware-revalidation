"""核对HTTP缓存语义、变体选择、304合并、失效规则与落盘恢复。"""
import json
import tempfile
import unittest
from pathlib import Path
from app.cache import (RepresentationCache, evaluate_freshness, normalize_field_value,
                       normalized_request_headers, parse_cache_control, parse_http_date,
                       parse_vary)

ZH_BODY = '中文摘要'.encode('utf-8')
EN_BODY = b'english summary'


def zh_headers(**extra):
    headers = [('Content-Type', 'text/plain; charset=utf-8'), ('ETag', '"zh-v1"'),
               ('Vary', 'Accept-Language'), ('Cache-Control', 'max-age=60')]
    for name, value in extra.items():
        headers.append((name.replace('_', '-'), value))
    return headers


class ParseTests(unittest.TestCase):
    def test_cache_control_directives(self):
        parsed = parse_cache_control('public, max-age=60, no-cache="Set-Cookie", X-Flag')
        self.assertEqual(parsed['public'], True)
        self.assertEqual(parsed['max-age'], '60')
        self.assertEqual(parsed['no-cache'], 'Set-Cookie')
        self.assertEqual(parsed['x-flag'], True)
        self.assertEqual(parse_cache_control(None), {})

    def test_http_date_formats(self):
        expected = 784111777.0
        self.assertEqual(parse_http_date('Sun, 06 Nov 1994 08:49:37 GMT'), expected)
        self.assertEqual(parse_http_date('Sunday, 06-Nov-94 08:49:37 GMT'), expected)
        self.assertEqual(parse_http_date('Sun Nov  6 08:49:37 1994'), expected)
        for bad in ('', 'garbage', '1994-11-06'):
            self.assertIsNone(parse_http_date(bad))

    def test_vary_parse(self):
        self.assertEqual(parse_vary('Accept-Language, accept, Accept-Language'),
                         (('accept-language', 'accept'), False))
        self.assertEqual(parse_vary('*'), ((), True))
        self.assertEqual(parse_vary(None), ((), False))

    def test_field_value_normalization(self):
        self.assertEqual(normalize_field_value('accept-language', ' ZH , EN '), 'zh,en')
        self.assertEqual(normalize_field_value('x-custom', '  Keep Case '), 'Keep Case')


class FreshnessTests(unittest.TestCase):
    def test_max_age_with_date_and_resident_time(self):
        headers = [('Cache-Control', 'max-age=200'), ('Date', 'Thu, 01 Jan 1970 00:15:00 GMT')]
        fresh = evaluate_freshness(headers, stored_at=1000.0, now=1050.0)
        self.assertEqual(fresh.basis, 'max-age')
        self.assertEqual(fresh.current_age, 150.0)
        self.assertTrue(fresh.fresh)
        stale = evaluate_freshness(headers, stored_at=1000.0, now=1300.0)
        self.assertEqual(stale.current_age, 400.0)
        self.assertFalse(stale.fresh)

    def test_age_header_raises_current_age(self):
        headers = [('Cache-Control', 'max-age=200'), ('Age', '250'),
                   ('Date', 'Thu, 01 Jan 1970 00:15:00 GMT')]
        self.assertFalse(evaluate_freshness(headers, 1000.0, 1000.0).fresh)

    def test_expires_and_invalid_expires(self):
        headers = [('Date', 'Thu, 01 Jan 1970 00:15:00 GMT'),
                   ('Expires', 'Thu, 01 Jan 1970 00:20:00 GMT')]
        fresh = evaluate_freshness(headers, 1000.0, 1000.0)
        self.assertEqual(fresh.basis, 'expires')
        self.assertEqual(fresh.lifetime, 300.0)
        invalid = evaluate_freshness([('Expires', '0')], 1000.0, 1000.0)
        self.assertEqual(invalid.lifetime, 0.0)
        self.assertFalse(invalid.fresh)

    def test_no_basis_and_no_cache_never_fresh(self):
        self.assertFalse(evaluate_freshness([], 1000.0, 1000.0).fresh)
        self.assertEqual(evaluate_freshness([], 1000.0, 1000.0).basis, 'none')
        no_cache = evaluate_freshness([('Cache-Control', 'max-age=200, no-cache')], 1000.0, 1000.0)
        self.assertFalse(no_cache.fresh)
        self.assertTrue(no_cache.no_cache)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = RepresentationCache(self.tmp.name)
        self.url = 'http://source.test/news'
        self.scope = 'scope:partner-a'
        self.request = [('Accept-Language', 'zh')]

    def store_zh(self, now=1000.0, headers=None, body=ZH_BODY, base=None):
        return self.cache.store(self.scope, self.url, self.request, status=200,
                                headers=headers or zh_headers(), body=body, now=now,
                                base_variant=base, shared=False, had_auth=True)

    def test_store_hit_and_persistence_recovery(self):
        result = self.store_zh()
        self.assertTrue(result.stored)
        lookup = self.cache.lookup(self.scope, self.url, self.request, 1030.0)
        self.assertEqual(lookup.kind, 'hit')
        self.assertEqual(lookup.variant.request_headers, {'accept-language': 'zh'})
        recovered = RepresentationCache(self.tmp.name)
        lookup = recovered.lookup(self.scope, self.url, self.request, 1030.0)
        self.assertEqual(lookup.kind, 'hit')
        self.assertEqual(recovered.read_body(lookup.variant.body_sha256), ZH_BODY)
        stale = recovered.lookup(self.scope, self.url, self.request, 2000.0)
        self.assertEqual(stale.kind, 'stale')
        self.assertEqual(stale.reason, 'stale')
        self.assertEqual(stale.variant.validators()['If-None-Match'], '"zh-v1"')

    def test_variant_selection_by_vary(self):
        self.store_zh()
        miss = self.cache.lookup(self.scope, self.url, [('Accept-Language', 'en')], 1000.0)
        self.assertEqual(miss.kind, 'miss')
        self.assertEqual(miss.reason, 'no-variant-match')
        hit = self.cache.lookup(self.scope, self.url, [('ACCEPT-LANGUAGE', ' ZH ')], 1000.0)
        self.assertEqual(hit.kind, 'hit')

    def test_no_freshness_basis_is_stale_but_revalidatable(self):
        headers = [('ETag', '"x"'), ('Last-Modified', 'Thu, 01 Jan 1970 00:00:00 GMT')]
        self.cache.store(self.scope, self.url, [], status=200, headers=headers, body=b'x',
                         now=1000.0, base_variant=None, shared=False, had_auth=False)
        lookup = self.cache.lookup(self.scope, self.url, [], 1000.0)
        self.assertEqual(lookup.kind, 'stale')
        self.assertEqual(lookup.reason, 'no-freshness-basis')
        self.assertEqual(lookup.variant.validators(),
                         {'If-None-Match': '"x"',
                          'If-Modified-Since': 'Thu, 01 Jan 1970 00:00:00 GMT'})

    def test_merge_304_replaces_and_retains_headers(self):
        self.store_zh()
        lookup = self.cache.lookup(self.scope, self.url, self.request, 2000.0)
        merge = self.cache.merge_304(self.scope, self.url, variant_id=lookup.variant.id,
                                     base_seq=lookup.variant.seq,
                                     headers=[('Cache-Control', 'max-age=500'), ('X-New', '2')],
                                     now=2000.0)
        self.assertEqual(merge.outcome, 'merged')
        names = {k.lower(): v for k, v in merge.variant.headers}
        self.assertEqual(names['etag'], '"zh-v1"')
        self.assertEqual(names['cache-control'], 'max-age=500')
        self.assertEqual(names['x-new'], '2')
        self.assertEqual(self.cache.read_body(merge.variant.body_sha256), ZH_BODY)
        hit = self.cache.lookup(self.scope, self.url, self.request, 2100.0)
        self.assertEqual(hit.kind, 'hit')

    def test_merge_304_vary_change_invalidates(self):
        self.store_zh()
        lookup = self.cache.lookup(self.scope, self.url, self.request, 2000.0)
        merge = self.cache.merge_304(self.scope, self.url, variant_id=lookup.variant.id,
                                     base_seq=lookup.variant.seq,
                                     headers=[('Vary', 'Accept')], now=2000.0)
        self.assertEqual(merge.outcome, 'vary-changed')
        self.assertEqual(self.cache.lookup(self.scope, self.url, self.request, 2000.0).kind, 'miss')

    def test_merge_304_vary_star_invalidates(self):
        self.cache.store(self.scope, self.url, [], status=200,
                         headers=[('ETag', '"x"'), ('Cache-Control', 'max-age=60')], body=b'x',
                         now=1000.0, base_variant=None, shared=False, had_auth=False)
        lookup = self.cache.lookup(self.scope, self.url, [], 2000.0)
        merge = self.cache.merge_304(self.scope, self.url, variant_id=lookup.variant.id,
                                     base_seq=lookup.variant.seq,
                                     headers=[('Vary', '*')], now=2000.0)
        self.assertEqual(merge.outcome, 'vary-changed')
        self.assertEqual(self.cache.lookup(self.scope, self.url, [], 2000.0).kind, 'miss')

    def test_merge_304_no_store_invalidates(self):
        self.store_zh()
        lookup = self.cache.lookup(self.scope, self.url, self.request, 2000.0)
        merge = self.cache.merge_304(self.scope, self.url, variant_id=lookup.variant.id,
                                     base_seq=lookup.variant.seq,
                                     headers=[('Cache-Control', 'no-store')], now=2000.0)
        self.assertEqual(merge.outcome, 'no-store')
        self.assertEqual(self.cache.lookup(self.scope, self.url, self.request, 2000.0).kind, 'miss')

    def test_late_merge_304_cannot_overwrite_newer(self):
        self.store_zh()
        lookup = self.cache.lookup(self.scope, self.url, self.request, 2000.0)
        merge = self.cache.merge_304(self.scope, self.url, variant_id=lookup.variant.id,
                                     base_seq=lookup.variant.seq + 99,
                                     headers=[('Cache-Control', 'max-age=500')], now=2000.0)
        self.assertEqual(merge.outcome, 'superseded')
        gone = self.cache.merge_304(self.scope, self.url, variant_id='v-absent',
                                    base_seq=1, headers=[], now=2000.0)
        self.assertEqual(gone.outcome, 'gone')

    def test_late_store_cannot_overwrite_newer(self):
        first = self.store_zh()
        lookup = self.cache.lookup(self.scope, self.url, self.request, 1000.0)
        newer = self.store_zh(headers=zh_headers(ETag='"zh-v2"'), body='中文摘要v2'.encode(),
                              base=lookup.variant)
        self.assertTrue(newer.stored)
        late = self.store_zh(headers=zh_headers(), body=b'stale-body', base=lookup.variant)
        self.assertFalse(late.stored)
        self.assertEqual(late.reason, 'superseded')
        hit = self.cache.lookup(self.scope, self.url, self.request, 1001.0)
        self.assertEqual(self.cache.read_body(hit.variant.body_sha256), '中文摘要v2'.encode())
        self.assertEqual(hit.variant.id, newer.variant_id)
        self.assertNotEqual(hit.variant.id, first.variant_id)

    def test_store_vary_change_drops_old_variant(self):
        self.store_zh()
        headers = [('ETag', '"a1"'), ('Vary', 'Accept'), ('Cache-Control', 'max-age=60')]
        result = self.cache.store(self.scope, self.url, self.request, status=200,
                                  headers=headers, body=b'by-accept', now=1001.0,
                                  base_variant=None, shared=False, had_auth=True)
        self.assertTrue(result.stored)
        record = json.loads((Path(self.tmp.name) / self.cache.record_relpath(self.scope, self.url))
                            .read_text(encoding='utf-8'))
        self.assertEqual(len(record['variants']), 1)
        self.assertEqual(record['variants'][0]['vary'], ['accept'])

    def test_no_store_response_invalidates_matching(self):
        self.store_zh()
        result = self.cache.store(self.scope, self.url, self.request, status=200,
                                  headers=[('Cache-Control', 'no-store')], body=b'x', now=1001.0,
                                  base_variant=None, shared=False, had_auth=True)
        self.assertFalse(result.stored)
        self.assertEqual(result.reason, 'no-store')
        self.assertEqual(self.cache.lookup(self.scope, self.url, self.request, 1001.0).kind, 'miss')

    def test_unstorable_responses(self):
        cases = [([('Vary', '*')], 'vary-star'),
                 ([('Vary', 'Authorization')], 'vary-sensitive'),
                 ([], 'not-cacheable-status')]
        for headers, reason in cases:
            with self.subTest(reason=reason):
                result = self.cache.store(self.scope, self.url, [], status=500, headers=headers,
                                          body=b'x', now=1000.0, base_variant=None,
                                          shared=False, had_auth=False)
                self.assertEqual(result.reason, reason)
        ok = self.cache.store(self.scope, self.url, [], status=500,
                              headers=[('Cache-Control', 'max-age=60')], body=b'x', now=1000.0,
                              base_variant=None, shared=False, had_auth=False)
        self.assertTrue(ok.stored)

    def test_shared_scope_requires_http_permission(self):
        denied = self.cache.store('shared:news', self.url, [], status=200,
                                  headers=[('Cache-Control', 'max-age=60')], body=b'x', now=1000.0,
                                  base_variant=None, shared=True, had_auth=True)
        self.assertEqual(denied.reason, 'shared-auth')
        private = self.cache.store('shared:news', self.url, [], status=200,
                                   headers=[('Cache-Control', 'max-age=60, public, private')],
                                   body=b'x', now=1000.0, base_variant=None,
                                   shared=True, had_auth=False)
        self.assertEqual(private.reason, 'shared-private')
        allowed = self.cache.store('shared:news', self.url, [], status=200,
                                   headers=[('Cache-Control', 'max-age=60, public')], body=b'x',
                                   now=1000.0, base_variant=None, shared=True, had_auth=True)
        self.assertTrue(allowed.stored)

    def test_corrupt_record_quarantined(self):
        self.store_zh()
        path = Path(self.tmp.name) / self.cache.record_relpath(self.scope, self.url)
        path.write_text('{broken', encoding='utf-8')
        self.assertEqual(self.cache.lookup(self.scope, self.url, self.request, 1000.0).kind, 'miss')
        self.assertTrue(path.with_name(path.name + '.bad').is_file())

    def test_tampered_body_quarantined(self):
        self.store_zh()
        lookup = self.cache.lookup(self.scope, self.url, self.request, 1000.0)
        body_path = Path(self.tmp.name) / self.cache.body_relpath(lookup.variant.body_sha256)
        body_path.write_bytes(b'tampered')
        self.assertIsNone(self.cache.read_body(lookup.variant.body_sha256))
        self.assertTrue(body_path.with_name(body_path.name + '.bad').is_file())

    def test_scope_isolation_on_disk(self):
        self.store_zh()
        other = self.cache.store('scope:partner-b', self.url, self.request, status=200,
                                 headers=zh_headers(), body=ZH_BODY, now=1000.0,
                                 base_variant=None, shared=False, had_auth=True)
        self.assertTrue(other.stored)
        self.assertNotEqual(self.cache.record_relpath('scope:partner-a', self.url),
                            self.cache.record_relpath('scope:partner-b', self.url))
        self.assertEqual(self.cache.lookup('scope:partner-b', self.url, self.request, 1000.0).kind,
                         'hit')


if __name__ == '__main__':
    unittest.main()
