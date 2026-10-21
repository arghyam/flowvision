"""Minimal OpenAI Chat Completions client for one vision request (standard library only)."""
import base64
import http.client
import json
import random
import socket
import time
import urllib.error
import urllib.request

from . import net
from .prompt import PROMPT, SCHEMA

# Retried once, when the deadline allows: these responses were not billed. 429 "insufficient_quota" is not retried.
RETRY_STATUS = (429, 500, 502, 503)
MIN_ATTEMPT_SECONDS = 10.0          # an attempt with less time left than this is not started
MAX_RETRY_WAIT = 20.0


class ModelError(RuntimeError):
    """The model call failed or returned something unusable. `retryable`: sending the request again later can work.
    Raised by both model clients. `alert`: needs an operator (e.g. the provider account is out of credit, or the key
    was rejected). `usage`: token usage of a call that was answered (and billed) but unusable; `maybe_billed`: no
    answer, but the provider may still bill it (a timeout).
    `cost_usd` is filled in by the reader."""

    def __init__(self, message, retryable=False, alert=False, usage=None, maybe_billed=False):
        super().__init__(message)
        self.retryable = retryable
        self.alert = alert
        self.usage = usage or {}
        self.maybe_billed = maybe_billed
        self.cost_usd = 0.0


def _retry_after(headers) -> float | None:
    for name in ("retry-after-ms", "retry-after"):
        value = headers.get(name) if headers else None
        if value:
            try:
                return float(value) / (1000.0 if name.endswith("-ms") else 1.0)
            except ValueError:
                return None
    return None


def _read_all(response, end: float) -> bytes:
    """Reads a response body in small pieces, so a server that trickles bytes cannot outlast `end`."""
    chunks = []
    while chunk := response.read1(64 * 1024):
        chunks.append(chunk)
        if time.monotonic() > end:
            raise TimeoutError("response body took too long")
    return b"".join(chunks)


def _error_code(body: str) -> str | None:
    try:
        return (json.loads(body).get("error") or {}).get("code")
    except (ValueError, AttributeError):
        return None


class OpenAIVisionClient:
    def __init__(self, settings, attempts=None):
        self.settings = settings
        self.attempts = attempts or settings.max_attempts

    def read(self, image_bytes, mime="image/jpeg", deadline=None):
        """Returns {"answer": dict, "usage": dict, "model": str, "seconds": float}. Raises ModelError.
        deadline: time.monotonic() value by which the answer is needed (None: request_timeout per attempt)."""
        s = self.settings
        body = {
            "model": s.model,
            "max_completion_tokens": 16000,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "meter_reading", "strict": True, "schema": SCHEMA}},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64," + base64.b64encode(image_bytes).decode(),
                    "detail": s.image_detail}},
            ]}],
        }
        if s.reasoning_effort:
            body["reasoning_effort"] = s.reasoning_effort
        req = urllib.request.Request(
            f"{s.openai_base_url}/chat/completions", data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {s.openai_api_key}", "Content-Type": "application/json"})

        t0 = time.time()
        resp = self._send(req, deadline)
        if not isinstance(resp, dict):
            raise ModelError("OpenAI returned an unreadable response", retryable=True, maybe_billed=True)
        usage = resp.get("usage")                     # billed from here on, even if the answer is unusable
        if not isinstance(usage, dict) or not all(isinstance(usage.get(k, 0), (int, float))
                                                  and not isinstance(usage.get(k, 0), bool)
                                                  for k in ("prompt_tokens", "completion_tokens")):
            raise ModelError("OpenAI returned an answer without usable token counts", retryable=True,
                             maybe_billed=True)
        choices = resp.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        message = choice.get("message")
        message = message if isinstance(message, dict) else {}
        if message.get("refusal"):
            raise ModelError("the model refused to read the photo", usage=usage)
        if choice.get("finish_reason") == "length":
            raise ModelError("model output was cut off (finish_reason=length)", usage=usage)
        content = message.get("content")
        if not content or not isinstance(content, str):
            raise ModelError("model returned no content", usage=usage)
        try:
            answer = json.loads(content)
        except json.JSONDecodeError as e:
            raise ModelError("model returned invalid JSON", usage=usage) from e
        if not isinstance(answer, dict):
            raise ModelError("model returned JSON that is not an object", usage=usage)
        return {"answer": answer, "usage": usage, "model": resp.get("model", s.model),
                "seconds": round(time.time() - t0, 2)}

    def _remaining(self, deadline):
        return self.settings.request_timeout if deadline is None else deadline - time.monotonic()

    def _send(self, req, deadline):
        last = None
        for attempt in range(self.attempts):
            remaining = self._remaining(deadline)
            if remaining < MIN_ATTEMPT_SECONDS:
                raise last or ModelError("no time left for the model call before the request deadline", retryable=True)
            timeout = min(self.settings.request_timeout, remaining)
            end = time.monotonic() + timeout          # one clock for the status line, headers and body
            watch = net.Watch(end)
            wait = None
            try:
                with net.build_opener(watch).open(req, timeout=timeout) as r:
                    body = _read_all(r, end)
                if watch.expired:
                    raise TimeoutError("the answer took too long")
                try:
                    return json.loads(body)
                except ValueError as e:      # answered (maybe billed) but cut off or not JSON: not sent again
                    raise ModelError("OpenAI returned an unreadable response", retryable=True,
                                     maybe_billed=True) from e
            except ModelError:
                raise
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors="replace")[:500]
                code = _error_code(body)
                if e.code == 429 and code == "insufficient_quota":
                    raise ModelError("OpenAI account is out of credit (insufficient_quota)", alert=True) from e
                if e.code in (401, 403):
                    raise ModelError(f"OpenAI rejected the API key (HTTP {e.code})", alert=True) from e
                last = ModelError(f"OpenAI HTTP {e.code}" + (f" ({code})" if code else ""),
                                  retryable=e.code in RETRY_STATUS or e.code >= 500)
                if e.code not in RETRY_STATUS:
                    raise last from e
                wait = _retry_after(e.headers)
            except (TimeoutError, socket.timeout) as e:
                # The request was sent: OpenAI may still finish and bill it, so it is not sent again here.
                raise ModelError(f"OpenAI did not answer within {timeout:.0f} s", retryable=True,
                                 maybe_billed=True) from e
            except urllib.error.URLError as e:
                if watch.expired or isinstance(e.reason, (TimeoutError, socket.timeout)):
                    raise ModelError(f"OpenAI did not answer within {timeout:.0f} s", retryable=True,
                                     maybe_billed=True) from e
                last = ModelError("cannot reach OpenAI", retryable=True)   # failed while connecting: not sent, safe
            except (ConnectionError, OSError, http.client.HTTPException) as e:
                # dropped, cut off or garbled after the request may have been sent: not retried here (it could be
                # billed twice), and counted as possibly billed
                if watch.expired:
                    raise ModelError(f"OpenAI did not answer within {timeout:.0f} s", retryable=True,
                                     maybe_billed=True) from e
                raise ModelError("the connection to OpenAI was lost or its answer was cut off", retryable=True,
                                 maybe_billed=True) from e
            finally:
                watch.cancel()
            if attempt < self.attempts - 1:
                wait = max(0.0, min(MAX_RETRY_WAIT, wait if wait is not None else random.uniform(1.0, 4.0)))
                if self._remaining(deadline) - wait < MIN_ATTEMPT_SECONDS:
                    raise last
                time.sleep(wait)
        raise last
