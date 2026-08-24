"""OpenTelemetry bootstrap for ProxyPool.

Importing this module (for its side effects) configures and registers the
global OpenTelemetry TracerProvider and MeterProvider exactly once, wires up
an OTLP exporter (endpoint taken from OTEL_EXPORTER_OTLP_ENDPOINT), and
enables Flask auto-instrumentation for every Flask app created afterwards.

This module MUST be imported before any Flask() app is instantiated, which is
why run.py imports it before importing proxypool.scheduler.
"""
import os
import time

from flask import got_request_exception

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# Opt in to the stable HTTP semantic conventions so Flask auto-instrumentation
# emits `http.server.request.duration` (seconds) with the current attribute
# keys instead of the legacy `http.server.duration` metric.
os.environ.setdefault("OTEL_SEMCONV_STABILITY_OPT_IN", "http")

_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "proxypool")
_resource = Resource.create({SERVICE_NAME: _SERVICE_NAME})

# Build the providers once. set_tracer_provider/set_meter_provider log and
# keep any already-registered provider (e.g. from an attached OTel agent)
# instead of raising, so this is safe to call unconditionally at startup.
_tracer_provider = TracerProvider(resource=_resource)
_tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(_tracer_provider)

_metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
_meter_provider = MeterProvider(resource=_resource, metric_readers=[_metric_reader])
metrics.set_meter_provider(_meter_provider)

tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

# Request outcome counter (proxy-pool-http-availability / -http-request-rate):
# one counter, dimensioned by route, outcome class and tenant, so
# availability and per-tenant throughput are both computable without
# scanning traces, and the same completed-request event isn't double-counted
# across two separate instruments.
request_outcome_counter = meter.create_counter(
    name="http.server.request.outcome",
    unit="1",
    description="Count of completed HTTP requests by route, outcome class and tenant",
)

# Budget used to flag slow requests (proxy-pool-http-latency-p99) with a span
# event instead of scanning histograms after the fact.
_SLOW_REQUEST_BUDGET_SECONDS = float(
    os.environ.get("PROXYPOOL_SLOW_REQUEST_BUDGET_SECONDS", "1.0")
)


def _request_hook(span, environ):
    """Tag the server span with a low-cardinality tenant identifier."""
    if span is not None and span.is_recording():
        tenant = environ.get("HTTP_X_API_KEY") or environ.get("HTTP_X_TENANT") or "unknown"
        span.set_attribute("tenant.id", tenant)


def _response_hook(span, status, response_headers):
    """Record the outcome counter and a slow-request span event."""
    if span is None:
        return

    try:
        status_code = int(str(status).split(" ")[0])
    except (ValueError, IndexError):
        status_code = 0

    route = "unknown"
    tenant = "unknown"
    duration_seconds = None
    if span.is_recording():
        attributes = getattr(span, "attributes", None) or {}
        route = attributes.get("http.route", "unknown")
        tenant = attributes.get("tenant.id", "unknown")
        start_time = getattr(span, "start_time", None)
        if start_time:
            duration_seconds = (time.time_ns() - start_time) / 1e9

    outcome = "success" if 0 < status_code < 500 else "error"
    request_outcome_counter.add(
        1,
        {"http.route": route, "outcome": outcome, "tenant.id": tenant},
    )

    if span.is_recording() and duration_seconds is not None and duration_seconds > _SLOW_REQUEST_BUDGET_SECONDS:
        span.add_event(
            "slow_request",
            {"duration_seconds": duration_seconds, "http.route": route},
        )


def _handle_request_exception(sender, exception, **extra):
    """Record the exception class on the current server span (proxy-pool-http-error-rate)."""
    span = trace.get_current_span()
    if span is not None and span.is_recording():
        span.set_attribute("error.type", type(exception).__name__)
        span.set_status(trace.StatusCode.ERROR, str(exception))


# Connects to every Flask app's `got_request_exception` signal (sender=None
# means "any sender"), so this works without needing a reference to the
# app instance created later inside proxypool.processors.server.
got_request_exception.connect(_handle_request_exception, weak=False)

# Instrument the Flask class itself (no app instance available here yet) so
# every Flask() app created after this module is imported gets the standard
# http.server.request.duration histogram plus our custom hooks.
FlaskInstrumentor().instrument(request_hook=_request_hook, response_hook=_response_hook)
