"""Tests for check_or_refresh_session race-condition lock.

Regression: two concurrent callers on an expired token both fired HTTP
requests, rotating the refreshToken twice.  The second rotation invalidated
the first caller's iotToken, causing 460 "iotToken blank" on the very next
send_cloud_command call, which then triggered further refresh attempts and
eventually a full account block on Aliyun.

Fix: _refresh_lock inside check_or_refresh_session serialises callers; the
second waiter finds the token fresh after the first caller returns and skips
the HTTP call entirely.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pymammotion.aliyun.cloud_gateway import CloudIOTGateway


def _make_session(iot_token_expire: int = 72000, issued_at: int | None = None) -> MagicMock:
    """Build a minimal SessionByAuthCodeResponse mock."""
    data = MagicMock()
    data.iotToken = "tok_initial"
    data.iotTokenExpire = iot_token_expire
    data.refreshToken = "ref_initial"
    data.refreshTokenExpire = 720000
    data.identityId = "identity123"
    session = MagicMock()
    session.data = data
    session.token_issued_at = issued_at if issued_at is not None else int(time.time()) - iot_token_expire - 7200
    return session


def _make_expired_session() -> MagicMock:
    """Session whose iotToken expired more than 1 h ago."""
    return _make_session(iot_token_expire=1, issued_at=0)


def _make_fresh_session() -> MagicMock:
    """Session whose iotToken is valid for the next 20 h."""
    return _make_session(iot_token_expire=72000, issued_at=int(time.time()))


def _make_gateway(session: MagicMock) -> CloudIOTGateway:
    """Build a CloudIOTGateway with a mocked MammotionHTTP."""
    http = MagicMock()
    region = MagicMock()
    region.data.apiGatewayEndpoint = "https://api.example.com"
    gw = CloudIOTGateway.__new__(CloudIOTGateway)
    gw.mammotion_http = http
    gw._app_key = "key"
    gw._app_secret = "secret"
    gw.domain = "aliyun.example.com"
    gw.message_delay = 1
    gw._rate_limited_until = 0.0
    gw._rate_limit_backoff = 60.0
    gw._client_id = "cid"
    gw._device_sn = "sn"
    gw._utdid = "utdid"
    gw._connect_response = None
    gw._login_by_oauth_response = None
    gw._aep_response = None
    gw._session_by_authcode_response = session
    gw._region_response = region
    gw._devices_by_account_response = None
    gw._iot_token_issued_at = session.token_issued_at
    gw._refresh_lock = asyncio.Lock()
    return gw


async def test_concurrent_calls_only_refresh_once() -> None:
    """Two concurrent callers on an expired token must produce exactly one HTTP call.

    Before the fix: both coroutines called the HTTP endpoint, rotating the
    refreshToken twice and invalidating the first caller's iotToken.
    After the fix: the second waiter finds the token fresh and returns early.
    """
    gw = _make_gateway(_make_expired_session())

    http_call_count = 0
    fresh_session = _make_fresh_session()

    async def _fake_refresh(*_args, **_kwargs) -> MagicMock:
        nonlocal http_call_count
        http_call_count += 1
        await asyncio.sleep(0.02)  # simulate network latency so both enter concurrently
        # Update gateway state as a real HTTP call would
        gw._session_by_authcode_response = fresh_session
        gw._iot_token_issued_at = int(time.time())
        resp = MagicMock()
        resp.body = b'{"code":200,"data":{"iotToken":"new_tok","iotTokenExpire":72000,"refreshToken":"new_ref","refreshTokenExpire":720000,"identityId":"id"}}'
        resp.status_message = "OK"
        resp.headers = {}
        resp.status_code = 200
        return resp

    with patch(
        "pymammotion.aliyun.cloud_gateway.Client.async_do_request",
        side_effect=_fake_refresh,
    ):
        with patch(
            "pymammotion.aliyun.cloud_gateway.SessionByAuthCodeResponse.from_dict",
            return_value=fresh_session,
        ):
            await asyncio.gather(
                gw.check_or_refresh_session(force=True),
                gw.check_or_refresh_session(force=True),
            )

    assert http_call_count == 1, (
        f"Expected exactly 1 HTTP refresh call, got {http_call_count}. "
        "Race condition: both concurrent callers fired a token rotation."
    )


async def test_force_bypasses_freshness_check() -> None:
    """force=True must hit the network even when the local token clock says fresh.

    This covers the account-blocked / 460-on-fresh-token case: Aliyun has
    rejected the token server-side even though our expiry timestamp is fine.
    Without force=True the freshness re-check would skip the HTTP call, silently
    dropping every subsequent command indefinitely.
    """
    gw = _make_gateway(_make_fresh_session())  # token is locally fresh

    http_called = False

    async def _fake_refresh(*_args, **_kwargs) -> MagicMock:
        nonlocal http_called
        http_called = True
        resp = MagicMock()
        resp.body = b'{"code":200,"data":{"iotToken":"t","iotTokenExpire":72000,"refreshToken":"r","refreshTokenExpire":720000,"identityId":"i"}}'
        resp.status_message = "OK"
        resp.headers = {}
        resp.status_code = 200
        return resp

    with patch(
        "pymammotion.aliyun.cloud_gateway.Client.async_do_request",
        side_effect=_fake_refresh,
    ):
        with patch(
            "pymammotion.aliyun.cloud_gateway.SessionByAuthCodeResponse.from_dict",
            return_value=_make_fresh_session(),
        ):
            await gw.check_or_refresh_session(force=True)

    assert http_called, "force=True must bypass the freshness re-check and hit the network"


async def test_fresh_token_skips_http_call() -> None:
    """check_or_refresh_session must be a no-op when the token is already fresh."""
    gw = _make_gateway(_make_fresh_session())

    with patch(
        "pymammotion.aliyun.cloud_gateway.Client.async_do_request",
        new_callable=AsyncMock,
    ) as mock_http:
        await gw.check_or_refresh_session()

    mock_http.assert_not_called()


async def test_second_waiter_skips_after_first_refreshes() -> None:
    """After the first caller refreshes, the second must not make another HTTP call."""
    gw = _make_gateway(_make_expired_session())
    http_calls: list[str] = []
    fresh = _make_fresh_session()

    async def _fake_refresh(*_args, **_kwargs) -> MagicMock:
        http_calls.append("refresh")
        await asyncio.sleep(0.01)
        gw._session_by_authcode_response = fresh
        gw._iot_token_issued_at = int(time.time())
        resp = MagicMock()
        resp.body = b'{"code":200,"data":{"iotToken":"t","iotTokenExpire":72000,"refreshToken":"r","refreshTokenExpire":720000,"identityId":"i"}}'
        resp.status_message = "OK"
        resp.headers = {}
        resp.status_code = 200
        return resp

    with patch(
        "pymammotion.aliyun.cloud_gateway.Client.async_do_request",
        side_effect=_fake_refresh,
    ):
        with patch(
            "pymammotion.aliyun.cloud_gateway.SessionByAuthCodeResponse.from_dict",
            return_value=fresh,
        ):
            # Run sequentially to confirm the second is a genuine no-op (not just lucky timing)
            await gw.check_or_refresh_session()  # first: refreshes
            await gw.check_or_refresh_session()  # second: token is now fresh → skip

    assert len(http_calls) == 1
