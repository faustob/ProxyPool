from proxypool.telemetry import setup_telemetry

# Register the global OTel SDK + Flask/Redis instrumentors before anything
# else is imported, since importing Scheduler transitively constructs the
# Flask app and Redis client.
setup_telemetry()

from proxypool.scheduler import Scheduler
import argparse


parser = argparse.ArgumentParser(description='ProxyPool')
parser.add_argument('--processor', type=str, help='processor to run')
args = parser.parse_args()

if __name__ == '__main__':
    # if processor set, just run it
    if args.processor:
        getattr(Scheduler(), f'run_{args.processor}')()
    else:
        Scheduler().run()
