"""OpenTelemetry bootstrap and shared instruments for the ProxyPool
application entrypoint (run.py).

This module builds and registers the global TracerProvider and
MeterProvider exactly once, using an OTLP exporter whose endpoint is read
from the OTEL_EXPORTER_OTLP_ENDPOINT environment variable (never
hardcoded). ProxyPool (run.py) is a single-process, non-forking
application, so module-level instrument creation plus an explicit
init_telemetry() call from the entrypoint before any work runs is safe.
"""
import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
    OTLPMetricExporter,
)
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
    OTLPSpanExporter,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

logger = logging.getLogger(__name__)

_initialized = False


def init_telemetry():
    """Build and register the global TracerProvider/MeterProvider once.

    Safe to call multiple times (idempotent) and safe to run when an
    OTel agent/SDK has already registered a global provider out-of-band:
    opentelemetry-python's set_tracer_provider/set_meter_provider log and
    keep the existing provider instead of raising on re-registration, so
    no extra guarding is required around those calls.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    service_name = os.environ.get('OTEL_SERVICE_NAME', 'proxypool')
    otlp_endpoint = os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT')

    resource = Resource.create({'service.name': service_name})

    span_exporter_kwargs = {}
    metric_exporter_kwargs = {}
    if otlp_endpoint:
        span_exporter_kwargs['endpoint'] = otlp_endpoint
        metric_exporter_kwargs['endpoint'] = otlp_endpoint

    try:
        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(**span_exporter_kwargs))
        )
        trace.set_tracer_provider(tracer_provider)

        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(**metric_exporter_kwargs)
        )
        meter_provider = MeterProvider(
            resource=resource, metric_readers=[metric_reader]
        )
        metrics.set_meter_provider(meter_provider)
    except Exception:
        logger.warning(
            'Failed to initialize OpenTelemetry SDK; falling back to any '
            'already-registered global provider',
            exc_info=True,
        )


# Module-level tracer/meter: these are proxy objects that re-bind to the
# real provider once set_tracer_provider/set_meter_provider run above, so
# creating them here (before init_telemetry() executes) is correct.
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

flow_entries_counter = meter.create_counter(
    'flow.entries',
    unit='1',
    description='Number of times the proxy pool flow entry point was invoked',
)

flow_outcomes_counter = meter.create_counter(
    'flow.outcomes',
    unit='1',
    description='Terminal outcome of the proxy pool flow (success/failure)',
)

flow_duration_histogram = meter.create_histogram(
    'flow.duration',
    unit='s',
    description='End-to-end duration of the proxy pool flow',
)

flow_entry_to_terminal_histogram = meter.create_histogram(
    'flow.entry_to_terminal.duration',
    unit='s',
    description='Wall-clock time between flow entry and its terminal state',
)
