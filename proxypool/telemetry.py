"""OpenTelemetry bootstrap for ProxyPool.

Builds and registers the global TracerProvider/MeterProvider exactly once at
process startup, with the OTLP endpoint read from the standard
OTEL_EXPORTER_OTLP_ENDPOINT environment variable (never hardcoded). Also
activates the Flask and Redis OTel instrumentors so inbound HTTP requests
(http.server.request.duration, with http.route / http.response.status_code)
and outbound Redis calls are instrumented automatically per OTel semantic
conventions.

ProxyPool's Scheduler starts the API server, the Getter and the Tester as
separate multiprocessing.Process workers, i.e. this process forks after
startup. BatchSpanProcessor is not fork-safe (it owns a background export
thread), so providers are rebuilt in each child via os.register_at_fork.
"""
import os
import threading

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "proxypool")

_providers_lock = threading.Lock()
_providers_initialized = False
_instrumentors_lock = threading.Lock()
_instrumentors_initialized = False


def _shutdown_inherited_providers():
    """Best-effort shutdown of providers inherited from the parent process.

    After fork the child holds copies of the parent's TracerProvider and
    MeterProvider (with a now-broken, non-forked export thread). Shut them
    down before installing fresh ones so the inherited BatchSpanProcessor /
    PeriodicExportingMetricReader don't leak background resources.
    """
    try:
        current_tracer_provider = trace.get_tracer_provider()
        shutdown = getattr(current_tracer_provider, "shutdown", None)
        if shutdown is not None:
            shutdown()
    except Exception:
        pass
    try:
        current_meter_provider = metrics.get_meter_provider()
        shutdown = getattr(current_meter_provider, "shutdown", None)
        if shutdown is not None:
            shutdown()
    except Exception:
        pass


def _configure_providers():
    """Build fresh TracerProvider/MeterProvider and register them globally.

    Re-invoked after a fork (via os.register_at_fork) so the inherited,
    now-broken BatchSpanProcessor export thread is replaced with a fresh one
    in the child rather than deadlocking.
    """
    resource = Resource.create({"service.name": _SERVICE_NAME})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    # set_tracer_provider logs and keeps the existing provider on
    # re-registration (e.g. if an OTel agent already set one) rather than
    # raising, so this is safe to call whether or not an agent is attached.
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)


def _reinit_after_fork():
    global _providers_initialized, _instrumentors_initialized
    _shutdown_inherited_providers()
    _configure_providers()
    # The child process serves its own requests (API server, Getter, or
    # Tester); providers were just rebuilt above. Ensure the guards reflect
    # that state in the child's own memory (fork copies the parent's already-
    # True guard, which is what we want here since instrumentors are process-
    # wide class patches inherited across fork and do not need re-applying).
    _providers_initialized = True
    _instrumentors_initialized = True


def setup_telemetry():
    """Register the global OTel SDK and activate framework instrumentors.

    Must be called exactly once, as early as possible in process lifetime,
    before the Flask app or the Redis client are constructed.
    """
    global _providers_initialized, _instrumentors_initialized

    with _providers_lock:
        if not _providers_initialized:
            _configure_providers()
            _providers_initialized = True
            try:
                # ProxyPool forks worker processes (server/getter/tester) via
                # multiprocessing.Process; rebuild providers in each child so
                # the BatchSpanProcessor export thread is never inherited
                # across the fork (upstream explicitly warns this deadlocks).
                os.register_at_fork(after_in_child=_reinit_after_fork)
            except (AttributeError, ValueError):
                # fork() not available on this platform, or already registered
                pass

    with _instrumentors_lock:
        if not _instrumentors_initialized:
            try:
                # Bare instrument() patches the Flask class globally; it
                # must run before any Flask app instance is created.
                FlaskInstrumentor().instrument()
            except Exception:
                pass
            try:
                RedisInstrumentor().instrument()
            except Exception:
                pass
            _instrumentors_initialized = True
