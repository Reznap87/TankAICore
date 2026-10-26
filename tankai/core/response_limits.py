"""Fixed byte bounds for opt-in provider responses; SDK imports stay optional."""
from __future__ import annotations

from .request_dispatch import DispatchDenied


MAX_RESPONSE_BYTES = 256_000
MAX_OUTPUT_BYTES = 128_000


def bounded_text(text: str) -> str:
    """Bound the returned text independently of the requested token ceiling."""
    if type(text) is not str or len(text) > MAX_OUTPUT_BYTES:
        raise DispatchDenied("provider output exceeds the text byte limit")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise DispatchDenied("provider output is not valid UTF-8 text") from None
    if size > MAX_OUTPUT_BYTES:
        raise DispatchDenied("provider output exceeds the text byte limit")
    return text


def bounded_transport(inner):
    """Limit streaming bytes before SDK buffering, decompression or JSON parsing.

    The protected path requests identity encoding and rejects other encodings,
    so a compressed body cannot inflate after the transport's byte check. The
    actual stream is counted even when Content-Length is absent or inaccurate.
    """
    import httpx2

    class BoundedStream(httpx2.SyncByteStream):
        def __init__(self, stream):
            self.stream = stream
            self.closed = False

        def __iter__(self):
            size = 0
            try:
                for chunk in self.stream:
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise DispatchDenied("provider response exceeds the body byte limit")
                    yield chunk
            finally:
                self.close()

        def close(self):
            if not self.closed:
                self.closed = True
                self.stream.close()

    class BoundedTransport(httpx2.BaseTransport):
        def handle_request(self, request):
            request.headers["accept-encoding"] = "identity"
            response = inner.handle_request(request)
            try:
                if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                    raise DispatchDenied("encoded provider responses are not allowed")
                declared = response.headers.get("content-length")
                if declared is not None and (
                    not 1 <= len(declared) <= 7 or not declared.isascii()
                    or not declared.isdecimal() or int(declared) > MAX_RESPONSE_BYTES
                ):
                    raise DispatchDenied("invalid or oversized provider response length")
                # Native HTTPTransport streams. Also bound already-buffered
                # responses from an in-process transport before SDK parsing.
                if response.is_stream_consumed and len(response.content) > MAX_RESPONSE_BYTES:
                    raise DispatchDenied("provider response exceeds the body byte limit")
                response.stream = BoundedStream(response.stream)
                return response
            except BaseException:
                response.close()
                raise

        def close(self):
            inner.close()

    return BoundedTransport()
