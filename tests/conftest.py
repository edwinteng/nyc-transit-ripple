"""Suite-wide fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Tests run offline: any socket connection is an error (fakes must be used)."""
    import socket

    def refuse(*args, **kwargs):
        raise RuntimeError("network access is disabled in tests")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
