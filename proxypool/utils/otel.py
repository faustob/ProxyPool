"""OpenTelemetry SDK bootstrap for ProxyPool.

This module builds and registers the global TracerProvider and MeterProvider
exactly once, as MODULE-LEVEL statements, so importing this module (for its
side effect) from the process entrypoint (run.py) is sufficient to activate
telemetry before any instrumented code runs.

The OTLP endpoint is taken from the standard environment variable
OTEL_EXPORTER_OTLP_ENDPOINT (never hardcoded here) — this is the default
behavior of the OTLP exporters when no endpoint is passed explicitly.
"""
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_resource = Resource.create({"service.name": "proxypool"})

_tracer_provider = TracerProvider(resource=_resource)
_tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
# set_tracer_provider logs and keeps the existing provider if one is already
# registered (e.g. by an attached agent) — it never raises, so this is safe
# to call unconditionally.
trace.set_tracer_provider(_tracer_provider)

_metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
_meter_provider = MeterProvider(resource=_resource, metric_readers=[_metric_reader])
# Likewise, set_meter_provider never raises on re-registration.
metrics.set_meter_provider(_meter_provider)


def get_tracer(name: str):
    """Return a tracer bound to the (possibly already-registered) global TracerProvider."""
    return trace.get_tracer(name)


def get_meter(name: str):
    """Return a meter bound to the (possibly already-registered) global MeterProvider."""
    return metrics.get_meter(name)


def shutdown_telemetry():
    """Flush and shut down the tracer and meter providers.

    run.py is a short-lived CLI process: the BatchSpanProcessor and
    PeriodicExportingMetricReader buffer telemetry in-memory, so without an
    explicit shutdown/flush before process exit, any spans and metrics
    recorded right before termination would be dropped.
    """
    try:
        _tracer_provider.shutdown()
    except Exception:
        pass
    try:
        _meter_provider.shutdown()
    except Exception:
        pass
