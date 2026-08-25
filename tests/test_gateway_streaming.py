from __future__ import annotations

import asyncio

import httpx
import pytest

from local_dev_mcp_bridge.gateway import (
    _read_and_close_upstream,
    _stream_and_close_upstream,
    _stream_sse_with_keepalive_and_close_upstream,
    _upstream_is_sse,
)


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.iterations = 0
        self.closed = 0

    async def __aiter__(self):
        self.iterations += 1
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed += 1


class DelayedStream(httpx.AsyncByteStream):
    def __init__(self, delay_seconds: float, chunks: list[bytes]) -> None:
        self.delay_seconds = delay_seconds
        self.chunks = chunks
        self.iterations = 0
        self.closed = 0

    async def __aiter__(self):
        self.iterations += 1
        for chunk in self.chunks:
            await asyncio.sleep(self.delay_seconds)
            yield chunk

    async def aclose(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_buffered_upstream_is_read_once_and_closed() -> None:
    stream = CountingStream([b"one", b"two"])
    response = httpx.Response(200, stream=stream)

    payload = await _read_and_close_upstream(response)

    assert payload == b"onetwo"
    assert stream.iterations == 1
    assert stream.closed == 1
    assert response.is_closed


@pytest.mark.asyncio
async def test_streaming_upstream_has_single_owner_and_closes() -> None:
    stream = CountingStream([b"one", b"two"])
    response = httpx.Response(200, stream=stream)

    chunks = [chunk async for chunk in _stream_and_close_upstream(response)]

    assert chunks == [b"one", b"two"]
    assert stream.iterations == 1
    assert stream.closed == 1
    assert response.is_closed


@pytest.mark.asyncio
async def test_streaming_upstream_closes_on_downstream_cancel() -> None:
    stream = CountingStream([b"one", b"two"])
    response = httpx.Response(200, stream=stream)
    iterator = _stream_and_close_upstream(response)

    assert await anext(iterator) == b"one"
    await iterator.aclose()

    assert stream.iterations == 1
    assert stream.closed == 1
    assert response.is_closed


@pytest.mark.asyncio
async def test_sse_keepalive_emits_during_idle_and_preserves_payload() -> None:
    stream = DelayedStream(0.12, [b'data: {"jsonrpc":"2.0"}\n\n'])
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream; charset=utf-8"},
        stream=stream,
    )

    chunks = [
        chunk
        async for chunk in _stream_sse_with_keepalive_and_close_upstream(
            response, keepalive_seconds=0.05
        )
    ]

    assert chunks[-1] == b'data: {"jsonrpc":"2.0"}\n\n'
    assert any(chunk == b": devbridge-keepalive\n\n" for chunk in chunks[:-1])
    assert stream.iterations == 1
    assert stream.closed == 1
    assert response.is_closed


@pytest.mark.asyncio
async def test_sse_keepalive_never_splits_partial_upstream_event() -> None:
    stream = DelayedStream(0.12, [b"data: part", b"ial\n\n"])
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=stream,
    )

    chunks = [
        chunk
        async for chunk in _stream_sse_with_keepalive_and_close_upstream(
            response, keepalive_seconds=0.05
        )
    ]
    partial_index = chunks.index(b"data: part")
    complete_index = chunks.index(b"ial\n\n")
    assert b": devbridge-keepalive\n\n" not in chunks[partial_index + 1 : complete_index]


@pytest.mark.asyncio
async def test_sse_keepalive_closes_on_downstream_cancel() -> None:
    stream = DelayedStream(0.2, [b"data: late\n\n"])
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)
    iterator = _stream_sse_with_keepalive_and_close_upstream(
        response, keepalive_seconds=0.05
    )

    assert await anext(iterator) == b": devbridge-keepalive\n\n"
    await iterator.aclose()

    assert stream.iterations == 1
    assert stream.closed == 1
    assert response.is_closed


def test_upstream_sse_content_type_detection() -> None:
    sse = httpx.Response(200, headers={"content-type": "text/event-stream; charset=utf-8"})
    json_response = httpx.Response(200, headers={"content-type": "application/json"})

    assert _upstream_is_sse(sse)
    assert not _upstream_is_sse(json_response)
