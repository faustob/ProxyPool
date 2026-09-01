import hmac
import re
import time
from flask import Flask, g, request, abort
from opentelemetry import trace, metrics
from proxypool.exceptions import PoolEmptyException
from proxypool.storages.redis import RedisClient
from proxypool.setting import API_HOST, API_PORT, API_THREADED, API_KEY, IS_DEV, PROXY_RAND_KEY_DEGRADED
import functools
from random import choice, sample
from proxypool.utils.geo import get_country_iso

__all__ = ['app']

app = Flask(__name__)
if IS_DEV:
    app.debug = True

# OTel instruments: get_tracer/get_meter return proxy objects that rebind
# automatically once the SDK is registered (see proxypool/telemetry.py, wired
# from proxypool/scheduler.py's run_server, which runs post-fork in the child
# process that actually serves this app)
tracer = trace.get_tracer(__name__)
meter = metrics.get_meter(__name__)

# budget used to flag a request as slow for the P99 triage SLI
SLOW_REQUEST_THRESHOLD_SECONDS = 1.0

http_server_request_duration = meter.create_histogram(
    name='http.server.request.duration',
    unit='s',
    description='Duration of inbound HTTP requests, in seconds',
)

http_server_request_count = meter.create_counter(
    name='http.server.request.count',
    unit='1',
    description='Count of inbound HTTP requests by route and outcome class',
)

# allowed characters for the `key` query parameter that selects a redis sub-pool;
# restricts to a safe charset to avoid probing arbitrary redis keys via the API
VALID_KEY_PATTERN = re.compile(r'^[a-zA-Z0-9_:\-]{1,64}$')


@app.before_request
def _start_request_timer():
    """
    record the start time of the request for duration measurement
    """
    g._otel_request_start = time.monotonic()


@app.after_request
def _record_request_metrics(response):
    """
    emit http.server.request.duration / http.server.request.count for every
    response, and a slow-request span event when the P99 budget is exceeded
    """
    start_time = getattr(g, '_otel_request_start', None)
    duration = time.monotonic() - start_time if start_time is not None else 0
    route = request.url_rule.rule if request.url_rule is not None else 'other'
    outcome = 'success' if response.status_code < 500 else 'error'
    attributes = {
        'http.request.method': request.method,
        'url.scheme': request.scheme,
        'http.response.status_code': response.status_code,
        'http.route': route,
    }
    error_type = getattr(g, '_otel_error_type', None)
    if error_type:
        attributes['error.type'] = error_type
    http_server_request_duration.record(duration, attributes)
    http_server_request_count.add(1, {'http.route': route, 'outcome': outcome})
    if duration > SLOW_REQUEST_THRESHOLD_SECONDS:
        trace.get_current_span().add_event('slow_request', {
            'http.route': route,
            'duration_seconds': duration,
        })
    return response


@app.errorhandler(Exception)
def _handle_exception(exc):
    """
    tag the server span with the originating exception type before returning
    a generic 500; this only fires for exceptions that were previously left
    unhandled by Flask (HTTPException/abort() responses are unaffected)
    """
    g._otel_error_type = type(exc).__name__
    span = trace.get_current_span()
    span.set_attribute('error.type', type(exc).__name__)
    span.set_status(trace.StatusCode.ERROR, str(exc))
    return {'error': 'internal_error'}, 500


def auth_required(func):
    @functools.wraps(func)
    def decorator(*args, **kwargs):
        # conditional decorator, when setting API_KEY is set, otherwise just ignore this decorator
        if API_KEY == "":
            return func(*args, **kwargs)
        if request.headers.get('API-KEY', None) is not None:
            api_key = request.headers.get('API-KEY')
        else:
            return {"message": "Please provide an API key in header"}, 400
        # Check if API key is correct and valid
        if request.method == "GET" and hmac.compare_digest(api_key, API_KEY):
            return func(*args, **kwargs)
        else:
            return {"message": "The provided API key is not valid"}, 403

    return decorator


def get_conn():
    """
    get redis client object
    :return:
    """
    if not hasattr(g, 'redis'):
        g.redis = RedisClient()
    return g.redis


def get_request_key():
    """
    read the `key` query parameter and validate its format;
    reject unexpected characters to avoid redis key probing/injection
    :return: validated key or None
    """
    key = request.args.get('key')
    if key and not VALID_KEY_PATTERN.match(key):
        abort(400, description='invalid key parameter')
    return key


def filter_proxies_by_area(proxies, area):
    """
    filter proxies by country iso code (e.g. 'CN', 'US'), case-insensitive;
    proxies whose country cannot be resolved are excluded
    :param proxies: list of Proxy
    :param area: country iso code, or falsy to skip filtering
    :return: filtered list of Proxy
    """
    if not area:
        return proxies
    area = area.upper()
    return [proxy for proxy in proxies if get_country_iso(proxy.host) == area]


@app.route('/')
@auth_required
def index():
    """
    get home page, you can define your own templates
    :return:
    """
    return '<h2>Welcome to Proxy Pool System</h2>'


@app.route('/random')
@auth_required
def get_proxy():
    """
    get a random proxy, can query the specific sub-pool according the (redis) key
    if PROXY_RAND_KEY_DEGRADED is set to True, will get a universal random proxy if no proxy found in the sub-pool
    can pass a `count` parameter to get multiple random proxies at once
    can pass an `area` parameter to only get proxies from a country (iso code, e.g. CN)
    :return: get a random proxy
    """
    key = get_request_key()
    count = request.args.get('count', type=int)
    area = request.args.get('area')
    conn = get_conn()
    # return conn.random(key).string() if key else conn.random().string()
    if area:
        # area filtering needs the candidate set first, then filter by country
        candidates = conn.all(key) if key else conn.all()
        candidates = filter_proxies_by_area(candidates, area)
        if not candidates and key and PROXY_RAND_KEY_DEGRADED:
            candidates = filter_proxies_by_area(conn.all(), area)
        if not candidates:
            raise PoolEmptyException
        if count and count > 1:
            count = min(count, len(candidates))
            return '\n'.join(proxy.string() for proxy in sample(candidates, count))
        return choice(candidates).string()
    if count and count > 1:
        # return multiple random proxies, one per line
        try:
            proxies = conn.randoms(count, key) if key else conn.randoms(count)
        except PoolEmptyException:
            if key and PROXY_RAND_KEY_DEGRADED:
                proxies = conn.randoms(count)
            else:
                raise
        return '\n'.join(proxy.string() for proxy in proxies)
    if key:
        try:
            return conn.random(key).string()
        except PoolEmptyException:
            if not PROXY_RAND_KEY_DEGRADED:
                raise
    return conn.random().string()


@app.route('/all')
@auth_required
def get_proxy_all():
    """
    get all proxies, optionally filtered by `area` (country iso code, e.g. CN)
    :return: all proxies
    """
    key = get_request_key()
    area = request.args.get('area')

    conn = get_conn()
    proxies = conn.all(key) if key else conn.all()
    proxies = filter_proxies_by_area(proxies, area)
    proxies_string = ''
    if proxies:
        for proxy in proxies:
            proxies_string += str(proxy) + '\n'

    return proxies_string


@app.route('/count')
@auth_required
def get_count():
    """
    get the count of proxies
    :return: count, int
    """
    conn = get_conn()
    key = get_request_key()
    return str(conn.count(key)) if key else str(conn.count())


if __name__ == '__main__':
    app.run(host=API_HOST, port=API_PORT, threaded=API_THREADED)
