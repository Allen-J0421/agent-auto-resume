from __future__ import annotations

import json
import socket
from typing import Any, Dict


MAX_MESSAGE_BYTES = 1024 * 1024


def request(socket_path: str, message: Dict[str, Any], timeout: float = 10.0) -> Dict[str, Any]:
    """Send one bounded JSON request to the supervisor and return its response."""
    payload = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
    if len(payload) > MAX_MESSAGE_BYTES:
        raise ValueError("agent-resume message is too large")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(socket_path)
        connection.sendall(payload)
        chunks = bytearray()
        while b"\n" not in chunks:
            part = connection.recv(65536)
            if not part:
                break
            chunks.extend(part)
            if len(chunks) > MAX_MESSAGE_BYTES:
                raise ValueError("agent-resume response is too large")
        line = bytes(chunks).split(b"\n", 1)[0]
        if not line:
            return {}
        result = json.loads(line.decode("utf-8"))
        return result if isinstance(result, dict) else {}
    finally:
        connection.close()


def encode_response(value: Dict[str, Any]) -> bytes:
    return (json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8")
