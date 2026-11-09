"""HTTPS credential site boundaries using a bundled ICANN + PRIVATE PSL snapshot.

No network lookup occurs while using a credential. Refresh public_suffix_list.dat
from https://publicsuffix.org/list/public_suffix_list.dat when shipping updates.
Unknown/internal suffixes, IP addresses and public suffixes use exact-origin scope.
"""
from functools import lru_cache
import ipaddress
from pathlib import Path
import re
from urllib.parse import urlsplit


def website_origin(value):
    if not isinstance(value, str) or len(value) > 512 or any(ord(c) <= 32 for c in value):
        raise ValueError('Enter an HTTPS website origin, for example https://example.com')
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Enter an HTTPS website origin without a path or sign-in details')
    host = parsed.hostname.encode('idna').decode('ascii').lower()
    try:
        address = ipaddress.ip_address(host)
        host = '[' + address.compressed + ']' if address.version == 6 else address.compressed
    except ValueError:
        if len(host) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in host.split('.')):
            raise ValueError('Invalid website hostname')
    port = parsed.port
    if port is not None and not 1 <= port <= 65535:
        raise ValueError('Invalid HTTPS port')
    return 'https://' + host + (':' + str(port) if port not in (None, 443) else '')


@lru_cache(maxsize=1)
def suffix_rules():
    exact, wildcards, exceptions = set(), set(), set()
    for line in Path(__file__).with_name('public_suffix_list.dat').read_text(encoding='utf-8').splitlines():
        rule = line.split('//', 1)[0].strip()
        if not rule:
            continue
        target = exceptions if rule.startswith('!') else wildcards if rule.startswith('*.') else exact
        rule = rule.removeprefix('!').removeprefix('*.').encode('idna').decode('ascii').lower()
        target.add(rule)
    if 'com' not in exact or 'github.io' not in exact:
        raise ValueError('Credential site rules are unavailable')
    return exact, wildcards, exceptions


def registrable_domain(host):
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    exact, wildcards, exceptions = suffix_rules()
    labels = host.split('.')
    longest = 0
    for index in range(len(labels)):
        suffix = '.'.join(labels[index:])
        size = len(labels) - index
        if suffix in exceptions:
            longest = size - 1
            break
        if suffix in exact:
            longest = max(longest, size)
        if index and suffix in wildcards:
            longest = max(longest, size + 1)
    if not longest or len(labels) <= longest:
        return None
    return '.'.join(labels[-longest - 1:])


def website_site(origin):
    parsed = urlsplit(website_origin(origin))
    return registrable_domain(parsed.hostname)


def allows_origin(approved, candidate):
    approved, candidate = website_origin(approved), website_origin(candidate)
    if approved == candidate:
        return True
    a, b = urlsplit(approved), urlsplit(candidate)
    site = registrable_domain(a.hostname)
    return bool(site and site == registrable_domain(b.hostname) and (a.port or 443) == (b.port or 443))
