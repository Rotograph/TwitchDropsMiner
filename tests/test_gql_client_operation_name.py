"""A "service error" reply without "extensions" is retried once; the log line still names the operation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.gql_client import GQLClient
from src.config import ClientType, GQLRawQuery


OK = {"data": {"x": 1}}


def client(monkeypatch, replies):
    monkeypatch.setattr("src.api.gql_client.asyncio.sleep", AsyncMock())
    browser = SimpleNamespace(gql=AsyncMock(side_effect=list(replies)))
    auth = SimpleNamespace(validate=AsyncMock(return_value=SimpleNamespace(
        browser_active=True, _twitch=SimpleNamespace(_browser=browser),
    )))
    return GQLClient(MagicMock(), auth, ClientType.WEB), browser


def named(name, query="query { x }"):
    op = GQLRawQuery(query)
    if name is not None:
        op["operationName"] = name
    return op


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["service error", "PersistedQueryNotFound"])
@pytest.mark.parametrize("op, logged", [(named("Named"), "Named"), (named(None), "unknown")])
async def test_single_retry_reply_without_extensions_is_retried_once(monkeypatch, caplog, message, op, logged):
    # a reply without "extensions" raised KeyError in the log line instead of being retried
    gql, browser = client(monkeypatch, [{"errors": [{"message": message}]}, OK])
    assert await gql.request(op) == OK
    assert browser.gql.call_count == 2
    assert f"Retrying a {message} for {logged}" in caplog.text


@pytest.mark.asyncio
async def test_single_retry_log_names_the_batch_entry_that_failed(monkeypatch, caplog):
    gql, browser = client(monkeypatch, [[OK, {"errors": [{"message": "service error"}], "extensions": []}], [OK, OK]])
    assert await gql.request([named("A"), named("B")]) == [OK, OK]
    assert browser.gql.call_count == 2
    assert "Retrying a service error for B" in caplog.text
