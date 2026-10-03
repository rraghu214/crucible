"""Tests for ActuatorMetricsProvider.fetch_all_env().

Non-negotiable 7: /actuator/env must be fetched before any snapshot reaches
a model or journal, and the result must pass through the redaction allowlist.
These tests verify the fetch side; collector.py's redact_runtime_config tests
cover the redaction side.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from crucible.perf.providers.actuator import ActuatorMetricsProvider


def _mock_client(status_code: int = 200, json_body: object = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body or {}
    if status_code >= 400:
        import httpx  # noqa: PLC0415
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=resp
        )
    else:
        resp.raise_for_status.return_value = None
    client = MagicMock()
    client.get.return_value = resp
    return client


_ENV_PAYLOAD = {
    "activeProfiles": ["lab"],
    "propertySources": [
        {
            "name": "commandLineArgs",
            "properties": {},
        },
        {
            "name": "applicationConfig: [classpath:/application-lab.properties]",
            "properties": {
                "spring.datasource.hikari.maximum-pool-size": {"value": "5"},
                "spring.datasource.hikari.minimum-idle": {"value": "5"},
            },
        },
        {
            "name": "applicationConfig: [classpath:/application.properties]",
            "properties": {
                "spring.datasource.url": {"value": "jdbc:postgresql://localhost:5432/perflab"},
                "spring.datasource.username": {"value": "perflab"},
                "spring.datasource.password": {"value": "s3cr3t"},
                # lower-priority duplicate — should be ignored
                "spring.datasource.hikari.maximum-pool-size": {"value": "10"},
            },
        },
    ],
}


class TestFetchAllEnv:
    def test_returns_flat_dict_of_all_properties(self) -> None:
        provider = ActuatorMetricsProvider("http://app:8080", client=_mock_client(json_body=_ENV_PAYLOAD))
        result = provider.fetch_all_env()
        assert result is not None
        assert result["spring.datasource.hikari.maximum-pool-size"] == "5"
        assert result["spring.datasource.url"] == "jdbc:postgresql://localhost:5432/perflab"
        assert result["spring.datasource.password"] == "s3cr3t"

    def test_higher_priority_source_wins_on_duplicate_key(self) -> None:
        # application-lab.properties (index 1) declares pool=5;
        # application.properties (index 2) declares pool=10.
        # Spring resolves with index 1 winning -- so should we.
        provider = ActuatorMetricsProvider("http://app:8080", client=_mock_client(json_body=_ENV_PAYLOAD))
        result = provider.fetch_all_env()
        assert result is not None
        assert result["spring.datasource.hikari.maximum-pool-size"] == "5"

    def test_returns_none_on_http_error(self) -> None:
        provider = ActuatorMetricsProvider("http://app:8080", client=_mock_client(status_code=500))
        assert provider.fetch_all_env() is None

    def test_returns_none_on_empty_sources(self) -> None:
        provider = ActuatorMetricsProvider(
            "http://app:8080",
            client=_mock_client(json_body={"activeProfiles": [], "propertySources": []}),
        )
        assert provider.fetch_all_env() is None

    def test_returns_none_when_all_sources_empty(self) -> None:
        payload = {
            "propertySources": [
                {"name": "commandLineArgs", "properties": {}},
            ]
        }
        provider = ActuatorMetricsProvider("http://app:8080", client=_mock_client(json_body=payload))
        assert provider.fetch_all_env() is None

    def test_correct_endpoint_is_called(self) -> None:
        client = _mock_client(json_body=_ENV_PAYLOAD)
        provider = ActuatorMetricsProvider("http://app:8080", client=client)
        provider.fetch_all_env()
        client.get.assert_called_once_with("http://app:8080/actuator/env")

    def test_trailing_slash_stripped_from_base_url(self) -> None:
        client = _mock_client(json_body=_ENV_PAYLOAD)
        provider = ActuatorMetricsProvider("http://app:8080/", client=client)
        provider.fetch_all_env()
        client.get.assert_called_once_with("http://app:8080/actuator/env")
