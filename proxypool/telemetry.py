"""
OpenTelemetry bootstrap for ProxyPool.

Builds and registers the global TracerProvider and MeterProvider exactly once
at process startup. The OTLP endpoint is taken from the standard
OTEL_EXPORTER_OTLP_ENDPOINT environment variable (never hardcoded).

This module is imported by the application entrypoint (run.py) before any
instrumented code runs. ProxyPool's run.py is a single-process entrypoint
(no pre-fork server), so module-level instrument creation plus an explicit
init_telemetry() call from the entrypoint is safe here.
"""
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

_initialized = False


def init_telemetry(service_name: str = "proxypool"):
    """Build and register the global OTel providers exactly once.

    Safe to call multiple times (a no-op after the first successful call).
    Also defensive against an already-registered provider (e.g. an agent
    attached at runtime): set_tracer_provider/set_meter_provider log and
    keep the existing provider instead of raising, and any exporter
    construction failure is swallowed so the app can still start without
    telemetry rather than crash.
    """
    global _initialized
    if _initialized:
        return

    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", service_name)})

    try:
        span_exporter = OTLPSpanExporter()
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)
    except Exception:
        pass

    try:
        metric_exporter = OTLPMetricExporter()
        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)
    except Exception:
        pass

    _initialized = True


# Module-level tracer/meter: these are proxy objects until init_telemetry()
# registers the real providers, then they re-bind automatically.
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

flow_entries_counter = meter.create_counter(
    "flow.entries",
    unit="1",
    description="Number of times the proxy lifecycle flow's entry point was invoked",
)

flow_outcomes_counter = meter.create_counter(
    "flow.outcomes",
    unit="1",
    description="Terminal outcome of the proxy lifecycle flow (success/failed)",
)

flow_duration_histogram = meter.create_histogram(
    "flow.duration",
    unit="s",
    description="End-to-end duration of the proxy lifecycle flow",
)

flow_entry_to_terminal_histogram = meter.create_histogram(
    "flow.entry_to_terminal.duration",
    unit="s",
    description="Wall-clock time between the flow's entry event and its terminal state transition",
)
