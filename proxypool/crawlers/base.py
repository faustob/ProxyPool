from retrying import RetryError, retry
import requests
from loguru import logger
from proxypool.setting import GET_TIMEOUT
from fake_headers import Headers
import time
from typing import Dict, Any
from urllib.parse import urlparse
from opentelemetry import metrics


_meter = metrics.get_meter(__name__)
_http_client_duration = _meter.create_histogram(
    'http.client.request.duration',
    unit='s',
    description='Duration of outbound HTTP requests made by proxy crawlers to fetch proxy source pages',
)
_fetch_result_counter = _meter.create_counter(
    'proxypool.crawler.fetch.result',
    unit='1',
    description='Count of proxy source fetch attempts by outcome (success/failure) - backs proxy fetch success rate SLI',
)


class BaseCrawler(object):
    urls: list = []

    @retry(stop_max_attempt_number=3, retry_on_result=lambda x: x is None, wait_fixed=2000)
    def fetch(self, url, **kwargs):
        parsed_url = urlparse(url)
        server_address: str = parsed_url.hostname or ''
        server_port: int = parsed_url.port or (443 if parsed_url.scheme == 'https' else 80)
        start_time = time.time()
        try:
            headers = Headers(headers=True).generate()
            kwargs.setdefault('timeout', GET_TIMEOUT)
            kwargs.setdefault('verify', False)
            kwargs.setdefault('headers', headers)
            response = requests.get(url, **kwargs)
            duration_attrs: Dict[str, Any] = {
                'http.request.method': 'GET',
                'server.address': server_address,
                'server.port': server_port,
                'http.response.status_code': response.status_code,
            }
            _http_client_duration.record(time.time() - start_time, duration_attrs)
            if response.status_code == 200:
                _fetch_result_counter.add(1, {'outcome': 'success'})
                response.encoding = 'utf-8'
                return response.text
            else:
                _fetch_result_counter.add(1, {'outcome': 'failure'})
        except (requests.ConnectionError, requests.ReadTimeout) as e:
            error_attrs: Dict[str, Any] = {
                'http.request.method': 'GET',
                'server.address': server_address,
                'server.port': server_port,
                'error.type': type(e).__name__,
            }
            _http_client_duration.record(time.time() - start_time, error_attrs)
            _fetch_result_counter.add(1, {'outcome': 'failure'})
            return

    def process(self, html, url):
        """
        used for parse html
        """
        for proxy in self.parse(html):
            logger.info(f'fetched proxy {proxy.string()} from {url}')
            yield proxy

    def crawl(self):
        """
        crawl main method
        """
        try:
            for url in self.urls:
                logger.info(f'fetching {url}')
                html = self.fetch(url)
                if not html:
                    continue
                time.sleep(.5)
                yield from self.process(html, url)
        except RetryError:
            logger.error(
                f'crawler {self} crawled proxy unsuccessfully, '
                'please check if target url is valid or network issue')
