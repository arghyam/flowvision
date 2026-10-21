"""
The Claude provider (METER_PROVIDER=claude). Anthropic's API is replaced by a scripted fake at the HTTP layer (an
httpx2 mock transport under the real SDK), so these tests check the requests the SDK sends and how its responses and
errors are handled, with no network and no cost.
"""
import base64
import io
import json
import logging
import os
import time
import uuid
from dataclasses import replace

import anthropic
import httpx2
import pytest
from fastapi.testclient import TestClient

from app_factory import create_app
from conf.config import load_settings
from conftest import answer
from service.vision import ACCEPTED, MeterReader, ModelError, ReaderSettings
from service.vision import claude_client
from service.vision.claude_client import (CONNECT_TIMEOUT, MAX_IMAGE_BASE64, ClaudeVisionClient, claude_schema,
                                          fit_image)
from service.vision.images import ImageError
from service.vision.prompt import PROMPT, SCHEMA

BASE = "/flowvision/v1/elm"


def claude_settings(**overrides):
    return replace(ReaderSettings(openai_api_key="", provider="claude", model="claude-sonnet-5-5",
                                  anthropic_api_key="sk-ant-test-not-real", anthropic_base_url="https://claude.test"),
                   **overrides)


def message(text=None, stop="end_turn", tokens=(5000, 1200), model="claude-sonnet-5-5", stop_details=None):
    """A Messages API response body: a thinking block (empty, as by default), then the answer's text block."""
    content = [{"type": "thinking", "thinking": "", "signature": "sig"}]
    if text is not None:
        content.append({"type": "text", "text": text})
    return {"id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": content,
            "stop_reason": stop, "stop_sequence": None, "stop_details": stop_details,
            "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1],
                      "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}


def error(status, kind, headers=None):
    return status, {"type": "error", "error": {"type": kind, "message": f"simulated {kind}"}}, headers or {}


class FakeAnthropic:
    """Answers each request with the next scripted item (the last one repeats): a response body (HTTP 200), a
    (status, body, headers) tuple, a ready httpx2.Response, or an httpx2 exception class, raised as the network
    failure it names."""

    def __init__(self, *script):
        self.script, self.requests = list(script), []

    def __call__(self, request):
        self.requests.append(request)
        item = self.script[min(len(self.requests), len(self.script)) - 1]
        if isinstance(item, type) and issubclass(item, Exception):
            raise item("simulated network failure", request=request)
        if isinstance(item, httpx2.Response):
            return item
        status, body, headers = item if isinstance(item, tuple) else (200, item, {})
        return httpx2.Response(status, json=body, headers=headers)

    def body(self, i=0):
        return json.loads(self.requests[i].content)


def make(fake, **overrides):
    http = anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(fake))
    return ClaudeVisionClient(claude_settings(**overrides), http_client=http)


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    monkeypatch.setattr(claude_client.random, "uniform", lambda a, b: 0.0)   # retries without the 1-4 s pause


def test_request_uses_the_same_prompt_and_answer_schema(jpeg_bytes):
    fake = FakeAnthropic(message(json.dumps(answer())))
    out = make(fake).read(jpeg_bytes, "image/jpeg")
    assert out["answer"] == answer() and out["model"] == "claude-sonnet-5-5"
    assert out["usage"] == {"prompt_tokens": 5000, "completion_tokens": 1200}
    req = fake.requests[0]
    assert req.url.path == "/v1/messages" and req.headers["x-api-key"] == "sk-ant-test-not-real"
    assert "anthropic-beta" not in req.headers and "beta" not in req.url.query.decode()     # no beta features
    body = fake.body()
    assert body["model"] == "claude-sonnet-5-5" and body["max_tokens"] == 16000
    assert body["thinking"] == {"type": "adaptive"} and "fallbacks" not in body              # this model only
    assert body["output_config"] == {"effort": "high", "format": {"type": "json_schema", "schema": claude_schema(SCHEMA)}}
    image, prompt = body["messages"][0]["content"]
    assert image["source"] == {"type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpeg_bytes).decode()}
    assert prompt == {"type": "text", "text": PROMPT}


def test_workspace_header_only_when_a_workspace_is_set(jpeg_bytes):
    fake = FakeAnthropic(message(json.dumps(answer())))
    make(fake).read(jpeg_bytes)
    make(fake, anthropic_workspace_id="wrkspc_test").read(jpeg_bytes)
    assert "anthropic-workspace-id" not in fake.requests[0].headers
    assert fake.requests[1].headers["anthropic-workspace-id"] == "wrkspc_test"


def test_effort_can_be_left_to_the_model_default(jpeg_bytes):
    fake = FakeAnthropic(message(json.dumps(answer())))
    make(fake, reasoning_effort=None).read(jpeg_bytes)
    assert "effort" not in fake.body()["output_config"]            # the model's default effort


@pytest.mark.parametrize("response", [
    httpx2.Response(200, content=b'{"id": "msg_test", "type": "message", "content": [',
                    headers={"content-type": "application/json"}),        # cut off
    httpx2.Response(200, content=b"<html>proxy error</html>", headers={"content-type": "text/html"}),
    httpx2.Response(200, json=[1, 2]),
])
def test_unreadable_answer_is_a_model_error_and_not_resent(jpeg_bytes, response):
    fake = FakeAnthropic(response)
    with pytest.raises(ModelError, match="unreadable response") as e:
        make(fake).read(jpeg_bytes)
    assert e.value.retryable and e.value.maybe_billed and len(fake.requests) == 1


def test_timeouts_fail_fast_on_connect_and_stay_within_the_deadline(jpeg_bytes):
    fake = FakeAnthropic(message(json.dumps(answer())))
    make(fake).read(jpeg_bytes)
    limits = fake.requests[0].extensions["timeout"]
    assert limits["connect"] == CONNECT_TIMEOUT and limits["read"] == 90.0
    make(fake).read(jpeg_bytes, deadline=time.monotonic() + 30)
    assert max(fake.requests[1].extensions["timeout"].values()) <= 30


def test_debug_logging_never_writes_the_photo(jpeg_bytes, caplog):
    caplog.set_level(logging.DEBUG)
    make(FakeAnthropic(message(json.dumps(answer())))).read(jpeg_bytes)
    photo = base64.b64encode(jpeg_bytes).decode()
    assert photo[:200] not in caplog.text


def test_schema_for_claude_writes_type_lists_as_anyof():
    schema = claude_schema(SCHEMA)

    def walk(node):
        if isinstance(node, dict):
            assert not isinstance(node.get("type"), list)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(schema)
    assert schema["properties"]["unit"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    assert schema["properties"]["status"] == SCHEMA["properties"]["status"]      # enums unchanged
    assert schema["required"] == SCHEMA["required"] and schema["additionalProperties"] is False
    assert SCHEMA["properties"]["unit"]["type"] == ["string", "null"]           # OpenAI's schema is not modified


def test_reader_accepts_and_prices_a_claude_reading(jpeg_bytes):
    fake = FakeAnthropic(message(json.dumps(answer("0019712.7"))))
    settings = claude_settings()
    result = MeterReader(settings, client=make(fake)).read(jpeg_bytes)
    assert result.status == ACCEPTED and result.reading == "0019712.7" and result.model == "claude-sonnet-5-5"
    assert result.cost_usd == round((5000 * 2.0 + 1200 * 10.0) / 1e6, 5)        # $0.022 at $2 / $10 per MTok


@pytest.mark.parametrize("script, calls", [
    ((error(429, "rate_limit_error", {"retry-after": "0"}), message("{}")), 2),
    ((error(529, "overloaded_error", {"retry-after": "0"}), message("{}")), 2),
    ((error(500, "api_error"), message("{}")), 2),
    ((httpx2.ConnectError, message("{}")), 2),          # never connected: nothing was sent
    ((httpx2.ConnectTimeout, message("{}")), 2),
])
def test_unbilled_errors_are_retried_once(jpeg_bytes, script, calls):
    fake = FakeAnthropic(*script)
    assert make(fake).read(jpeg_bytes)["answer"] == {}
    assert len(fake.requests) == calls


def test_a_second_unbilled_error_fails_retryable_and_costs_nothing(jpeg_bytes):
    fake = FakeAnthropic(error(529, "overloaded_error", {"retry-after": "0"}))
    with pytest.raises(ModelError) as e:
        make(fake).read(jpeg_bytes)
    assert len(fake.requests) == 2 and e.value.retryable and not e.value.maybe_billed and not e.value.usage


@pytest.mark.parametrize("failure", [httpx2.ReadTimeout, httpx2.RemoteProtocolError])
def test_a_sent_request_that_failed_is_never_sent_again(jpeg_bytes, failure):
    fake = FakeAnthropic(failure, message("{}"))
    with pytest.raises(ModelError) as e:
        make(fake).read(jpeg_bytes)
    assert len(fake.requests) == 1                      # it may still be billed: one call only
    assert e.value.retryable and e.value.maybe_billed


@pytest.mark.parametrize("status, kind, alert", [
    (401, "authentication_error", True), (403, "permission_error", True), (402, "billing_error", True),
    (404, "not_found_error", True), (400, "invalid_request_error", False), (413, "request_too_large", False),
])
def test_client_errors_are_not_retried(jpeg_bytes, status, kind, alert):
    fake = FakeAnthropic(error(status, kind))
    with pytest.raises(ModelError) as e:
        make(fake).read(jpeg_bytes)
    assert len(fake.requests) == 1 and e.value.alert is alert and not e.value.retryable


@pytest.mark.parametrize("body, text", [
    (message(None, stop="refusal", stop_details={"type": "refusal", "category": "general_harms", "explanation": "x"}),
     "declined"),
    (message('{"status": "RE', stop="max_tokens"), "cut off"),
    (message("not json"), "invalid JSON"),
    (message("[1, 2]"), "not an object"),
    (message(None), "no content"),
])
def test_unusable_answers_fail_and_keep_their_cost(jpeg_bytes, body, text):
    fake = FakeAnthropic(body)
    with pytest.raises(ModelError) as e:
        MeterReader(claude_settings(), client=make(fake)).read(jpeg_bytes)
    assert text in str(e.value) and len(fake.requests) == 1
    assert e.value.cost_usd == 0.022                    # answered, so billed: counted against the daily budget


def test_no_call_without_time_left_before_the_deadline(jpeg_bytes):
    fake = FakeAnthropic(message("{}"))
    with pytest.raises(ModelError) as e:
        make(fake).read(jpeg_bytes, deadline=time.monotonic() + 5)
    assert not fake.requests and e.value.retryable


def test_a_photo_over_anthropics_limit_is_sent_as_jpeg(jpeg_bytes):
    from PIL import Image
    assert fit_image(jpeg_bytes, "image/jpeg") == (jpeg_bytes, "image/jpeg")
    noise = Image.frombytes("RGB", (1400, 1000), os.urandom(1400 * 1000 * 3))
    buf = io.BytesIO()
    noise.save(buf, format="PNG")
    png = buf.getvalue()
    assert len(png) * 4 / 3 > MAX_IMAGE_BASE64
    data, mime = fit_image(png, "image/png")
    assert mime == "image/jpeg" and len(data) * 4 / 3 <= MAX_IMAGE_BASE64
    with pytest.raises(ImageError):
        fit_image(b"\x89PNG\r\n\x1a\n" + b"0" * MAX_IMAGE_BASE64, "image/png")     # not a decodable image


@pytest.fixture
def no_keys(monkeypatch):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_WORKSPACE_ID", "OPENAI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def test_load_settings_for_claude(config, monkeypatch, no_keys):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    assert load_settings(config).provider == "openai"            # the default; no Anthropic key needed
    monkeypatch.setenv("METER_PROVIDER", "claude")
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        load_settings(config)
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    s = load_settings(config)                                    # no OpenAI key needed
    assert (s.provider, s.model, s.reasoning_effort, s.anthropic_api_key) == ("claude", "claude-sonnet-5-5", "high", "a")
    assert (s.price_input_per_m, s.price_output_per_m, s.request_timeout, s.max_attempts) == (2.0, 10.0, 90.0, 2)
    assert s.anthropic_base_url == "https://api.anthropic.com"
    assert s.anthropic_workspace_id == ""
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_x")
    assert load_settings(config).anthropic_workspace_id == "wrkspc_x"
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID")
    monkeypatch.setenv("METER_REASONING_EFFORT", "medium")
    assert load_settings(config).reasoning_effort == "medium"
    for name, value in (("METER_REASONING_EFFORT", "extreme"), ("METER_MODEL", "gpt-6-sol"), ("METER_PROVIDER", "gemini")):
        monkeypatch.setenv(name, value)
        with pytest.raises(RuntimeError):
            load_settings(config)
        monkeypatch.delenv(name)
    monkeypatch.setenv("METER_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("METER_MODEL", "claude-sonnet-5-5")        # a Claude model for the OpenAI provider
    with pytest.raises(RuntimeError, match="not an openai model"):
        load_settings(config)


def test_service_reads_with_claude_when_configured(config, db_url, jpeg_bytes, monkeypatch, no_keys):
    monkeypatch.setenv("METER_PROVIDER", "claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real")
    app = create_app(config=config, db_url=db_url, service_api_key="")
    service = app.state.service
    assert isinstance(service.reader.client, ClaudeVisionClient)
    fake = FakeAnthropic(message(json.dumps(answer("0019712.7"))))
    service.reader.client = make(fake)                            # the same client, answering from the fake API
    c = TestClient(app)
    assert c.get("/").json()["model"] == "claude-sonnet-5-5"
    rid = str(uuid.uuid4())
    body = c.post(f"{BASE}/extract-reading", json={"id": rid, "imageBase64": base64.b64encode(jpeg_bytes).decode(),
                                                   "consumerId": "C-claude"}).json()
    assert body["result"]["status"] == "SUCCESS" and body["result"]["data"]["model"] == "claude-sonnet-5-5"
    stored = c.get(f"{BASE}/readings/{rid}").json()["result"]
    assert stored["model"] == "claude-sonnet-5-5" and stored["cost_usd"] == 0.022
