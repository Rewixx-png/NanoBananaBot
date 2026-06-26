import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_PRIVATE_NETWORKS = [
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('127.0.0.0/8'),
    ipaddress.ip_network('169.254.0.0/16'),
    ipaddress.ip_network('::1/128'),
    ipaddress.ip_network('fc00::/7'),
    ipaddress.ip_network('fe80::/10'),
    ipaddress.ip_network('0.0.0.0/8'),
    ipaddress.ip_network('100.64.0.0/10'),
]


def is_safe_url(url: str) -> bool:
    """Validate that a URL is safe to fetch: only http/https, no private/loopback/link-local IPs."""
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    if parsed.scheme not in ('http', 'https'):
        return False

    hostname = parsed.hostname
    if not hostname:
        return False

    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        logger.warning(f'DNS resolution failed for {hostname}, blocking as unsafe')
        return False

    for family, _, _, _, sockaddr in addrinfo:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        for net in _PRIVATE_NETWORKS:
            if ip in net:
                logger.warning(f'Blocked SSRF attempt: {url!r} resolved to {ip_str} ({net})')
                return False

    return True
