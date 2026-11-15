"""采集编排：按来源配置领取表示变体，协调缓存、条件重验、重定向与并发合并。

每次交付都带证据：复用了哪份正文、依据哪个响应获得新鲜度、何时只能重新获取；
凭据只用于请求，不进入缓存落盘与导出证据；跨站跳转不携带原站授权；
并行领取相同变体合并为一次传输，取消一个等待者不影响其他领取者。
"""
from __future__ import annotations

import hashlib
import json
import threading
import traceback
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from . import source as _source
from .cache import (SENSITIVE_REQUEST_HEADERS, Lookup, RepresentationCache,
                    evaluate_freshness, header_get, normalized_request_headers,
                    parse_cache_control)

DEFAULT_MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 1048576
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class CollectCancelled(Exception):
    """等待者取消合并传输；不影响同一传输的其他领取者。"""


@dataclass(frozen=True)
class SourceConfig:
    """来源配置：语言、媒体类型与访问身份都由这里给出。"""
    source_id: str
    credential_scope: str
    url: str
    headers: tuple = ()
    sensitive_headers: tuple = ()
    shared_scope: str | None = None
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    max_bytes: int = DEFAULT_MAX_BYTES

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError('来源配置无效')
        source_id = data.get('source_id')
        if not isinstance(source_id, str) or not source_id:
            raise ValueError('来源身份无效')
        credential_scope = data.get('credential_scope')
        if not isinstance(credential_scope, str) or not credential_scope:
            raise ValueError('身份范围无效')
        url = data.get('url')
        if not isinstance(url, str):
            raise ValueError('来源地址无效')
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https') or not parts.hostname:
            raise ValueError('来源地址无效')
        headers = _parse_header_pairs(data.get('headers', []))
        sensitive = data.get('sensitive_headers', [])
        if not isinstance(sensitive, list) \
                or not all(isinstance(item, str) and item for item in sensitive):
            raise ValueError('敏感头声明无效')
        shared_scope = data.get('shared_scope')
        if shared_scope is not None and (not isinstance(shared_scope, str) or not shared_scope):
            raise ValueError('共享范围无效')
        max_redirects = data.get('max_redirects', DEFAULT_MAX_REDIRECTS)
        if type(max_redirects) is not int or not 0 <= max_redirects <= 16:
            raise ValueError('跳转上限无效')
        max_bytes = data.get('max_bytes', DEFAULT_MAX_BYTES)
        if type(max_bytes) is not int or max_bytes < 0:
            raise ValueError('读取预算无效')
        return cls(source_id, credential_scope, url, headers,
                   tuple(item.lower() for item in sensitive), shared_scope,
                   max_redirects, max_bytes)


def _parse_header_pairs(raw):
    if not isinstance(raw, list):
        raise ValueError('请求头无效')
    pairs = []
    for pair in raw:
        if not isinstance(pair, list) or len(pair) != 2 \
                or not all(isinstance(item, str) for item in pair):
            raise ValueError('请求头结构无效')
        if not pair[0] or any(x in pair[0] + pair[1] for x in ('\r', '\n')):
            raise ValueError('请求头注入')
        pairs.append((pair[0], pair[1]))
    return tuple(pairs)


@dataclass
class Delivery:
    """一次交付：状态、头、正文与可追溯证据。"""
    ok: bool
    status: int | None
    headers: tuple
    body: bytes | None
    evidence: dict

    @property
    def body_sha256(self):
        return self.evidence.get('body', {}).get('sha256')


@dataclass
class _Redirect:
    status: int
    location: str
    events: list


@dataclass
class _Failed:
    error: str
    events: list


@dataclass
class _Final:
    status: int
    headers: tuple
    body: bytes
    body_origin: str
    variant: object
    granted_by: str
    stored: bool
    store_reason: str | None
    refetch_reason: str | None
    revalidation: dict | None
    freshness: object
    record_version: int | None
    events: list


class _Flight:
    __slots__ = ('done', 'outcome', 'error', 'waiters')

    def __init__(self):
        self.done = threading.Event()
        self.outcome = None
        self.error = None
        self.waiters = 0


def _origin(url):
    parts = urlsplit(url)
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == 'https' else 80
    return (parts.scheme.lower(), (parts.hostname or '').lower(), port)


def _is_zero(value):
    try:
        return int(value) == 0
    except (TypeError, ValueError):
        return False


def _flight_key(scope_key, url, headers, lookup):
    if lookup is not None and lookup.variant is not None:
        basis = 'variant:' + lookup.variant.id
    else:
        basis = 'headers:' + json.dumps(normalized_request_headers(headers), sort_keys=True)
    raw = scope_key + '\0' + url + '\0' + basis
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


def _decision_of(final):
    if final.body_origin == 'network':
        return 'fetched'
    if final.granted_by == 'revalidation-304':
        return 'revalidated'
    return 'hit'


class Collector:
    """采集器：同一实例内的并行领取可合并传输；缓存落盘可跨实例恢复。"""

    def __init__(self, cache_root, fetcher=None):
        self.cache = RepresentationCache(cache_root)
        self._fetch = fetcher or _source.fetch
        self._flights = {}
        self._flights_lock = threading.Lock()

    def collect(self, config, *, now, cancel_event=None):
        """领取一个表示变体；now为调用者显式传入的epoch秒。"""
        if isinstance(config, dict):
            config = SourceConfig.from_dict(config)
        if not isinstance(now, (int, float)) or isinstance(now, bool):
            raise ValueError('时间无效')
        try:
            return self._collect(config, float(now), cancel_event)
        except CollectCancelled:
            raise
        except Exception as exc:
            return Delivery(False, None, (), None, self._failure_evidence(config, now, exc))

    def _collect(self, config, now, cancel_event):
        events = []
        sensitive = set(SENSITIVE_REQUEST_HEADERS) | set(config.sensitive_headers)
        request_cc = parse_cache_control(header_get(config.headers, 'cache-control'))
        force_revalidate = 'no-cache' in request_cc or _is_zero(request_cc.get('max-age'))
        bypass_cache = 'no-store' in request_cc
        only_cached = 'only-if-cached' in request_cc
        scope_key = 'shared:' + config.shared_scope if config.shared_scope \
            else 'scope:' + config.credential_scope
        headers = list(config.headers)
        url = config.url
        redirects = []
        for hop in range(config.max_redirects + 1):
            had_auth = any(name.lower() in sensitive for name, _ in headers)
            lookup = None if bypass_cache else self.cache.lookup(scope_key, url, headers, now)
            events.append('lookup:' + (lookup.kind if lookup else 'bypassed')
                          + ('' if lookup is None or lookup.reason is None
                             else '(' + lookup.reason + ')'))
            if lookup is not None and lookup.kind == 'hit':
                if not force_revalidate:
                    body = self.cache.read_body(lookup.variant.body_sha256)
                    if body is not None:
                        final = _Final(lookup.variant.status, lookup.variant.headers, body,
                                       'stored-variant', lookup.variant, 'stored-response',
                                       False, None, None, None, lookup.freshness,
                                       lookup.record_version, events)
                        return self._deliver(config, scope_key, url, final, redirects,
                                             lookup, None, now, events)
                    lookup = Lookup('stale', lookup.variant, lookup.freshness,
                                    'entity-missing', lookup.record_version)
                    events.append('entity-missing:revalidate')
                else:
                    lookup = Lookup('stale', lookup.variant, lookup.freshness,
                                    'request-no-cache', lookup.record_version)
                    events.append('request-no-cache:revalidate')
            if only_cached:
                reason = 'only-if-cached-' + ('stale' if lookup is not None
                                              and lookup.kind == 'stale' else 'miss')
                events.append(reason)
                return self._fail(config, scope_key, url, redirects, events, reason, lookup, now)
            flight_key = _flight_key(scope_key, url, headers, lookup)
            outcome, role, waiters = self._join_flight(
                flight_key, cancel_event,
                lambda: self._network_round(config, scope_key, url, headers, lookup,
                                            bypass_cache, had_auth, now))
            events.extend(outcome.events)
            if isinstance(outcome, _Redirect):
                if hop == config.max_redirects:
                    return self._fail(config, scope_key, url, redirects, events,
                                      'redirect-limit', lookup, now)
                next_url = urljoin(url, outcome.location)
                if _origin(next_url) != _origin(url):
                    headers = [(name, value) for name, value in headers
                               if name.lower() not in sensitive]
                    forwarded = False
                else:
                    forwarded = True
                redirects.append({'url': url, 'status': outcome.status,
                                  'location': outcome.location, 'next_url': next_url,
                                  'credentials_forwarded': forwarded})
                events.append('redirect:' + str(outcome.status))
                url = next_url
                continue
            if isinstance(outcome, _Failed):
                return self._fail(config, scope_key, url, redirects, events,
                                  outcome.error, lookup, now)
            return self._deliver(config, scope_key, url, outcome, redirects, lookup,
                                 {'key': flight_key, 'role': role, 'waiters': waiters},
                                 now, events)
        return self._fail(config, scope_key, url, redirects, events, 'redirect-limit', None, now)

    def _network_round(self, config, scope_key, url, headers, lookup,
                       bypass_cache, had_auth, now):
        """单URL网络回合：条件重验、304合并、失效后无条件重取、存储与交付。"""
        events = []
        base_variant = lookup.variant if lookup is not None and lookup.kind == 'stale' else None
        unconditional = None
        for _ in range(3):
            conditional = {}
            if unconditional is None and base_variant is not None:
                conditional = base_variant.validators()
                if not conditional:
                    unconditional = 'stale-no-validator'
                    events.append('no-validator:unconditional')
            request_headers = dict(headers)
            request_headers.update(conditional)
            if conditional:
                events.append('conditional:' + ','.join(sorted(conditional)))
            response = self._fetch(url, config.source_id, method='GET',
                                   headers=request_headers, max_bytes=config.max_bytes)
            events.append('response:' + str(response.status))
            location = header_get(response.headers, 'location')
            if response.status in _REDIRECT_STATUSES and location:
                return _Redirect(response.status, location, events)
            if response.status == 304:
                if not conditional:
                    # 无条件请求收到304：无实体可交付，不得凭空生成正文
                    events.append('304-without-entity')
                    return _Failed('304-without-entity', events)
                merge = self.cache.merge_304(scope_key, url, variant_id=base_variant.id,
                                             base_seq=base_variant.seq,
                                             headers=response.headers, now=now)
                events.append('304-merge:' + merge.outcome)
                revalidation = {'conditional_request': dict(conditional), 'status': 304,
                                'merge': merge.outcome,
                                'response_date': header_get(response.headers, 'date')}
                if merge.outcome == 'merged':
                    body = self.cache.read_body(merge.variant.body_sha256)
                    if body is None:
                        # 304未带来实体且本地实体缺失：以合并后变体为基做无条件重取，
                        # 让恢复的新正文能够落回同一槽位
                        unconditional = 'entity-missing'
                        base_variant = merge.variant
                        events.append('304-but-entity-missing:unconditional')
                        continue
                    freshness = evaluate_freshness(merge.variant.headers,
                                                   merge.variant.stored_at, now)
                    return _Final(merge.variant.status, merge.variant.headers, body,
                                  'stored-variant', merge.variant, 'revalidation-304',
                                  False, None, None, revalidation, freshness,
                                  merge.record_version, events)
                if merge.outcome in ('vary-changed', 'no-store'):
                    unconditional = merge.outcome
                    base_variant = None
                    continue
                refreshed = self.cache.lookup(scope_key, url, headers, now)
                events.append('re-lookup:' + refreshed.kind)
                if refreshed.kind == 'hit':
                    body = self.cache.read_body(refreshed.variant.body_sha256)
                    if body is not None:
                        return _Final(refreshed.variant.status, refreshed.variant.headers,
                                      body, 'stored-variant', refreshed.variant,
                                      'stored-response', False, None, None, revalidation,
                                      refreshed.freshness, refreshed.record_version, events)
                base_variant = refreshed.variant if refreshed.kind == 'stale' else None
                if base_variant is None:
                    unconditional = 'revalidation-superseded'
                continue
            stored, store_reason, variant_id, record_version = False, None, None, None
            if not bypass_cache and not 300 <= response.status < 400:
                result = self.cache.store(scope_key, url, headers, status=response.status,
                                          headers=response.headers, body=response.body,
                                          now=now, base_variant=base_variant,
                                          shared=bool(config.shared_scope), had_auth=had_auth)
                stored = result.stored
                store_reason = result.reason
                variant_id = result.variant_id
                record_version = result.record_version
                events.append('store:' + ('applied(' + str(variant_id) + ')' if stored
                                          else 'skipped(' + str(store_reason) + ')'))
            variant = None
            if stored:
                after = self.cache.lookup(scope_key, url, headers, now)
                variant = after.variant
            freshness = evaluate_freshness(response.headers, now, now)
            return _Final(response.status, response.headers, response.body, 'network',
                          variant, 'network-response', stored, store_reason, unconditional,
                          None, freshness, record_version, events)
        return _Failed('revalidation-loop', events)

    def _join_flight(self, key, cancel_event, work):
        """相同变体的并行领取合并为一次传输；取消等待者不影响他人。"""
        with self._flights_lock:
            flight = self._flights.get(key)
            if flight is None:
                flight = _Flight()
                self._flights[key] = flight
                role = 'leader'
            else:
                flight.waiters += 1
                role = 'follower'
        if role == 'follower':
            while not flight.done.wait(0.02):
                if cancel_event is not None and cancel_event.is_set():
                    with self._flights_lock:
                        flight.waiters -= 1
                    raise CollectCancelled('等待合并传输时被取消')
            if flight.error is not None:
                raise flight.error
            return flight.outcome, 'follower', flight.waiters
        cancelled = cancel_event is not None and cancel_event.is_set()
        try:
            flight.outcome = work()
        except Exception as exc:
            flight.error = exc
        finally:
            flight.done.set()
            with self._flights_lock:
                if self._flights.get(key) is flight:
                    del self._flights[key]
        if flight.error is not None:
            raise flight.error
        if cancelled or (cancel_event is not None and cancel_event.is_set()):
            raise CollectCancelled('领取方已取消；结果已留给其他等待者')
        return flight.outcome, 'leader', flight.waiters

    def _deliver(self, config, scope_key, url, final, redirects, lookup,
                 flight_info, now, events):
        variant = final.variant
        freshness = final.freshness
        evidence = {
            'source_id': config.source_id,
            'credential_scope': config.credential_scope,
            'shared_scope': config.shared_scope,
            'url': config.url,
            'effective_url': url,
            'time': now,
            'cache': {
                'scope_key': scope_key,
                'record': self.cache.record_relpath(scope_key, url),
                'record_version': final.record_version,
                'decision': _decision_of(final),
                'lookup': lookup.kind if lookup else 'bypassed',
                'lookup_reason': lookup.reason if lookup else None,
                'unconditional_refetch': final.refetch_reason,
                'stored': final.stored,
                'store_reason': final.store_reason,
            },
            'variant': None if variant is None else {
                'id': variant.id,
                'vary': list(variant.vary),
                'request_headers': dict(variant.request_headers),
                'stored_at': variant.stored_at,
            },
            'body': {
                'sha256': hashlib.sha256(final.body).hexdigest(),
                'bytes': len(final.body),
                'origin': final.body_origin,
                'origin_variant': None if variant is None else variant.id,
            },
            'freshness': {
                'granted_by': final.granted_by,
                'basis': None if freshness is None else freshness.basis,
                'lifetime_seconds': None if freshness is None else freshness.lifetime,
                'age_seconds': None if freshness is None else round(freshness.current_age, 6),
                'fresh': None if freshness is None else freshness.fresh,
                'response_date': None if variant is None else header_get(variant.headers, 'date'),
                'etag': None if variant is None else header_get(variant.headers, 'etag'),
            },
            'revalidation': final.revalidation,
            'redirects': redirects,
            'flight': flight_info or {'key': None, 'role': 'solo', 'waiters': 0},
            'configured_request_headers': {name: value for name, value in config.headers
                                           if name.lower() not in
                                           set(SENSITIVE_REQUEST_HEADERS) | set(config.sensitive_headers)},
            'sensitive_headers_redacted': sorted(
                {name.lower() for name, _ in config.headers}
                & (set(SENSITIVE_REQUEST_HEADERS) | set(config.sensitive_headers))),
            'events': list(events),
        }
        return Delivery(True, final.status, final.headers, final.body, evidence)

    def _fail(self, config, scope_key, url, redirects, events, error, lookup, now):
        evidence = {
            'source_id': config.source_id,
            'credential_scope': config.credential_scope,
            'shared_scope': config.shared_scope,
            'url': config.url,
            'effective_url': url,
            'time': now,
            'cache': {
                'scope_key': scope_key,
                'record': self.cache.record_relpath(scope_key, url),
                'decision': 'failed',
                'lookup': lookup.kind if lookup else 'bypassed',
                'lookup_reason': lookup.reason if lookup else None,
            },
            'error': {'type': 'delivery', 'message': error},
            'redirects': redirects,
            'events': list(events),
        }
        return Delivery(False, None, (), None, evidence)

    @staticmethod
    def _failure_evidence(config, now, exc):
        return {'source_id': config.source_id,
                'credential_scope': config.credential_scope,
                'shared_scope': config.shared_scope,
                'url': config.url,
                'time': now,
                'cache': {'decision': 'failed'},
                'error': {'type': type(exc).__name__, 'message': str(exc),
                          'traceback': traceback.format_exc()},
                'events': []}
