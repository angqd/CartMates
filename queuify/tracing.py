"""OpenTelemetry tracing, written by hand (no auto-instrumentation: Telegram URLs hold the bot token).
With no endpoint configured nothing is installed and every span is a no-op."""
import functools
import json
import logging
from collections.abc import Callable
from contextlib import contextmanager

from openinference.semconv.trace import OpenInferenceSpanKindValues as Kind, SpanAttributes as A
from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

from queuify import observe
from queuify.config import Config

log = logging.getLogger(__name__)

CHAIN, AGENT, LLM, TOOL = (Kind.CHAIN.value, Kind.AGENT.value, Kind.LLM.value, Kind.TOOL.value)

_content = True
_max_chars = 8000
_provider = None       # what setup() installed; shutdown() flushes it
_override = None       # a test provider, used instead of the global one


def configure(content: bool = True, max_chars: int = 8000) -> None:
    global _content, _max_chars
    _content, _max_chars = content, max_chars


def use_provider(provider) -> None:
    """Tests: send spans to this provider, whatever the global one is."""
    global _override
    _override = provider


def trace_url(endpoint: str) -> str:
    base = endpoint.rstrip("/")
    return base if base.endswith("/v1/traces") else base + "/v1/traces"


def setup(config: Config) -> bool:
    """Install the exporter when an endpoint is set. Returns whether tracing is on."""
    global _provider
    configure(config.trace_content, config.trace_max_chars)
    if not config.otel_endpoint:
        return False
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create(
        {"service.name": "queuify", "openinference.project.name": "queuify"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=trace_url(config.otel_endpoint))))
    trace.set_tracer_provider(provider)
    _provider = provider
    log.info("tracing on: exporting to %s", trace_url(config.otel_endpoint))
    return True


def shutdown() -> None:
    if _provider:
        try:
            _provider.shutdown()  # flushes what is queued
        except Exception as e:
            log.warning("could not flush traces: %s", type(e).__name__)


def _tracer() -> trace.Tracer:
    return (_override or trace.get_tracer_provider()).get_tracer("queuify")


# ---- attributes ----

def cap(text: str) -> str:
    if len(text) <= _max_chars:
        return text
    return text[:_max_chars] + f"…[truncated {len(text) - _max_chars} chars]"


def dump(obj) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


def content_on() -> bool:
    return _content


def set_text(span: Span, key: str, text: str | None) -> None:
    """Text content: only with content capture on, capped, and never a login paste."""
    if not _content or text is None or observe.looks_like_paste(text):
        return
    span.set_attribute(key, cap(text))


def set_json(span: Span, key: str, obj) -> None:
    if _content and obj is not None:
        set_text(span, key, dump(obj))


def event(name: str, **attrs) -> None:
    """A point in time on the current span (an order state change, a payment poll)."""
    trace.get_current_span().add_event(name, attrs)


def fail(span: Span, exc: BaseException, redact: bool = False) -> None:
    """Mark the span failed. `redact` keeps only the exception type, not its message."""
    if redact:
        span.add_event("exception", {"exception.type": type(exc).__name__})
    else:
        span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, type(exc).__name__))


# ---- spans ----

@contextmanager
def span(name: str, kind: str | None = None, attributes: dict | None = None):
    attrs = dict(attributes or {})
    if kind:
        attrs[A.OPENINFERENCE_SPAN_KIND] = kind
    with _tracer().start_as_current_span(name, attributes=attrs) as sp:
        yield sp


def traced(name: str, kind: str | None = CHAIN, attrs: Callable[..., dict] | None = None):
    """Wrap an async function in a span. `attrs` gets the same arguments and returns attributes."""
    def wrap(fn):
        @functools.wraps(fn)
        async def inner(*args, **kwargs):
            with span(name, kind, attrs(*args, **kwargs) if attrs else None):
                return await fn(*args, **kwargs)
        return inner
    return wrap


def event_span(event_obj):
    """The root span of one lane event."""
    attrs: dict = {"session.id": str(event_obj.chat_id), "queuify.chat_id": event_obj.chat_id}
    user = getattr(event_obj, "user_id", None)
    if user is not None:
        attrs["user.id"] = str(user)
    command = getattr(event_obj, "command", None)
    if command:
        attrs["queuify.command"] = command
    data = getattr(event_obj, "data", None)
    if data is not None:
        attrs["queuify.callback_data"] = data
    return span(f"event:{type(event_obj).__name__}", CHAIN, attrs)
