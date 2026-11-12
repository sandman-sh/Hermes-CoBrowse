"""Security guards for Hermes CoBrowse: SSRF protection and minimal browser env."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from typing import Dict, List
from urllib.parse import urlsplit

_BLOCKED_SCHEMES = {
    "about",
    "blob",
    "chrome",
    "chrome-extension",
    "data",
    "devtools",
    "edge",
    "file",
    "filesystem",
    "ftp",
    "javascript",
    "vbscript",
    "view-source",
    "ws",
    "wss",
}

_SCHEME_PREFIX_RE = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9+.-]*):")
_HOST_PORT_ONLY_RE = re.compile(r"^[a-zA-Z0-9.\[\]-]+:\d+([/?#]|$)")

_CGNAT_NET = ipaddress.IPv4Network("100.64.0.0/10")
_IPV6_ULA_NET = ipaddress.IPv6Network("fc00::/7")
_IPV6_LINK_LOCAL_NET = ipaddress.IPv6Network("fe80::/10")


def is_blocked_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if IP is loopback, private, link-local, CGNAT, ULA, or otherwise non-public."""
    if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped:
        return is_blocked_ip(ip_obj.ipv4_mapped)

    if (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
        or not ip_obj.is_global
    ):
        return True

    if isinstance(ip_obj, ipaddress.IPv4Address):
        return ip_obj in _CGNAT_NET
    return ip_obj in _IPV6_ULA_NET or ip_obj in _IPV6_LINK_LOCAL_NET


def is_blocked_hostname_literal(hostname: str) -> bool:
    """Reject local-only hostnames before DNS lookup."""
    h = (hostname or "").strip().strip("[]").rstrip(".").lower()
    if not h:
        return True
    return (
        h == "localhost"
        or h.endswith(".localhost")
        or h.endswith(".local")
        or h.endswith(".internal")
    )


def normalize_navigation_url(raw_url: str, *, allow_about_blank: bool = True) -> str:
    """Validate and normalize model/user-supplied URLs, allowing only http(s) or about:blank."""
    url = str(raw_url or "").strip()
    if not url:
        raise ValueError("URL cannot be empty.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ValueError("URL contains invalid control characters.")

    if url.lower() == "about:blank":
        if not allow_about_blank:
            raise ValueError("about:blank is not allowed for this operation.")
        return "about:blank"

    scheme_match = _SCHEME_PREFIX_RE.match(url)
    if scheme_match:
        scheme = scheme_match.group(1).lower()
        if "://" in url:
            if scheme not in ("http", "https"):
                raise ValueError(
                    f"Blocked URL scheme '{scheme}://'. Only http:// and https:// URLs are allowed."
                )
        else:
            # Allow unprefixed host:port (e.g. "example.com:8443/path"), reject script/data/view-source schemes
            if scheme in _BLOCKED_SCHEMES or not _HOST_PORT_ONLY_RE.match(url):
                raise ValueError(
                    f"Blocked URL scheme '{scheme}:'. Only http:// and https:// URLs are allowed."
                )
            url = f"https://{url}"
    else:
        if url.startswith("/") or url.startswith("\\"):
            raise ValueError("Local filesystem paths are not allowed. Provide an http:// or https:// URL.")
        url = f"https://{url}"

    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValueError(f"Invalid navigation URL: {raw_url!r}")
    return url


def _parse_addrinfo_ips(infos: list) -> List[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    ips: List[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4] if len(info) > 4 else ()
        if not sockaddr:
            continue
        ip_str = str(sockaddr[0]).split("%", 1)[0]
        try:
            ips.append(ipaddress.ip_address(ip_str))
        except ValueError:
            continue
    return ips


def _check_literal_ip(hostname: str) -> List[ipaddress.IPv4Address | ipaddress.IPv6Address] | None:
    clean = (hostname or "").strip().strip("[]").split("%", 1)[0]
    try:
        return [ipaddress.ip_address(clean)]
    except ValueError:
        return None


async def validate_target_url_async(
    raw_url: str,
    *,
    allow_private_networks: bool = False,
    allow_about_blank: bool = True,
) -> str:
    """Validate URL scheme and verify target hostname does not resolve to private/loopback/link-local IPs."""
    url = normalize_navigation_url(raw_url, allow_about_blank=allow_about_blank)
    if url == "about:blank" or allow_private_networks:
        return url

    hostname = urlsplit(url).hostname or ""
    if is_blocked_hostname_literal(hostname):
        raise ValueError(f"Blocked private/local hostname: {hostname}")

    ips = _check_literal_ip(hostname)
    if ips is None:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            ips = _parse_addrinfo_ips(infos)
        except OSError:
            ips = []

    for ip_obj in ips:
        if is_blocked_ip(ip_obj):
            raise ValueError(
                f"Blocked non-public address '{ip_obj}' resolved from host '{hostname}'."
            )
    return url


def validate_target_url_sync(
    raw_url: str,
    *,
    allow_private_networks: bool = False,
    allow_about_blank: bool = True,
) -> str:
    """Synchronous variant of validate_target_url_async for non-async callers and unit tests."""
    url = normalize_navigation_url(raw_url, allow_about_blank=allow_about_blank)
    if url == "about:blank" or allow_private_networks:
        return url

    hostname = urlsplit(url).hostname or ""
    if is_blocked_hostname_literal(hostname):
        raise ValueError(f"Blocked private/local hostname: {hostname}")

    ips = _check_literal_ip(hostname)
    if ips is None:
        try:
            infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            ips = _parse_addrinfo_ips(infos)
        except OSError:
            ips = []

    for ip_obj in ips:
        if is_blocked_ip(ip_obj):
            raise ValueError(
                f"Blocked non-public address '{ip_obj}' resolved from host '{hostname}'."
            )
    return url


async def is_route_request_allowed(
    url: str,
    resource_type: str,
    is_navigation: bool,
    *,
    allow_private_networks: bool = False,
) -> bool:
    """Enforce SSRF/scheme guards on every Playwright request, redirect hop, and sub-navigation."""
    lower = (url or "").strip().lower()
    if lower == "about:blank":
        return True

    parts = urlsplit(url)
    scheme = parts.scheme.lower()

    # Inline non-navigation asset URLs (e.g. base64 icons) stay functional; never allow data/blob/script navigation
    if scheme in ("data", "blob"):
        return (not is_navigation) and resource_type in ("image", "font", "stylesheet", "media")

    if scheme not in ("http", "https"):
        return False

    try:
        await validate_target_url_async(
            url,
            allow_private_networks=allow_private_networks,
            allow_about_blank=True,
        )
        return True
    except ValueError:
        return False


def build_minimal_browser_env() -> Dict[str, str]:
    """Return an explicit minimal environment for Chromium, excluding provider/API secrets."""
    allowed = ["PATH", "HOME", "LANG", "DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"]
    if os.name == "nt":
        allowed.extend(["USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "LOCALAPPDATA"])
    return {key: os.environ[key] for key in allowed if key in os.environ}
