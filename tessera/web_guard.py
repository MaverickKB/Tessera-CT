"""Refuse web_fetch destinations that are not publicly routable.

The check runs inside the transport, right before a socket is created, so a
redirect hop and a re-resolved name are vetted exactly like the first request.
The vetted address is then pinned for the actual connect, which closes the
window between the lookup and the dial.
"""

import ipaddress
import socket
from urllib import request


def _unwrapped(address):
    """An IPv4-mapped IPv6 address is checked as the IPv4 address it carries."""
    parsed = ipaddress.ip_address(address)
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def _is_non_public(address):
    parsed = _unwrapped(address)
    # is_global covers private, loopback, link-local, reserved and unspecified.
    # Multicast reports as global in ipaddress, so it is checked on its own.
    return parsed.is_multicast or not parsed.is_global


def vet_public_host(name, port, resolver=socket.getaddrinfo):
    """Resolve name and return one address, refusing if any answer is non-public."""
    answers = resolver(name, port, type=socket.SOCK_STREAM)
    addresses = [answer[4][0].split('%')[0] for answer in answers]
    for address in addresses:
        if _is_non_public(address):
            raise ValueError(f'web_fetch refused {name}: {address} is not a public address')
    if not addresses:
        raise ValueError(f'web_fetch refused {name}: no addresses resolved')
    return addresses[0]


class CappedRedirect(request.HTTPRedirectHandler):
    """Follow at most five hops, and keep a refused hop's host in the message."""

    max_hops = 5

    def http_error_302(self, req, fp, code, msg, headers):
        visited = getattr(req, 'redirect_dict', {})
        hops = sum(visited.values())
        if hops >= self.max_hops:
            raise ValueError(f'web_fetch refused {req.full_url}: more than {self.max_hops} redirects')
        try:
            return super().http_error_302(req, fp, code, msg, headers)
        except ValueError as error:
            raise ValueError(f'{error} (redirect from {req.full_url})') from error

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302
