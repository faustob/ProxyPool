from proxypool.scheduler import Scheduler
import argparse
import sys
import time
import uuid

from opentelemetry import trace

from proxypool.telemetry import (
    init_telemetry,
    tracer,
    flow_entries_counter,
    flow_outcomes_counter,
    flow_duration_histogram,
    flow_entry_to_terminal_histogram,
)


parser = argparse.ArgumentParser(description='ProxyPool')
parser.add_argument('--processor', type=str, help='processor to run')
args = parser.parse_args()

if __name__ == '__main__':
    init_telemetry()

    flow_name = args.processor if args.processor else 'full_pool'
    flow_id = str(uuid.uuid4())
    flow_entries_counter.add(1, {'flow': flow_name})

    start_time = time.time()
    with tracer.start_as_current_span('proxypool.flow') as span:
        span.set_attribute('flow.id', flow_id)
        span.set_attribute('flow.name', flow_name)
        try:
            # if processor set, just run it
            if args.processor:
                getattr(Scheduler(), f'run_{args.processor}')()
            else:
                Scheduler().run()
        finally:
            exc_type, exc_value, _ = sys.exc_info()
            outcome = 'failure' if exc_type is not None else 'success'
            elapsed = time.time() - start_time
            span.set_attribute('flow.outcome', outcome)
            if exc_type is not None:
                span.set_status(trace.StatusCode.ERROR, str(exc_value))
                span.set_attribute('error.type', exc_type.__name__)
            flow_outcomes_counter.add(1, {'flow': flow_name, 'outcome': outcome})
            flow_duration_histogram.record(elapsed, {'flow': flow_name, 'outcome': outcome})
            flow_entry_to_terminal_histogram.record(
                elapsed, {'flow': flow_name, 'terminal_state': outcome}
            )
