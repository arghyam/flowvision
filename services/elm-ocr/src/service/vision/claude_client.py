"""
Claude client for one vision request, through Anthropic's Python SDK. Used when the provider is "claude"
(METER_PROVIDER); it keeps the contract and billing rules of openai_client.OpenAIVisionClient, so the reader and the
checks do not depend on which model answered:
- read() returns {"answer": dict, "usage": {"prompt_tokens", "completion_tokens"}, "model": str, "seconds": float}
  and raises ModelError (retryable / alert / usage / maybe_billed) when the call fails;
- the same prompt and answer schema (prompt.py); Claude's structured outputs hold the answer to the schema;
- only the configured model reads the photo: no fallback to another model;
- one retry at most, only for errors Anthropic did not bill (rate limit, overloaded, server error, a connection that
  never opened), and never after a timeout or a dropped connection, which may be billed. The SDK's own retries are off.
"""
import base64
import io
import json
import logging
import random
import time

import anthropic
import httpx2

from .images import ImageError
from .openai_client import MAX_RETRY_WAIT, MIN_ATTEMPT_SECONDS, ModelError, _retry_after
from .prompt import PROMPT, SCHEMA

RETRY_STATUS = (429, 500, 502, 503, 529)            # not billed: retried once when the deadline allows
MAX_IMAGE_BASE64 = 5 * 1024 * 1024                  # Anthropic's limit per image, as sent (base64)
CONNECT_TIMEOUT = 5.0                               # a dead network fails fast, while a retry still fits the deadline

# At DEBUG level the SDK logs each request's options, which include the whole photo (base64): never let it.
for _name in ("anthropic", "httpx2", "httpcore2"):
    logging.getLogger(_name).setLevel(logging.INFO)


def claude_schema(schema):
    """The answer schema in the form Claude's structured outputs document: a list of types ("type": ["string",
    "null"]) is written as anyOf. Everything else is unchanged."""
    if isinstance(schema, list):
        return [claude_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out = {key: claude_schema(value) for key, value in schema.items()}
    if isinstance(out.get("type"), list):
        out = {"anyOf": [{"type": t} for t in out.pop("type")], **out}
    return out


def fit_image(image_bytes, mime):
    """The photo within Anthropic's per-image limit. The reader already shrinks photos to 2048 px, so only a rare large
    PNG is over it: that one is sent re-encoded as JPEG. Raises ImageError (no model call is made)."""
    if len(image_bytes) * 4 / 3 <= MAX_IMAGE_BASE64:
        return image_bytes, mime
    from PIL import Image
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:  # noqa: BLE001 - Pillow raises many types for corrupt files
        raise ImageError(f"the photo could not be prepared for Claude: {type(e).__name__}") from e
    for quality in (90, 80, 70):
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality)
        if out.tell() * 4 / 3 <= MAX_IMAGE_BASE64:
            return out.getvalue(), "image/jpeg"
    raise ImageError("the photo is too large to send to Claude (over 5 MB as JPEG)")


def _usage(usage):
    """Token counts as the reader prices them: everything read as input (cache tokens included), and the output with
    the thinking, which bills as output."""
    if usage is None:
        return {"prompt_tokens": 0, "completion_tokens": 0}
    read = sum(getattr(usage, name, 0) or 0
               for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return {"prompt_tokens": read, "completion_tokens": getattr(usage, "output_tokens", 0) or 0}


def _status(e):
    return f"Anthropic HTTP {e.status_code}" + (f" ({e.type})" if getattr(e, "type", None) else "")


class ClaudeVisionClient:
    def __init__(self, settings, attempts=None, http_client=None):
        """http_client: an anthropic.DefaultHttpxClient, for tests (a mock transport); None in normal use."""
        self.settings = settings
        self.attempts = attempts or settings.max_attempts
        # a key not scoped to a workspace (sk-ant-usr...) must name the workspace in this header on every request
        headers = {"anthropic-workspace-id": settings.anthropic_workspace_id} if settings.anthropic_workspace_id else None
        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key, base_url=settings.anthropic_base_url,
                                          max_retries=0, timeout=settings.request_timeout, http_client=http_client,
                                          default_headers=headers)
        self.schema = claude_schema(SCHEMA)

    def read(self, image_bytes, mime="image/jpeg", deadline=None):
        """Returns {"answer": dict, "usage": dict, "model": str, "seconds": float}. Raises ModelError.
        deadline: time.monotonic() value by which the answer is needed (None: request_timeout per attempt)."""
        s = self.settings
        data, mime = fit_image(image_bytes, mime)
        output_config = {"format": {"type": "json_schema", "schema": self.schema}}
        if s.reasoning_effort:
            output_config["effort"] = s.reasoning_effort
        params = {
            "model": s.model,
            "max_tokens": 16000,
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime,
                                             "data": base64.standard_b64encode(data).decode()}},
                {"type": "text", "text": PROMPT},
            ]}],
        }

        t0 = time.time()
        message = self._send(params, deadline)
        if not isinstance(message, anthropic.types.Message):
            # HTTP 200 whose body is not a message (e.g. plain text from a proxy): answered, possibly billed
            raise ModelError("Anthropic returned an unreadable response", retryable=True, maybe_billed=True)
        usage = _usage(message.usage)                 # billed from here on, even if the answer is unusable
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ModelError("Claude declined to read the photo" + (f" ({category})" if category else ""), usage=usage)
        if message.stop_reason == "max_tokens":
            raise ModelError("model output was cut off (stop_reason=max_tokens)", usage=usage)
        text = next((block.text for block in message.content or [] if block.type == "text"), None)
        if not text:
            raise ModelError("model returned no content", usage=usage)
        try:
            answer = json.loads(text)
        except json.JSONDecodeError as e:
            raise ModelError("model returned invalid JSON", usage=usage) from e
        if not isinstance(answer, dict):
            raise ModelError("model returned JSON that is not an object", usage=usage)
        return {"answer": answer, "usage": usage, "model": message.model or s.model,
                "seconds": round(time.time() - t0, 2)}

    def _remaining(self, deadline):
        return self.settings.request_timeout if deadline is None else deadline - time.monotonic()

    def _send(self, params, deadline):
        last = None
        for attempt in range(self.attempts):
            remaining = self._remaining(deadline)
            if remaining < MIN_ATTEMPT_SECONDS:
                raise last or ModelError("no time left for the model call before the request deadline", retryable=True)
            timeout = min(self.settings.request_timeout, remaining)
            wait = None
            try:
                return self.client.messages.create(
                    **params, timeout=anthropic.Timeout(timeout, connect=min(CONNECT_TIMEOUT, timeout)))
            except ValueError as e:
                # HTTP 200 whose body is cut off or not valid JSON: answered, possibly billed, so not sent again
                raise ModelError("Anthropic returned an unreadable response", retryable=True,
                                 maybe_billed=True) from e
            except anthropic.APITimeoutError as e:
                if not isinstance(e.__cause__, httpx2.ConnectTimeout):
                    # The request was sent: Anthropic may still finish and bill it, so it is not sent again here.
                    raise ModelError(f"Claude did not answer within {timeout:.0f} s", retryable=True,
                                     maybe_billed=True) from e
                last = ModelError("cannot reach Anthropic", retryable=True)     # never connected: nothing sent
            except anthropic.APIConnectionError as e:
                if not isinstance(e.__cause__, httpx2.ConnectError):
                    # dropped or cut off after the request may have been sent: not retried (it could be billed twice)
                    raise ModelError("the connection to Anthropic was lost or its answer was cut off", retryable=True,
                                     maybe_billed=True) from e
                last = ModelError("cannot reach Anthropic", retryable=True)     # failed while connecting: not sent
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                raise ModelError(f"Anthropic rejected the API key (HTTP {e.status_code})", alert=True) from e
            except anthropic.NotFoundError as e:
                raise ModelError(f"Anthropic does not know the model {self.settings.model!r} (HTTP 404)",
                                 alert=True) from e
            except anthropic.APIStatusError as e:
                if e.status_code == 402 or getattr(e, "type", None) == "billing_error":
                    raise ModelError("Anthropic account has a billing problem, e.g. out of credit (HTTP 402)",
                                     alert=True) from e
                last = ModelError(_status(e), retryable=e.status_code in RETRY_STATUS or e.status_code >= 500)
                if e.status_code not in RETRY_STATUS:
                    raise ModelError(f"{_status(e)}: {str(e.message)[:200]}", retryable=last.retryable) from e
                wait = _retry_after(e.response.headers)
            if attempt < self.attempts - 1:
                wait = max(0.0, min(MAX_RETRY_WAIT, wait if wait is not None else random.uniform(1.0, 4.0)))
                if self._remaining(deadline) - wait < MIN_ATTEMPT_SECONDS:
                    raise last
                time.sleep(wait)
        raise last
