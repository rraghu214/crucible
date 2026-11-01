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


# -- Group 48, drafted 1 November 2026, approved by the operator the same day ---


class TestActuatorTagFilters:
    """heap_used_peak_bytes must be the heap. Actuator sums every tag combination
    unless asked, so an unfiltered jvm.memory.used read 267 MB on a 128 MB heap."""

    def test_a_declared_tag_is_sent_as_actuators_own_filter(self):
        client = _mock_client(json_body={"measurements": [{"statistic": "VALUE", "value": 1.0}]})
        provider = ActuatorMetricsProvider("http://app:8080", client=client,
                                           tags={"jvm.memory.used": {"area": "heap"}})
        provider.fetch("jvm.memory.used")
        client.get.assert_called_once_with("http://app:8080/actuator/metrics/jvm.memory.used",
                                           params=[("tag", "area:heap")])

    def test_a_meter_with_no_declared_tag_is_read_unfiltered(self):
        client = _mock_client(json_body={"measurements": []})
        provider = ActuatorMetricsProvider("http://app:8080", client=client,
                                           tags={"jvm.memory.used": {"area": "heap"}})
        provider.fetch("hikaricp.connections.pending")
        client.get.assert_called_once_with("http://app:8080/actuator/metrics/hikaricp.connections.pending",
                                           params=[])

    def test_the_spring_boot_profile_filters_the_heap_gauge_to_the_heap(self):
        from crucible.perf.profile import TargetProfile

        profile = TargetProfile.named("spring-boot")
        assert profile.actuator_tags["jvm.memory.used"] == {"area": "heap"}
        # The same filter the PromQL and Datadog views already apply.
        assert profile.promql["jvm.memory.used"]["labels"] == {"area": "heap"}
        assert profile.datadog["jvm.memory.used"]["tags"] == {"area": "heap"}


class TestATimerNotInSecondsIsDeclaredNotConverted:
    """Found 1 November 2026: on some starts PerfLab's /actuator/metrics answered from
    its Datadog registry, in milliseconds over the last 10 s, so timers read 1000x."""

    def _timer(self, unit):
        return {"baseUnit": unit, "measurements": [
            {"statistic": "COUNT", "value": 0.0}, {"statistic": "TOTAL_TIME", "value": 0.0},
            {"statistic": "MAX", "value": 70.27}]}

    def test_a_millisecond_timer_is_refused_and_declared(self):
        provider = ActuatorMetricsProvider("http://app:8080", client=_mock_client(json_body=self._timer("milliseconds")))
        assert provider.fetch("http.server.requests") is None
        declared = provider.unreadable["http.server.requests"]
        assert declared["reported_unit"] == "milliseconds"
        assert declared["unit"] == "unknown"
        assert declared["raw"]["MAX"] == 70.27  # carried, never dropped

    def test_a_seconds_timer_is_read(self):
        provider = ActuatorMetricsProvider("http://app:8080", client=_mock_client(json_body=self._timer("seconds")))
        assert provider.fetch("http.server.requests")["MAX"] == 70.27
        assert provider.unreadable == {}

    def test_a_gauge_in_bytes_is_not_a_timer_and_is_read(self):
        body = {"baseUnit": "bytes", "measurements": [{"statistic": "VALUE", "value": 5.0}]}
        provider = ActuatorMetricsProvider("http://app:8080", client=_mock_client(json_body=body))
        assert provider.fetch("jvm.memory.used") == {"VALUE": 5.0}

    def test_the_declaration_reaches_the_model(self):
        from crucible.perf.diagnosis import build_prompt

        snapshot = {"unreadable_metrics": {"http.server.requests": {
            "unit": "unknown", "reported_unit": "milliseconds", "reason": "not seconds"}}}
        prompt = build_prompt(snapshot, {"p99_ms": 120})
        assert "http.server.requests" in prompt and "milliseconds" in prompt
