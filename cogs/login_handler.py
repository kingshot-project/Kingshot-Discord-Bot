"""
Centralized login and player data handler for Kingshot. Single-API access with rate limiting.
"""
import aiohttp
import asyncio
import hashlib
import time
import ssl
import logging
from typing import Optional, List, Dict, Callable

from .pimp_my_bot import theme
from .browser_headers import get_headers

logger = logging.getLogger('bot')


class LoginHandler:
    """
    Centralized handler for player login/check operations for Kingshot.
    Kingshot has a single login API, so dual-API mode is never active —
    the dual-API attributes (api2_*, dual_api_mode) are preserved for
    interface compatibility with the WOS-style call sites.
    Note: This does NOT handle gift code operations which have separate rate limits.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(LoginHandler, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        # API Configuration — Kingshot has only one login API
        self.api1_url = 'https://kingshot-giftcode.centurygame.com/api/player'
        self.api2_url = None
        self.secret = 'mN4!pQs6JrYwV9'

        # Rate limiting
        self.api1_requests = []
        self.api2_requests = []  # always empty — kept for interface compatibility
        self.rate_limit_per_api = 30
        self.rate_limit_window = 60
        self.last_api_used = 1

        # Retry transient failures (timeouts, 5xx) before reporting an error
        self.max_retries = 3
        self.retry_delay = 3.0

        # API mode — always single, never dual
        self.dual_api_mode = False
        self.available_apis = []
        self.request_delay = 2.0

        # Alliance operation locks to prevent conflicts
        self.alliance_locks = {}

        self.ssl_context = self._create_ssl_context()

        self._initialized = True

    def _create_ssl_context(self):
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        return ssl_context

    def get_alliance_lock(self, alliance_id: str) -> asyncio.Lock:
        if alliance_id not in self.alliance_locks:
            self.alliance_locks[alliance_id] = asyncio.Lock()
        return self.alliance_locks[alliance_id]

    async def check_apis_availability(self, test_fid: str = "43180889") -> Dict[str, bool]:
        """
        Check if the Kingshot login API is available.
        Returns: dict with api1_available (api2 always False).
        """
        api_status = {
            "api1_available": False,
            "api2_available": False,
            "api1_url": self.api1_url,
            "api2_url": self.api2_url,
        }

        connector = aiohttp.TCPConnector(ssl=self.ssl_context)

        async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
            try:
                current_time = int(time.time() * 1000)
                form = f"fid={test_fid}&time={current_time}"
                sign = hashlib.md5((form + self.secret).encode('utf-8')).hexdigest()
                form = f"sign={sign}&{form}"
                headers = get_headers('https://kingshot-giftcode.centurygame.com')

                async with session.post(self.api1_url, headers=headers, data=form, timeout=5) as response:
                    api_status["api1_available"] = response.status in [200, 429]
            except Exception as e:
                logger.error(f"API availability check failed: {e}")
                api_status["api1_available"] = False

        if api_status["api1_available"]:
            self.available_apis = [1]
            self.request_delay = 2.0
        else:
            self.available_apis = []

        return api_status

    # Alias for callers that use the singular form
    check_api_availability = check_apis_availability

    def _get_available_api(self) -> Optional[int]:
        """
        Determine whether the API is available based on rate limits.
        Returns: 1 if available, or a (None, wait_time) tuple if rate-limited.
        """
        now = time.time()
        self.api1_requests = [t for t in self.api1_requests if now - t < self.rate_limit_window]

        if not self.available_apis:
            return None, 0

        if len(self.api1_requests) < self.rate_limit_per_api:
            return 1

        wait_time = self.rate_limit_window - (now - self.api1_requests[0]) if self.api1_requests else 0
        return None, max(0, wait_time)

    def _record_api_request(self, api_num: int):
        now = time.time()
        self.api1_requests.append(now)
        self.last_api_used = 1

    def _get_wait_time(self) -> float:
        now = time.time()
        wait_time = self.rate_limit_window - (now - self.api1_requests[0]) if self.api1_requests else 0
        return max(0, wait_time)

    async def fetch_player_data(self, fid: str, use_proxy: Optional[str] = None, retry: bool = True) -> Dict:
        """
        Fetch player login data (nickname, town center level, kid, etc.) from the Kingshot API.
        Returns a dict with 'status', 'data', 'api_used', 'error_message'.
        Transient failures (timeouts, 5xx) are retried before reporting an error.
        """
        api_result = self._get_available_api()

        if api_result is None or (isinstance(api_result, tuple) and api_result[0] is None):
            wait_time = api_result[1] if isinstance(api_result, tuple) else self._get_wait_time()
            return {
                'status': 'rate_limited',
                'data': None,
                'wait_time': wait_time,
                'error_message': f'Rate limit reached. Wait {wait_time:.1f} seconds.'
            }

        api_num = api_result if isinstance(api_result, int) else api_result
        api_url = self.api1_url

        current_time = int(time.time() * 1000)
        form = f"fid={fid}&time={current_time}"
        sign = hashlib.md5((form + self.secret).encode('utf-8')).hexdigest()
        form = f"sign={sign}&{form}"
        headers = get_headers(api_url.rsplit('/api/', 1)[0])

        last_error = 'Unknown error'
        attempts = self.max_retries if retry else 1
        for attempt in range(attempts):
            try:
                if use_proxy:
                    from aiohttp_socks import ProxyConnector
                    connector = ProxyConnector.from_url(use_proxy, ssl=self.ssl_context)
                else:
                    connector = aiohttp.TCPConnector(ssl=self.ssl_context)

                async with aiohttp.ClientSession(connector=connector, trust_env=True) as session:
                    async with session.post(api_url, headers=headers, data=form, timeout=aiohttp.ClientTimeout(total=15)) as response:
                        self._record_api_request(api_num)

                        if response.status == 200:
                            data = await response.json()

                            if data.get('data'):
                                return {
                                    'status': 'success',
                                    'data': data['data'],
                                    'api_used': api_num,
                                    'error_message': None
                                }

                            elif data.get('err_code') == 40004 or (data.get('err_code') == 40001 and 'role not exist' in str(data.get('msg', '')).lower()):
                                return {
                                    'status': 'not_found',
                                    'data': None,
                                    'api_used': api_num,
                                    'error_message': 'Player does not exist (role not exist)',
                                    'err_code': data.get('err_code')
                                }

                            else:
                                err_code = data.get('err_code', 'unknown')
                                err_msg = data.get('msg', 'Unknown error')
                                return {
                                    'status': 'error',
                                    'data': None,
                                    'api_used': api_num,
                                    'error_message': f'API Error {err_code}: {err_msg}',
                                    'err_code': err_code
                                }
                        elif response.status == 429:
                            return {
                                'status': 'rate_limited',
                                'data': None,
                                'api_used': api_num,
                                'error_message': 'Unexpected rate limit'
                            }
                        elif response.status >= 500:
                            # Transient upstream/server error - retry
                            last_error = f'HTTP {response.status}'
                        else:
                            return {
                                'status': 'error',
                                'data': None,
                                'api_used': api_num,
                                'error_message': f'HTTP {response.status}'
                            }

            except Exception as e:
                last_error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__

            # Transient failure - back off and retry unless this was the last attempt
            if attempt < attempts - 1:
                await asyncio.sleep(self.retry_delay)

        logger.error(f"Error fetching player data for ID {fid} after {attempts} attempt(s): {last_error}")
        return {
            'status': 'error',
            'data': None,
            'api_used': api_num,
            'error_message': last_error,
        }

    def get_mode_text(self, for_console: bool = False) -> str:
        """Human-readable description of current API mode."""
        if self.available_apis:
            prefix = "" if for_console else f"{theme.verifiedIcon} "
            return f"{prefix}Kingshot API online (1 member/2 seconds)"
        prefix = "" if for_console else f"{theme.deniedIcon} "
        return f"{prefix}Kingshot API unavailable"

    def get_processing_rate(self) -> str:
        if self.available_apis:
            return f"{theme.boltIcon} Rate: 1 member/2 seconds"
        return f"{theme.deniedIcon} Service unavailable"

