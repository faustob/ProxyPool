"""
OpenTelemetry bootstrap for ProxyPool.

ProxyPool's scheduler forks one child process per component (tester, getter,
server) via multiprocessing.Process. BatchSpanProcessor is NOT fork-safe (it
owns a background export thread), so the SDK must be built and registered
INSIDE each child, after the fork - never at module import time in the
parent. `init_telemetry()` is called from the top of each of
Scheduler.run_tester / run_getter / run_server in proxypool/scheduler.py,
which is exactly where each child process begins its work.

trace.set_tracer_provider / metrics.set_meter_provider never raise on
re-registration (they log and keep the existing provider), and this module
also short-circuits via `_initialized` so calling init_telemetry() more than
once in the same process is a harmless no-op.
"""
import os

from opentelemetry import trace, metrics
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

_initialized: bool = False


def init_telemetry(service_name: str = 'proxypool') -> None:
    """
    Build and register the global TracerProvider/MeterProvider exactly once
    per process, exporting via OTLP/HTTP. The collector endpoint comes from
    the standard OTEL_EXPORTER_OTLP_ENDPOINT env var (never hardcoded here).
    MUST be called after the process has forked (see module docstring).
    """
    global _initialized
    if _initialized:
        return

    resource = Resource.create({
        'service.name': os.environ.get('OTEL_SERVICE_NAME', service_name),
    })

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    _initialized = True
