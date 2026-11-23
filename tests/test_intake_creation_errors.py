"""Malformed successful responses have unknown outcome, never a safe retry."""

from unittest.mock import AsyncMock

import httpx
import pytest

from bot.glpi.client import GlpiHTTPError
from tests.test_client import make_client


@pytest.mark.parametrize("body", [b"not json", b"[]", b"{}", b'{"id":0}', b'{"id":-1}'])
async def test_malformed_created_response_is_glpi_error_with_response(body):
    client = make_client()
    response = httpx.Response(201, content=body)
    client._request = AsyncMock(return_value=response)
    try:
        with pytest.raises(GlpiHTTPError) as error:
            await client.create_ticket(name="Issue", content="Original text", urgency=3)
        assert error.value.response is response
        client._request.assert_awaited_once()
    finally:
        await client.close()
