"""Async client for the Ollama REST API."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

REQUEST_TIMEOUT = 30.0
PING_TIMEOUT = 5.0


class OllamaError(Exception):
    """Any failure talking to Ollama. The message is safe to show to the user."""


class OllamaConnectionError(OllamaError):
    """Ollama is unreachable or the connection dropped."""


@dataclass(frozen=True)
class ModelInfo:
    name: str
    size: int
    family: str
    parameter_size: str
    quantization: str
    modified: str

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> ModelInfo:
        details = raw.get("details") or {}
        return cls(
            name=raw.get("name") or raw.get("model") or "",
            size=raw.get("size") or 0,
            family=details.get("family") or "-",
            parameter_size=details.get("parameter_size") or "-",
            quantization=details.get("quantization_level") or "-",
            modified=(raw.get("modified_at") or "")[:10],
        )


@dataclass(frozen=True)
class PullProgress:
    status: str
    total: int = 0
    completed: int = 0


@dataclass(frozen=True)
class ChatChunk:
    content: str = ""
    thinking: str = ""
    done: bool = False
    eval_count: int = 0
    eval_duration: int = 0  # nanoseconds


def _error_message(body: bytes, status: int) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        data = None
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"])
    return f"Ollama returned HTTP {status}"


class OllamaClient:
    def __init__(self, base_url: str) -> None:
        # No read timeout on the client: loading a large model can take minutes
        # before the first byte. Short calls pass their own timeout.
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(connect=PING_TIMEOUT, read=None, write=10.0, pool=PING_TIMEOUT),
        )

    @property
    def base_url(self) -> str:
        return str(self._client.base_url).rstrip("/")

    @base_url.setter
    def base_url(self, value: str) -> None:
        self._client.base_url = httpx.URL(value)

    async def close(self) -> None:
        await self._client.aclose()

    def _translate(self, exc: httpx.TransportError) -> OllamaError:
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return OllamaConnectionError(f"Cannot reach Ollama at {self.base_url}")
        if isinstance(exc, (httpx.ReadError, httpx.RemoteProtocolError)):
            return OllamaConnectionError("Connection to Ollama was lost")
        if isinstance(exc, httpx.TimeoutException):
            return OllamaError("Request to Ollama timed out")
        return OllamaError(f"Network error: {exc}")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: float = REQUEST_TIMEOUT,
    ) -> httpx.Response:
        try:
            resp = await self._client.request(method, path, json=body, timeout=timeout)
        except httpx.TransportError as exc:
            raise self._translate(exc) from exc
        if resp.is_error:
            raise OllamaError(_error_message(resp.content, resp.status_code))
        return resp

    @staticmethod
    def _json(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            data = None
        if not isinstance(data, dict):
            raise OllamaError("Unexpected response from server (is this really Ollama?)")
        return data

    async def _stream(self, path: str, body: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Yield each JSON line of a streaming POST response.

        Stops with OllamaError if the server reports an error mid-stream.
        Wrap the iterator in contextlib.aclosing() when breaking out early so
        the connection is released.
        """
        try:
            async with self._client.stream("POST", path, json=body) as resp:
                if resp.is_error:
                    await resp.aread()
                    raise OllamaError(_error_message(resp.content, resp.status_code))
                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(data, dict):
                        continue
                    if "error" in data:
                        raise OllamaError(str(data["error"]))
                    yield data
        except httpx.TransportError as exc:
            raise self._translate(exc) from exc

    async def version(self) -> str:
        resp = await self._request("GET", "/api/version", timeout=PING_TIMEOUT)
        return str(self._json(resp).get("version", "unknown"))

    async def list_models(self) -> list[ModelInfo]:
        resp = await self._request("GET", "/api/tags")
        return [ModelInfo.from_api(m) for m in self._json(resp).get("models") or []]

    async def running_models(self) -> list[str]:
        resp = await self._request("GET", "/api/ps")
        models = self._json(resp).get("models") or []
        return [m.get("name") or m.get("model") or "" for m in models]

    async def delete(self, name: str) -> None:
        # Older Ollama versions expect "name", newer ones "model".
        await self._request("DELETE", "/api/delete", body={"model": name, "name": name})

    async def unload(self, name: str) -> None:
        await self._request(
            "POST",
            "/api/generate",
            body={"model": name, "keep_alive": 0, "stream": False},
        )

    async def pull(self, name: str) -> AsyncIterator[PullProgress]:
        body = {"model": name, "name": name, "stream": True}
        async for data in self._stream("/api/pull", body):
            yield PullProgress(
                status=str(data.get("status") or ""),
                total=data.get("total") or 0,
                completed=data.get("completed") or 0,
            )

    async def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        temperature: float,
        num_ctx: int,
    ) -> AsyncIterator[ChatChunk]:
        body = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature, "num_ctx": num_ctx},
        }
        async for data in self._stream("/api/chat", body):
            message = data.get("message") or {}
            yield ChatChunk(
                content=message.get("content") or "",
                thinking=message.get("thinking") or "",
                done=bool(data.get("done")),
                eval_count=data.get("eval_count") or 0,
                eval_duration=data.get("eval_duration") or 0,
            )
