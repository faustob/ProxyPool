from proxypool.scheduler import Scheduler
import argparse
import time
from opentelemetry import trace
from proxypool.telemetry import (
    init_telemetry,
    tracer,
    flow_entries_counter,
    flow_outcomes_counter,
    flow_duration_histogram,
    flow_entry_to_terminal_histogram,
)

init_telemetry()


parser = argparse.ArgumentParser(description='ProxyPool')
parser.add_argument('--processor', type=str, help='processor to run')
args = parser.parse_args()

if __name__ == '__main__':
    flow_entries_counter.add(1, {"flow": "proxy_lifecycle"})
    _flow_start_time = time.time()
    _flow_outcome = "success"
    with tracer.start_as_current_span("proxy_lifecycle.flow") as _flow_span:
        _flow_span.set_attribute("flow.name", "proxy_lifecycle")
        try:
            # if processor set, just run it
            if args.processor:
                getattr(Scheduler(), f'run_{args.processor}')()
            else:
                Scheduler().run()
        except Exception as exc:
            _flow_outcome = "failed"
            _flow_span.set_status(trace.StatusCode.ERROR, str(exc))
            raise
        finally:
            _flow_duration = time.time() - _flow_start_time
            flow_duration_histogram.record(_flow_duration, {"flow": "proxy_lifecycle", "outcome": _flow_outcome})
            flow_entry_to_terminal_histogram.record(
                _flow_duration, {"flow": "proxy_lifecycle", "terminal_state": _flow_outcome}
            )
            flow_outcomes_counter.add(1, {"flow": "proxy_lifecycle", "outcome": _flow_outcome})
