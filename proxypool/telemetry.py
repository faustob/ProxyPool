"""
OpenTelemetry bootstrap for ProxyPool.

ProxyPool runs its processors (server/getter/tester) either as independent
OS processes (spawned directly by supervisord via `run.py --processor X`) or,
when started via `Scheduler().run()`, as `multiprocessing.Process` children
forked from a single parent. The OTel SDK's BatchSpanProcessor and
PeriodicExportingMetricReader spin up background export threads, and
upstream is explicit that those threads are NOT fork-safe (the child
inherits a lock held by the parent and can deadlock).

`init_telemetry()` must therefore only ever be called from inside a
processor's own run_* function (see proxypool/scheduler.py) -- i.e. AFTER
any fork has already happened -- never at module import time. Instruments
obtained via `metrics.get_meter()` below are safe to create at import time:
they are proxy objects that transparently rebind once a real MeterProvider
is registered.
"""
import os
import time
import functools

from loguru import logger
from opentelemetry import trace, metrics
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

_initialized = False

SERVICE_NAME = os.environ.get('OTEL_SERVICE_NAME', 'proxypool')

# module-level proxy objects: safe to obtain before the real SDK is
# registered, they transparently rebind once set_meter_provider /
# set_tracer_provider is called later (post-fork) from init_telemetry().
meter = metrics.get_meter(__name__)
tracer = trace.get_tracer(__name__)

# http.server.request.duration -- backs the HTTP availability, P95/P99
# latency, 5xx error-rate and request-throughput SLIs. Recorded for every
# request by proxypool.processors.server's after_request hook.
http_server_request_duration = meter.create_histogram(
    name='http.server.request.duration',
    unit='s',
    description='Duration of inbound HTTP requests handled by the ProxyPool API',
)

# db.client.operation.duration -- backs the Database Query Success Rate SLI.
# recorded by proxypool.storages.redis via the measure_db_operation decorator.
db_operation_duration = meter.create_histogram(
    name='db.client.operation.duration',
    unit='s',
    description='Duration of Redis operations performed by ProxyPool storages',
)


def init_telemetry():
    """
    build the OTel SDK TracerProvider/MeterProvider with an OTLP exporter and
    register them as global, exactly once per process. Endpoint/service name
    come from the standard OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_SERVICE_NAME
    env vars (no hardcoded endpoint).

    Must be called from inside a processor's run_* function (post-fork),
    never at module import time -- see module docstring.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    resource = Resource.create({'service.name': SERVICE_NAME})

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    # set_tracer_provider logs and keeps the existing provider instead of
    # raising if one is already registered (e.g. by an attached agent) --
    # safe to call unconditionally.
    trace.set_tracer_provider(tracer_provider)

    metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    logger.info(f'opentelemetry initialized for service {SERVICE_NAME}')


def measure_db_operation(operation_name):
    """
    decorator recording db.client.operation.duration around a RedisClient
    method call. Classifies the raised exception's class (never swallowing
    or replacing it -- the same exception always propagates) so query
    outcomes can be aggregated by error.type (transient vs terminal).
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            start = time.monotonic()
            error_type = None
            try:
                return func(self, *args, **kwargs)
            except Exception as exc:
                error_type = type(exc).__name__
                raise
            finally:
                attributes = {
                    'db.system.name': 'redis',
                    'db.operation.name': operation_name,
                }
                if error_type:
                    attributes['error.type'] = error_type
                db_operation_duration.record(time.monotonic() - start, attributes)
        return wrapper
    return decorator
