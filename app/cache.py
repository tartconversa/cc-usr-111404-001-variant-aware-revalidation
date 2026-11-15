"""表示变体缓存：HTTP缓存语义、落盘恢复与并发防护。

缓存记录按(身份范围, URL)组织，一条记录容纳同一URL的多个表示变体；
每个变体保存自己的Vary选择条件、响应头、实体摘要与存储时间，凭据不落盘。
新鲜度只依据显式协议信号(max-age/Expires)，无依据不得声称fresh；
304按RFC 9111 §4.3.4合并头字段，Vary变更或no-store使旧变体失效；
存储与合并以变体序号做比较-交换，迟到的旧重验不得覆盖较新正文；
落盘采用临时文件+原子替换，损坏记录与实体隔离后按未命中处理。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, replace
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

FORMAT = 1
SENSITIVE_REQUEST_HEADERS = frozenset({'authorization', 'proxy-authorization', 'cookie'})
_LIST_VALUED = frozenset({'accept', 'accept-language', 'accept-encoding', 'accept-charset', 'vary'})
_CACHEABLE_BY_DEFAULT = frozenset({200, 203, 204, 300, 301, 404, 405, 410, 414, 501})
_SHA256_RE = re.compile(r'^[0-9a-f]{64}$')


def header_get(headers, name):
    """合并重复字段为逗号列表；不存在返回None。name须为小写。"""
    values = [value for key, value in headers if key.lower() == name]
    return ', '.join(values) if values else None


def _split_list(value):
    """按逗号切分列表头，忽略引号内逗号。"""
    parts, current, quoted = [], [], False
    for char in value:
        if char == '"':
            quoted = not quoted
            current.append(char)
        elif char == ',' and not quoted:
            parts.append(''.join(current))
            current = []
        else:
            current.append(char)
    parts.append(''.join(current))
    return [part.strip() for part in parts]


def parse_cache_control(value):
    """解析Cache-Control为{指令: 值|True}；指令名小写，引号还原。"""
    directives = {}
    if not value:
        return directives
    for item in _split_list(value):
        if not item:
            continue
        name, sep, raw = item.partition('=')
        name = name.strip().lower()
        if not name:
            continue
        if not sep:
            directives[name] = True
        else:
            raw = raw.strip()
            if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
                raw = raw[1:-1]
            directives[name] = raw
    return directives


def _delta_seconds(value):
    if not isinstance(value, str):
        return None
    try:
        seconds = int(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def parse_http_date(value):
    """解析三种HTTP日期为epoch秒；无效返回None。"""
    if not value:
        return None
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def parse_vary(value):
    """返回(字段名元组, 是否含*)；字段名小写去重保序。"""
    if not value:
        return (), False
    fields, star = [], False
    for item in _split_list(value):
        item = item.lower()
        if not item:
            continue
        if item == '*':
            star = True
        elif item not in fields:
            fields.append(item)
    return tuple(fields), star


def normalize_field_value(name, value):
    """Vary比较用的请求头值归一化：去外围空白，已知协商列表字段小写并规整逗号。"""
    value = value.strip()
    if name in _LIST_VALUED:
        items = [part.strip().lower() for part in value.split(',')]
        return ','.join(part for part in items if part)
    return value


def normalized_request_headers(headers):
    """请求头序列→{小写名: 归一化值}；同名重复按列表合并。"""
    grouped = {}
    for name, value in headers:
        grouped.setdefault(name.strip().lower(), []).append(value)
    return {name: normalize_field_value(name, ', '.join(values))
            for name, values in grouped.items()}


@dataclass(frozen=True)
class Freshness:
    """一次新鲜度判定；basis为none时不得声称fresh。"""
    basis: str
    lifetime: float | None
    current_age: float
    fresh: bool
    no_cache: bool
    no_store: bool
    must_revalidate: bool


def evaluate_freshness(headers, stored_at, now):
    """按RFC 9111 §4.2计算当前年龄与新鲜度；时间由调用者显式传入。"""
    directives = parse_cache_control(header_get(headers, 'cache-control'))
    date = parse_http_date(header_get(headers, 'date'))
    if date is None:
        date = stored_at
    age_value = 0.0
    raw_age = header_get(headers, 'age')
    if raw_age is not None:
        try:
            age_value = max(0.0, float(int(raw_age.strip())))
        except ValueError:
            age_value = 0.0
    apparent_age = max(0.0, stored_at - date)
    current_age = max(apparent_age, age_value) + max(0.0, now - stored_at)
    lifetime, basis = None, 'none'
    max_age = _delta_seconds(directives.get('max-age'))
    if max_age is not None:
        lifetime, basis = float(max_age), 'max-age'
    else:
        raw_expires = header_get(headers, 'expires')
        if raw_expires is not None:
            expires = parse_http_date(raw_expires)
            lifetime = 0.0 if expires is None else max(0.0, expires - date)
            basis = 'expires'
    no_cache = 'no-cache' in directives
    fresh = lifetime is not None and current_age < lifetime and not no_cache
    return Freshness(basis, lifetime, current_age, fresh, no_cache,
                     'no-store' in directives, 'must-revalidate' in directives)


@dataclass(frozen=True)
class Variant:
    """一个表示变体：自己的Vary选择条件、响应头、实体摘要与存储时间。"""
    id: str
    vary: tuple
    request_headers: dict
    status: int
    headers: tuple
    body_sha256: str
    body_bytes: int
    stored_at: float
    seq: int

    def validators(self):
        """条件重验请求头；弱ETag按口径可用于If-None-Match。"""
        conditional = {}
        etag = header_get(self.headers, 'etag')
        if etag is not None:
            conditional['If-None-Match'] = etag
        last_modified = header_get(self.headers, 'last-modified')
        if last_modified is not None:
            conditional['If-Modified-Since'] = last_modified
        return conditional

    def to_json(self):
        return {'id': self.id, 'vary': list(self.vary),
                'request_headers': dict(self.request_headers), 'status': self.status,
                'headers': [list(pair) for pair in self.headers],
                'body_sha256': self.body_sha256, 'body_bytes': self.body_bytes,
                'stored_at': self.stored_at, 'seq': self.seq}

    @classmethod
    def from_json(cls, data):
        if not isinstance(data, dict):
            raise ValueError('变体结构无效')
        try:
            variant_id = data['id']
            vary = data['vary']
            request_headers = data['request_headers']
            status = data['status']
            headers = data['headers']
            body_sha256 = data['body_sha256']
            body_bytes = data['body_bytes']
            stored_at = data['stored_at']
            seq = data['seq']
        except KeyError as exc:
            raise ValueError('变体字段缺失') from exc
        if not isinstance(variant_id, str) or not variant_id:
            raise ValueError('变体身份无效')
        if not isinstance(vary, list) or not all(isinstance(field, str) for field in vary):
            raise ValueError('变体Vary无效')
        if not isinstance(request_headers, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in request_headers.items()):
            raise ValueError('变体请求头无效')
        if type(status) is not int or not 100 <= status <= 599:
            raise ValueError('变体状态无效')
        if not isinstance(headers, list) or not all(
                isinstance(pair, list) and len(pair) == 2
                and all(isinstance(item, str) for item in pair) for pair in headers):
            raise ValueError('变体响应头无效')
        if not isinstance(body_sha256, str) or not _SHA256_RE.match(body_sha256):
            raise ValueError('变体实体摘要无效')
        if type(body_bytes) is not int or body_bytes < 0:
            raise ValueError('变体实体长度无效')
        if not isinstance(stored_at, (int, float)) or isinstance(stored_at, bool):
            raise ValueError('变体存储时间无效')
        if type(seq) is not int or seq < 0:
            raise ValueError('变体序号无效')
        return cls(variant_id, tuple(vary), dict(request_headers), status,
                   tuple(tuple(pair) for pair in headers), body_sha256, body_bytes,
                   float(stored_at), seq)


@dataclass(frozen=True)
class Lookup:
    """缓存查找结果；kind为hit时可直接交付，stale时variant携带重验资格。"""
    kind: str
    variant: Variant | None
    freshness: Freshness | None
    reason: str | None
    record_version: int | None


@dataclass(frozen=True)
class StoreResult:
    stored: bool
    variant_id: str | None
    reason: str | None
    record_version: int | None


@dataclass(frozen=True)
class MergeResult:
    """304合并结果；outcome为merged时variant是合并后的新变体。"""
    outcome: str
    record_version: int | None
    variant: Variant | None


@dataclass
class _Record:
    scope_key: str
    url: str
    version: int
    variants: list


def _vary_matches(variant, normalized_request):
    return all(normalized_request.get(field, '') == variant.request_headers.get(field, '')
               for field in variant.vary)


def _storable_reason(status, headers, vary_fields, vary_star, shared, had_auth):
    """返回不可存原因；None表示可存。共享记录需HTTP显式允许。"""
    if vary_star:
        return 'vary-star'
    if set(vary_fields) & SENSITIVE_REQUEST_HEADERS:
        return 'vary-sensitive'
    directives = parse_cache_control(header_get(headers, 'cache-control'))
    if shared:
        if 'private' in directives:
            return 'shared-private'
        if had_auth and not any(name in directives for name in ('public', 'must-revalidate', 's-maxage')):
            return 'shared-auth'
    if status not in _CACHEABLE_BY_DEFAULT:
        explicit = _delta_seconds(directives.get('max-age')) is not None \
            or header_get(headers, 'expires') is not None
        if not explicit:
            return 'not-cacheable-status'
    return None


class RepresentationCache:
    """按(身份范围, URL)持久化表示变体；默认仅同身份范围可见。"""

    def __init__(self, root):
        self.root = Path(root)
        (self.root / 'records').mkdir(parents=True, exist_ok=True)
        (self.root / 'bodies').mkdir(parents=True, exist_ok=True)
        self._guard = threading.Lock()
        self._locks = {}

    def _lock(self, key):
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    @staticmethod
    def _slug(scope_key):
        return re.sub(r'[^A-Za-z0-9._-]+', '_', scope_key) or '_'

    def _record_path(self, scope_key, url):
        digest = hashlib.sha256(url.encode('utf-8')).hexdigest()[:32]
        return self.root / 'records' / self._slug(scope_key) / (digest + '.json')

    def record_relpath(self, scope_key, url):
        return str(self._record_path(scope_key, url).relative_to(self.root))

    def _body_path(self, sha256):
        return self.root / 'bodies' / sha256

    def body_relpath(self, sha256):
        return 'bodies/' + sha256

    def _body_exists(self, sha256):
        return self._body_path(sha256).is_file()

    def read_body(self, sha256):
        """读取并校验实体字节；缺失或被篡改返回None并隔离坏文件。"""
        if not isinstance(sha256, str) or not _SHA256_RE.match(sha256):
            return None
        path = self._body_path(sha256)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if hashlib.sha256(data).hexdigest() != sha256:
            self._quarantine(path)
            return None
        return data

    @staticmethod
    def _quarantine(path):
        try:
            path.rename(path.with_name(path.name + '.bad'))
        except OSError:
            pass

    def _load(self, path, scope_key, url):
        try:
            raw = path.read_text(encoding='utf-8')
        except OSError:
            return None
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get('format') != FORMAT:
                raise ValueError('缓存记录格式无效')
            if data.get('scope_key') != scope_key or data.get('url') != url:
                raise ValueError('缓存记录归属不符')
            version = data['version']
            if type(version) is not int or version < 0:
                raise ValueError('缓存记录版本无效')
            variants = [Variant.from_json(item) for item in data['variants']]
        except (ValueError, KeyError, TypeError):
            self._quarantine(path)
            return None
        return _Record(scope_key, url, version, variants)

    def _persist(self, path, record):
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({'format': FORMAT, 'scope_key': record.scope_key,
                              'url': record.url, 'version': record.version,
                              'variants': [variant.to_json() for variant in record.variants]},
                             ensure_ascii=False, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.tmp-', suffix='.json')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                stream.write(payload)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _write_body(self, sha256, body):
        path = self._body_path(sha256)
        if path.is_file():
            return
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.tmp-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(body)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def lookup(self, scope_key, url, request_headers, now):
        """选择匹配变体并判定新鲜度；实体缺失的fresh变体降级为stale。"""
        path = self._record_path(scope_key, url)
        record = self._load(path, scope_key, url)
        if record is None:
            return Lookup('miss', None, None, 'no-record', None)
        normalized = normalized_request_headers(request_headers)
        for variant in record.variants:
            if _vary_matches(variant, normalized):
                freshness = evaluate_freshness(variant.headers, variant.stored_at, now)
                if freshness.fresh and self._body_exists(variant.body_sha256):
                    return Lookup('hit', variant, freshness, None, record.version)
                if freshness.fresh:
                    reason = 'entity-missing'
                elif freshness.basis == 'none':
                    reason = 'no-freshness-basis'
                elif freshness.no_cache:
                    reason = 'no-cache-directive'
                else:
                    reason = 'stale'
                return Lookup('stale', variant, freshness, reason, record.version)
        return Lookup('miss', None, None, 'no-variant-match', record.version)

    def store(self, scope_key, url, request_headers, *, status, headers, body, now,
              base_variant, shared, had_auth):
        """存储完整响应；base_variant为本次请求依据的旧变体(可为None)。

        迟到存储不得覆盖同槽位较新变体；Vary变更或no-store使匹配旧变体失效。
        """
        key = (scope_key, url)
        with self._lock(key):
            path = self._record_path(scope_key, url)
            record = self._load(path, scope_key, url)
            if record is None:
                record = _Record(scope_key, url, 0, [])
            normalized = normalized_request_headers(request_headers)
            directives = parse_cache_control(header_get(headers, 'cache-control'))
            if 'no-store' in directives:
                if self._drop_matching(record, normalized):
                    record.version += 1
                    self._persist(path, record)
                return StoreResult(False, None, 'no-store', record.version)
            vary_fields, vary_star = parse_vary(header_get(headers, 'vary'))
            reason = _storable_reason(status, headers, vary_fields, vary_star, shared, had_auth)
            if reason is not None:
                return StoreResult(False, None, reason, record.version)
            nominated = {field: normalized.get(field, '') for field in vary_fields}
            # Vary变更：按旧Vary匹配本请求但协商轴不同的变体不再沿用
            record.variants = [variant for variant in record.variants
                               if not (_vary_matches(variant, normalized)
                                       and variant.vary != vary_fields)]
            slot = next((variant for variant in record.variants
                         if variant.vary == vary_fields
                         and variant.request_headers == nominated), None)
            if slot is not None and (base_variant is None
                                     or slot.id != base_variant.id
                                     or slot.seq != base_variant.seq):
                return StoreResult(False, None, 'superseded', record.version)
            body_sha256 = hashlib.sha256(body).hexdigest()
            self._write_body(body_sha256, body)
            record.version += 1
            variant = Variant('v' + str(record.version), vary_fields, nominated, status,
                              tuple(headers), body_sha256, len(body), float(now), record.version)
            record.variants = [item for item in record.variants if item is not slot] + [variant]
            self._persist(path, record)
            return StoreResult(True, variant.id, None, record.version)

    def merge_304(self, scope_key, url, *, variant_id, base_seq, headers, now):
        """按RFC 9111 §4.3.4把304头字段合并进已存变体；迟到合并不得覆盖较新变体。"""
        key = (scope_key, url)
        with self._lock(key):
            path = self._record_path(scope_key, url)
            record = self._load(path, scope_key, url)
            if record is None:
                return MergeResult('gone', None, None)
            index = next((i for i, variant in enumerate(record.variants)
                          if variant.id == variant_id), None)
            if index is None:
                return MergeResult('gone', record.version, None)
            variant = record.variants[index]
            if variant.seq != base_seq:
                return MergeResult('superseded', record.version, None)
            directives = parse_cache_control(header_get(headers, 'cache-control'))
            record.version += 1
            if 'no-store' in directives:
                del record.variants[index]
                self._persist(path, record)
                return MergeResult('no-store', record.version, None)
            raw_vary = header_get(headers, 'vary')
            if raw_vary is not None:
                new_vary, new_star = parse_vary(raw_vary)
                if new_star or new_vary != variant.vary:
                    del record.variants[index]
                    self._persist(path, record)
                    return MergeResult('vary-changed', record.version, None)
            updated_names = {name.lower() for name, _ in headers}
            merged = [pair for pair in variant.headers if pair[0].lower() not in updated_names]
            merged.extend(headers)
            refreshed = replace(variant, headers=tuple(merged), stored_at=float(now),
                                seq=record.version)
            record.variants[index] = refreshed
            self._persist(path, record)
            return MergeResult('merged', record.version, refreshed)

    @staticmethod
    def _drop_matching(record, normalized):
        kept = [variant for variant in record.variants
                if not _vary_matches(variant, normalized)]
        changed = len(kept) != len(record.variants)
        record.variants = kept
        return changed
