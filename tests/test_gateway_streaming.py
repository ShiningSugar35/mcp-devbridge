from __future__ import annotations

import httpx
import pytest

from local_dev_mcp_bridge.gateway import _read_and_close_upstream, _stream_and_close_upstream


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
