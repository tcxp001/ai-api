"""Strict request-body decompression, independent of response inspection limits."""

import zlib

DECODE_INPUT_CHUNK = 16384


class RequestContentError(Exception):
    """A request decoding failure with fields safe to include in an HTTP error."""

    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _invalid_body() -> RequestContentError:
    return RequestContentError(
        400, "invalid_compressed_body", "Invalid compressed request body."
    )


def _decode_frames(body: bytes, make_decoder, concatenated: bool = False) -> bytes:
    """Bound unused_data suffix copying even for many tiny concatenated frames."""
    parts = []
    position = 0
    pending = b""
    decoder = make_decoder()
    while True:
        if not pending:
            pending = body[position:position + DECODE_INPUT_CHUNK]
            position += len(pending)
        output = decoder.decompress(pending)
        if output:
            parts.append(output)
        pending = decoder.unused_data if decoder.eof else b""
        if decoder.eof:
            if not pending and position == len(body):
                return b"".join(parts)
            if not concatenated:
                raise ValueError("Unexpected data after compressed stream")
            decoder = make_decoder()
        elif position == len(body):
            raise ValueError("Incomplete compressed stream")


def _decode_zlib(body: bytes, wbits: int, concatenated: bool = False) -> bytes:
    return _decode_frames(body, lambda: zlib.decompressobj(wbits), concatenated)


def _decode_zstd(body: bytes) -> bytes:
    # Keep unrelated requests working on existing installations that have not
    # installed the new dependency yet. Never send compressed bytes to JSON.
    try:
        import zstandard
    except (ImportError, OSError):
        raise RequestContentError(
            503,
            "content_decoder_unavailable",
            "Request content decoder is unavailable.",
        ) from None

    try:
        return _decode_frames(body, lambda: zstandard.ZstdDecompressor().decompressobj(),
                              concatenated=True)
    except (zstandard.ZstdError, ValueError):
        raise _invalid_body() from None


def decode_request_body(body: bytes, content_encoding: str) -> bytes:
    """Decode a complete HTTP request body before parsing it as JSON.

    ``content_encoding`` is the comma-joined value of all Content-Encoding
    headers, or an empty string when absent. Codings are case-insensitive and
    reversed for decoding. Empty list entries and identity are ignored; without
    compression the original bytes object is returned, with no JSON validation.

    Unsupported codings raise RequestContentError (415); malformed, truncated,
    or trailing-junk streams raise it with 400. An unavailable zstandard runtime
    raises it with 503. Error fields never include body/header/decoder details.

    This helper has no response limits or truncation and does not mutate headers.
    The caller owns request admission limits, error responses, and removal or
    regeneration of Content-Encoding/Content-Length/Transfer-Encoding after
    decoding. Do not fall back to forwarding compressed bytes after an error.
    """
    codings = [
        coding
        for value in content_encoding.split(",")
        if (coding := value.strip().lower()) and coding != "identity"
    ]
    supported = {"gzip", "x-gzip", "deflate", "zstd", "zst"}
    # Reject the whole stack before producing any partially decoded output.
    if any(coding not in supported for coding in codings):
        raise RequestContentError(
            415,
            "unsupported_content_encoding",
            "Unsupported request content encoding.",
        )

    for coding in reversed(codings):
        try:
            if coding in {"gzip", "x-gzip"}:
                body = _decode_zlib(body, 16 + zlib.MAX_WBITS, concatenated=True)
            elif coding == "deflate":
                try:
                    body = _decode_zlib(body, zlib.MAX_WBITS)
                except (zlib.error, ValueError):
                    # RFC deflate is zlib-wrapped; accept raw deflate clients
                    # too, but only if their entire stream is valid.
                    body = _decode_zlib(body, -zlib.MAX_WBITS)
            else:
                body = _decode_zstd(body)
        except (zlib.error, ValueError):
            raise _invalid_body() from None
    return body
