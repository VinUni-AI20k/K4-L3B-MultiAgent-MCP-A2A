"""Day09 student starter kit."""

import socket

_orig_getaddrinfo = socket.getaddrinfo

def _fast_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host == "day09-competition.34-142-201-239.sslip.io":
        host = "34.142.201.239"
    return _orig_getaddrinfo(host, port, family, type, proto, flags)

socket.getaddrinfo = _fast_getaddrinfo

VARIANT_ID = "l3b"
OUTPUT_SCHEMA_VERSION = "day09-l3b-output-v2"

__all__ = ["OUTPUT_SCHEMA_VERSION", "VARIANT_ID"]
