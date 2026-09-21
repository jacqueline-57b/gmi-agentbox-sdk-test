"""Builders for the API payloads the fake transport serves.

Keeping the shapes in one place means a backend field rename is a one-line
fix here instead of a sweep through every test.
"""

from __future__ import annotations

from typing import Any, Dict


def agent_payload(**overrides: Any) -> Dict[str, Any]:
    payload = {
        "id": "tmpl_123",
        "slug": "agentbox-demo",
        "title": "agentbox-demo",
        "idc": "us-central-iowa2",
        "image_url": "docker.io/library/alpine:3.20",
        "runtime": "sandbox",
        "template_build_status": "ready",
        "template_build_error": "",
        "env": [],
        "revision": 1,
    }
    payload.update(overrides)
    return payload


def sandbox_payload(**overrides: Any) -> Dict[str, Any]:
    payload = {
        "id": "task_456",
        "task_status": "running",
        "deployment_id": "tmpl_123",
        "deployment_slug": "agentbox-demo",
        "idc_name": "us-central-iowa2",
        "instance_type": "gmi.sandbox.x-small",
        "endpoint_url": "https://sandbox.example/task_456",
        "capabilities": {"logs": True, "logs_stream": True, "metrics": True},
    }
    payload.update(overrides)
    return payload


def launch_payload(**overrides: Any) -> Dict[str, Any]:
    """POST /deployments/{slug}/tasks answers with this narrower shape."""
    payload = {
        "task_id": "task_456",
        "container_id": "container_789",
        "status": "creating",
    }
    payload.update(overrides)
    return payload


def execution_payload(**overrides: Any) -> Dict[str, Any]:
    payload = {
        "execution_id": "exec_1",
        "status": "completed",
        "exit_code": 0,
        "stdout": "hello\n",
        "stderr": "",
    }
    payload.update(overrides)
    return payload


def page_payload(items, *, total=None, page=1, page_size=20) -> Dict[str, Any]:
    items = list(items)
    return {
        "items": items,
        "total": len(items) if total is None else total,
        "page": page,
        "page_size": page_size,
    }


def error_payload(message: str, *, code=None, details=None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"error": message}
    if code is not None:
        payload["code"] = code
    if details is not None:
        payload["details"] = details
    return payload
