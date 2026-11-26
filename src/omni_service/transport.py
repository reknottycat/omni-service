"""Real HTTP transport restricted to literal loopback addresses for simulation."""

import http.client
import ipaddress
import json
import socket
from urllib.parse import urlsplit
from .models import ChannelError


class HttpTransport:
    def __init__(self, base_urls, timeout=3.0):
        self.base_urls = dict(base_urls)
        self.timeout = timeout
        for base in self.base_urls.values():
            self._endpoint(base)

    @staticmethod
    def _endpoint(base):
        parsed = urlsplit(base)
        if parsed.scheme != 'http' or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ChannelError('simulation_only', 'Simulation accepts only loopback HTTP endpoints.')
        host = parsed.hostname or ''
        if host == 'localhost':
            host = '127.0.0.1'
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ChannelError('simulation_only', 'The destination must be a literal loopback address.')
        return host, parsed.port or 80, parsed.path.rstrip('/')

    def send(self, channel, request):
        if channel not in self.base_urls:
            raise ChannelError('missing_endpoint', 'No simulated endpoint is configured for this channel.')
        host, port, prefix = self._endpoint(self.base_urls[channel])
        if not request.path.startswith('/') or request.path.startswith('//') or '\r' in request.path or '\n' in request.path:
            raise ChannelError('invalid_request', 'A relative API path is required.')
        body = None if request.body is None else json.dumps(request.body, ensure_ascii=False).encode('utf-8')
        headers = {'Content-Type': 'application/json', **request.headers}
        connection = http.client.HTTPConnection(host, port, timeout=self.timeout)
        try:
            connection.connect()
            if not ipaddress.ip_address(connection.sock.getpeername()[0]).is_loopback:
                raise ChannelError('simulation_only', 'Connected peer is not loopback.')
            connection.request(request.method, prefix + request.path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ChannelError('delivery_unknown', 'Response exceeds simulation size limit.')
            try:
                payload = json.loads(raw) if raw else {}
            except (ValueError, UnicodeDecodeError):
                raise ChannelError('delivery_unknown', 'Platform returned invalid JSON.') from None
            if not isinstance(payload, dict):
                raise ChannelError('delivery_unknown', 'Platform response must be an object.')
            if response.status == 429:
                parameters = payload.get('parameters')
                delay = (parameters.get('retry_after') if isinstance(parameters, dict) else None)
                delay = delay or response.getheader('Retry-After') or 1
                try:
                    delay = max(0.1, min(float(delay), 3600))
                except (TypeError, ValueError):
                    delay = 1
                raise ChannelError('rate_limited', 'Simulated platform rate limit.', delay)
            if response.status in (401, 403):
                raise ChannelError('authorization_failed', 'Simulated platform denied the request.')
            if 300 <= response.status < 400:
                raise ChannelError('redirect_rejected', 'Simulation never follows redirects.')
            if response.status >= 500:
                raise ChannelError('delivery_unknown', 'Server failure after sending the request.')
            if response.status >= 400:
                raise ChannelError('platform_rejected', 'Simulated platform rejected the request.')
            return payload
        except ChannelError:
            raise
        except (socket.timeout, TimeoutError, ConnectionError, OSError, http.client.HTTPException):
            raise ChannelError('delivery_unknown', 'The platform outcome could not be confirmed.') from None
        finally:
            connection.close()
