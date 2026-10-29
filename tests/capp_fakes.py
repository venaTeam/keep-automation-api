"""Shared CAPP wire fixtures."""

import json
from datetime import datetime, timedelta, timezone
import httpx

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)
IMAGE = "registry.example/automation@sha256:" + "a" * 64
NEXT_IMAGE = "registry.example/automation@sha256:" + "b" * 64
NAME = "automation-12345678-1234-1234-1234-123456789012"
SECRET = NAME + "-run-auth"
ROOT = "https://capp.example/api/v1"
PATH = ROOT + "/clusters/dev/namespaces/team/"
IDENTITY = (
    "service-user-sensitive",
    "password-sensitive",
    "access-sensitive",
    "refresh-sensitive",
)


def tokens(access=IDENTITY[2], expires=None):
    return {
        "accessToken": access,
        "refreshToken": IDENTITY[3],
        "expiresAt": (expires or NOW + timedelta(hours=1)).isoformat(),
    }


def capp(**changes):
    return {
        "name": NAME,
        "namespace": "team",
        "uid": "capp-uid",
        "image": IMAGE,
        "resourceVersion": "42",
        "size": "small",
        "state": "enabled",
        "routeSpec": {
            "hostname": "hello.example",
            "tlsEnabled": True,
            "routeTimeoutSeconds": 600,
        },
        "status": {
            "conditions": [{"type": "Ready", "status": "True", "source": "knative"}]
        },
        **changes,
    }


def secret():
    return {
        "name": SECRET,
        "namespace": "team",
        "uid": "secret-uid",
        "resourceVersion": "43",
        "data": {"old": "secret-sensitive"},
    }


class Wire:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def __call__(self, request):
        assert request.extensions["timeout"] == dict(connect=7, read=7, write=7, pool=7)
        self.calls.append(
            (
                request.method,
                str(request.url),
                json.loads(request.content) if request.content else None,
                dict(request.headers),
            )
        )
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        status, body = reply
        if isinstance(body, bytes):
            return httpx.Response(status, content=body)
        return httpx.Response(status, json=body)
