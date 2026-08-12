"""Typed REST client for the prima-pool control plane."""
from __future__ import annotations

from typing import Any

import httpx

from .models import (
    ClusterConfig,
    ClusterStatusResponse,
    Worker,
    WorkerState,
)


class PoolError(Exception):
    """Raised for non-2xx responses, carrying the RFC 7807 problem body."""

    def __init__(self, status: int, problem: dict[str, Any]) -> None:
        self.status = status
        self.problem = problem
        super().__init__(problem.get("detail") or problem.get("title") or f"HTTP {status}")


class PoolClient:
    def __init__(self, base_url: str, api_key: str = "", timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._client = httpx.Client(timeout=timeout)

    def _headers(self, bearer: str | None = None) -> dict[str, str]:
        token = bearer or self.api_key
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    def _request(self, method: str, path: str, *, bearer: str | None = None, json: Any = None) -> Any:
        resp = self._client.request(
            method,
            f"{self.base_url}{path}",
            headers=self._headers(bearer),
            json=json,
        )
        if resp.status_code >= 400:
            try:
                problem = resp.json()
            except ValueError:
                problem = {"title": resp.text, "status": resp.status_code}
            raise PoolError(resp.status_code, problem)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    # ── accounts ─────────────────────────────────────────────────────────
    def register_account(self, username: str, password: str) -> dict[str, Any]:
        return self._request("POST", "/v1/accounts/register", json={"username": username, "password": password})

    def login(self, username: str, password: str) -> dict[str, Any]:
        return self._request("POST", "/v1/accounts/login", json={"username": username, "password": password})

    def create_key(self, account_id: str, name: str, scope: str, session_token: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/v1/accounts/{account_id}/keys",
            bearer=session_token,
            json={"name": name, "scope": scope},
        )

    def list_keys(self, account_id: str, session_token: str) -> list[dict[str, Any]]:
        return self._request("GET", f"/v1/accounts/{account_id}/keys", bearer=session_token)

    def revoke_key(self, account_id: str, key_id: str, session_token: str) -> None:
        self._request("DELETE", f"/v1/accounts/{account_id}/keys/{key_id}", bearer=session_token)

    # ── workers ──────────────────────────────────────────────────────────
    def register_worker(self, payload: dict[str, Any]) -> Worker:
        data = self._request("POST", "/v1/workers/register", json=payload)
        return Worker(**data)

    def get_worker_state(self, worker_id: str) -> WorkerState:
        data = self._request("GET", f"/v1/workers/{worker_id}/state")
        return WorkerState(**data)

    def heartbeat(self, worker_id: str) -> Worker:
        data = self._request("POST", f"/v1/workers/{worker_id}/heartbeat")
        return Worker(**data)

    def revoke_worker(self, worker_id: str) -> None:
        self._request("DELETE", f"/v1/workers/{worker_id}")

    # ── clusters ─────────────────────────────────────────────────────────
    def get_cluster_config(self, cluster_id: str) -> ClusterConfig:
        data = self._request("GET", f"/v1/clusters/{cluster_id}/config")
        return ClusterConfig(**data)

    def report_ready(self, cluster_id: str, layer_windows: dict[str, int] | None = None) -> ClusterStatusResponse:
        body: dict[str, Any] = {}
        if layer_windows is not None:
            body["layer_windows"] = layer_windows
        data = self._request("POST", f"/v1/clusters/{cluster_id}/ready", json=body)
        return ClusterStatusResponse(**data)

    def close(self) -> None:
        self._client.close()
