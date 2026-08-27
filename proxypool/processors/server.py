import hmac
import re
from flask import Flask, g, request, abort, got_request_exception
import time
from opentelemetry import trace
from proxypool.exceptions import PoolEmptyException
from proxypool.storages.redis import RedisClient
from proxypool.setting import API_HOST, API_PORT, API_THREADED, API_KEY, IS_DEV, PROXY_RAND_KEY_DEGRADED, SLOW_REQUEST_THRESHOLD
import functools
from random import choice, sample
from proxypool.utils.geo import get_country_iso
from proxypool.telemetry import http_server_request_duration

__all__ = ['app']

app = Flask(__name__)
if IS_DEV:
    app.debug = True

# allowed characters for the `key` query parameter that selects a redis sub-pool;
# restricts to a safe charset to avoid probing arbitrary redis keys via the API
VALID_KEY_PATTERN = re.compile(r'^[a-zA-Z0-9_:\-]{1,64}$')


@app.before_request
def _telemetry_record_request_start():
    """
    record the start time of the request so after_request can measure
    duration for both the http.server.request.duration histogram and the
    slow-request span event (P99 triage)
    """
    g._telemetry_start_time = time.monotonic()


@app.after_request
def _telemetry_record_request_metrics(response):
    """
    record http.server.request.duration for every request -- this single
    histogram backs the HTTP availability (status<500 vs all), P95/P99
    latency, 5xx error-rate and request-throughput SLIs -- and add a span
    event when the handler exceeds the configured latency budget to help
    triage P99 regressions (e.g. unfiltered /all against a large proxy set)
    """
    start_time = getattr(g, '_telemetry_start_time', None)
    if start_time is not None:
        duration = time.monotonic() - start_time
        route = request.url_rule.rule if request.url_rule else 'unmatched'
        http_server_request_duration.record(
            duration,
            {
                'http.request.method': request.method,
                'url.scheme': request.scheme,
                'http.response.status_code': response.status_code,
                'http.route': route,
            },
        )
        if duration > SLOW_REQUEST_THRESHOLD:
            span = trace.get_current_span()
            span.add_event(
                'slow_request',
                {
                    'http.route': route,
                    'http.response.status_code': response.status_code,
                    'duration_s': duration,
                },
            )
    return response


def _telemetry_record_exception(sender, exception, **extra):
    """
    listen for Flask's got_request_exception signal (fired before the
    exception propagates to Flask's default error handling) to tag the
    current span with the originating exception class, without catching
    or altering the exception itself
    """
    span = trace.get_current_span()
    span.set_attribute('error.type', type(exception).__name__)
    span.set_status(trace.StatusCode.ERROR, str(exception))


got_request_exception.connect(_telemetry_record_exception, app)


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
