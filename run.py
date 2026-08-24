from proxypool.scheduler import Scheduler
import argparse
import time

from opentelemetry import trace

from proxypool.utils.otel import get_tracer, get_meter, shutdown_telemetry
import atexit

atexit.register(shutdown_telemetry)


parser = argparse.ArgumentParser(description='ProxyPool')
parser.add_argument('--processor', type=str, help='processor to run')
args = parser.parse_args()

_tracer = get_tracer(__name__)
_meter = get_meter(__name__)

flow_entries_counter = _meter.create_counter(
    "flow.entries",
    unit="1",
    description="Count of proxy lifecycle flow entry invocations",
)
flow_outcomes_counter = _meter.create_counter(
    "flow.outcomes",
    unit="1",
    description="Count of proxy lifecycle flow terminal outcomes",
)
flow_duration_histogram = _meter.create_histogram(
    "flow.duration",
    unit="s",
    description="Duration of the proxy lifecycle end-to-end flow",
)
flow_entry_to_terminal_histogram = _meter.create_histogram(
    "flow.entry_to_terminal.duration",
    unit="s",
    description="Wall-clock time from flow entry to terminal state, in seconds",
)

if __name__ == '__main__':
    flow_name = args.processor or 'full_pipeline'
    with _tracer.start_as_current_span("proxy_lifecycle.flow") as _flow_span:
        _flow_span.set_attribute("flow.name", flow_name)
        flow_entries_counter.add(1, {"flow": flow_name})
        _flow_start = time.time()
        _flow_outcome = "success"
        try:
            # if processor set, just run it
            if args.processor:
                getattr(Scheduler(), f'run_{args.processor}')()
            else:
                Scheduler().run()
        except Exception as _flow_exc:
            _flow_outcome = "failure"
            _flow_span.set_status(trace.StatusCode.ERROR, str(_flow_exc))
            _flow_span.set_attribute("error.type", type(_flow_exc).__name__)
            raise
        finally:
            _flow_duration = time.time() - _flow_start
            flow_duration_histogram.record(_flow_duration, {"flow": flow_name, "outcome": _flow_outcome})
            flow_entry_to_terminal_histogram.record(
                _flow_duration, {"flow": flow_name, "terminal_state": _flow_outcome}
            )
            flow_outcomes_counter.add(1, {"flow": flow_name, "outcome": _flow_outcome})
