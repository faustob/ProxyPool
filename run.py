from proxypool.scheduler import Scheduler
import argparse
import time

from opentelemetry import trace

from proxypool import telemetry


parser = argparse.ArgumentParser(description='ProxyPool')
parser.add_argument('--processor', type=str, help='processor to run')
args = parser.parse_args()

if __name__ == '__main__':
    flow_outcome = 'success'
    flow_start = time.monotonic()
    with telemetry.tracer.start_as_current_span('proxypool.flow') as span:
        span.set_attribute('flow.name', 'proxy_lifecycle')
        telemetry.flow_entries_counter.add(1, {'flow': 'proxy_lifecycle'})
        try:
            # if processor set, just run it
            if args.processor:
                getattr(Scheduler(), f'run_{args.processor}')()
            else:
                Scheduler().run()
            telemetry.flow_validation_outcomes_counter.add(1, {'flow': 'proxy_lifecycle', 'outcome': 'pass'})
        except Exception as exc:
            flow_outcome = 'failed'
            span.set_status(trace.StatusCode.ERROR, str(exc))
            span.set_attribute('error.type', type(exc).__name__)
            telemetry.flow_validation_outcomes_counter.add(1, {'flow': 'proxy_lifecycle', 'outcome': 'fail'})
            raise
        finally:
            flow_duration = time.monotonic() - flow_start
            span.set_attribute('flow.outcome', flow_outcome)
            telemetry.flow_outcomes_counter.add(1, {'flow': 'proxy_lifecycle', 'outcome': flow_outcome})
            telemetry.flow_duration_histogram.record(
                flow_duration, {'flow': 'proxy_lifecycle', 'outcome': flow_outcome}
            )
            telemetry.flow_entry_to_terminal_histogram.record(
                flow_duration, {'flow': 'proxy_lifecycle', 'terminal_state': flow_outcome}
            )
