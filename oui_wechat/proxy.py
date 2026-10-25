"""Forwarded headers are only meaningful behind explicitly trusted proxy addresses."""

from __future__ import annotations

import ipaddress
import os


class ProxyPolicy:
    def __init__(self):
        self.networks = tuple(
            ipaddress.ip_network(value.strip(), strict=False)
            for value in os.environ.get("OUI_TRUSTED_PROXIES", "").split(",") if value.strip()
        )

    def trusted(self, value: str) -> bool:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return False
        return any(address in network for network in self.networks)

    def peer(self, request) -> str:
        peer = request.remote or "unknown"
        if not self.trusted(peer):
            return peer
        forwarded = request.headers.get("X-Forwarded-For", "")
        if not forwarded:
            return peer
        chain = forwarded.split(",")
        if len(chain) > 16:
            return peer
        for candidate in reversed(chain):
            if not self.trusted(peer):
                break
            try:
                peer = str(ipaddress.ip_address(candidate.strip()))
            except ValueError:
                return request.remote or "unknown"
        return peer

    def secure(self, request) -> bool:
        return request.secure or (
            self.trusted(request.remote or "")
            and request.headers.get("X-Forwarded-Proto", "").lower() == "https"
        )
