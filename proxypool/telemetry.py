"""
OpenTelemetry bootstrap for ProxyPool.

Fork-safety note: proxypool runs the API server, the getter and the tester as
separate OS processes spawned via multiprocessing.Process (see
proxypool/scheduler.py). BatchSpanProcessor runs a background export thread
that upstream documents as NOT fork-safe (the child inherits a lock held by
the parent and can deadlock), so the SDK MUST be built and registered inside
each child process AFTER the fork, never at module import time here or in the
parent. init_telemetry() is therefore only ever called from inside
Scheduler.run_tester / run_getter / run_server, which execute in the child.

Registration is guarded so it tolerates re-invocation within a single process
and tolerates a provider that may already be registered by an external OTel
agent: opentelemetry-python's set_tracer_provider/set_meter_provider log and
keep the existing provider on re-registration rather than raising, but we
still wrap in try/except defensively in case an agent's shim behaves
differently.
"""
from opentelemetry import trace, metrics
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from loguru import logger

_initialized = False


def init_telemetry(service_name: str = 'proxypool') -> None:
    """
    Build and register the global OpenTelemetry TracerProvider/MeterProvider
    for the CURRENT process. Endpoint configuration comes from the standard
    OTEL_EXPORTER_OTLP_ENDPOINT (and friends) environment variables handled by
    the exporters themselves -- never hardcoded here.
    """
    global _initialized
    if _initialized:
        return

    resource = Resource.create({'service.name': service_name})

    try:
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tracer_provider)
    except Exception as e:
        # an SDK/agent may already be registered in this process; tolerate it
        # and keep using whatever global provider is already in place
        logger.warning(f'tracer provider registration skipped: {e}')

    try:
        reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(meter_provider)
    except Exception as e:
        logger.warning(f'meter provider registration skipped: {e}')

    _initialized = True
