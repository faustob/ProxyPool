"""OpenTelemetry bootstrap for ProxyPool.

This module builds and registers the global TracerProvider and
MeterProvider EXACTLY ONCE for the process, exporting via OTLP. The
endpoint is read from the standard OTEL_EXPORTER_OTLP_ENDPOINT
environment variable and is never hardcoded.

The setup below runs as MODULE-LEVEL statements, so importing this module
(see run.py, which does so at the top of the process entrypoint, before
any instrumented code runs) is sufficient to activate telemetry.

This is safe as module-level, import-time initialization because
ProxyPool's run.py is a single-process entrypoint: it does not fork
workers via gunicorn/uWSGI pre-fork hooks or multiprocessing.Process, so
the BatchSpanProcessor's background export thread is created exactly
once, in the only process that will ever use it.

set_tracer_provider/set_meter_provider do not raise if a provider
(e.g. from an attached OTel agent) is already registered -- they log a
warning and keep the existing global provider, so calling them here is
safe whether or not an agent is present.
"""
import os

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_SERVICE_NAME = os.environ.get('OTEL_SERVICE_NAME', 'proxypool')
_OTLP_ENDPOINT = os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT')

_resource = Resource.create({'service.name': _SERVICE_NAME})


def _build_span_exporter():
    if _OTLP_ENDPOINT:
        return OTLPSpanExporter(endpoint=_OTLP_ENDPOINT)
    return OTLPSpanExporter()


def _build_metric_exporter():
    if _OTLP_ENDPOINT:
        return OTLPMetricExporter(endpoint=_OTLP_ENDPOINT)
    return OTLPMetricExporter()


_tracer_provider = TracerProvider(resource=_resource)
_tracer_provider.add_span_processor(BatchSpanProcessor(_build_span_exporter()))
trace.set_tracer_provider(_tracer_provider)

_metric_reader = PeriodicExportingMetricReader(_build_metric_exporter())
_meter_provider = MeterProvider(resource=_resource, metric_readers=[_metric_reader])
metrics.set_meter_provider(_meter_provider)

tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

flow_entries_counter = meter.create_counter(
    'flow.entries',
    unit='1',
    description="Number of times the proxy lifecycle flow's entry point was invoked",
)

flow_outcomes_counter = meter.create_counter(
    'flow.outcomes',
    unit='1',
    description='Terminal outcomes (success/failed) of the proxy lifecycle flow',
)

flow_duration_histogram = meter.create_histogram(
    'flow.duration',
    unit='s',
    description='End-to-end duration of the proxy lifecycle flow',
)

flow_entry_to_terminal_histogram = meter.create_histogram(
    'flow.entry_to_terminal.duration',
    unit='s',
    description="Wall-clock time between the flow's entry event and its terminal state transition",
)

flow_validation_outcomes_counter = meter.create_counter(
    'flow.validation.outcomes',
    unit='1',
    description='Outcomes (pass/fail) of proxy validation checks in the proxy lifecycle flow',
)


import atexit as _atexit


def _shutdown_providers():
    """Flush and shut down the tracer/meter providers on process exit.

    Registered via atexit so buffered spans (BatchSpanProcessor) and
    metrics (PeriodicExportingMetricReader) are exported before this
    short-lived batch process terminates.
    """
    try:
        _tracer_provider.shutdown()
    finally:
        _meter_provider.shutdown()


_atexit.register(_shutdown_providers)
