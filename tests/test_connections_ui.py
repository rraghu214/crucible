"""Settings: metrics connections, as a browser-session preview. Group 56.

DRAFTED by Claude Code, 2 November 2026. NOT YET REVIEWED by the operator.
See `docs/CRUCIBLE_TEST_ASSERTIONS.md` GROUP 56 for the questions to check.

Operator, 2 November 2026: Settings should let you name connections to different
metrics providers, add as many as you like, and later pick one by name in a profile or
SLA. For now the UI feature alone: kept in the browser session, not sent to the server.
That makes honesty the main risk: a page that lets you "choose Datadog" must not
suggest that a run now reads Datadog. What is asserted:

1. every metrics provider in DESIGN.md §5 is in the dropdown, and "available" means
   exactly the adapters Crucible has;
2. a credential is the name of an environment variable, never a secret, and the rule
   that enforces it rejects real-looking secrets;
3. connection names are plain and unique, and the built-in one cannot be removed;
4. nothing about connections is sent to the server, and no run input reads them;
5. they live in the session (not local storage), and storage failures are survived;
6. the page says plainly that it is a preview and that runs still read Actuator.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from crucible.perf.commands import MEASURABLE_PROVIDERS

REPO = Path(__file__).resolve().parents[1]
PAGE = (REPO / "crucible" / "ui" / "client" / "crucible-ui.html").read_text(encoding="utf-8")


def _block(start: str, end: str) -> str:
    return PAGE[PAGE.index(start):PAGE.index(end, PAGE.index(start))]


CONNECTIONS_JS = _block("const CONN_NAME_RE", "(function initTokenDisplay(){")
PROVIDERS = re.findall(r"\{id:'([a-z]+)',label:'([^']+)',status:'([a-z]+)'", CONNECTIONS_JS)


def _regex(name: str) -> re.Pattern[str]:
    return re.compile(re.search(rf"const {name}=/(.*)/;", CONNECTIONS_JS).group(1))


# -- 56.1 ---------------------------------------------------------------------


class TestEveryProviderIsListed:
    def test_the_design_tables_metrics_providers_are_all_in_the_dropdown(self):
        """DESIGN.md §5: Actuator, PromQL, Datadog, Dynatrace, New Relic, Elastic (Grafana speaks PromQL)."""
        assert {i for i, _, _ in PROVIDERS} >= {"actuator", "promql", "datadog", "dynatrace", "newrelic", "elastic",
                                                 "grafana"}

    def test_available_means_exactly_the_adapters_crucible_has(self):
        """If an adapter is added to or removed from the code, this fails until the page says so."""
        assert {i for i, _, s in PROVIDERS if s == "available"} == set(MEASURABLE_PROVIDERS)

    def test_providers_without_an_adapter_say_so(self):
        status = {i: s for i, _, s in PROVIDERS}
        assert status["grafana"] == "preview"
        assert all(status[i] == "planned" for i in ("dynatrace", "newrelic", "elastic"))
        assert "' (planned)'" in CONNECTIONS_JS and "' (preview)'" in CONNECTIONS_JS


# -- 56.2 ---------------------------------------------------------------------


class TestACredentialIsAVariableNameNeverASecret:
    def test_every_credential_field_is_an_env_name_and_only_those(self):
        fields = re.findall(r"\['([a-z_]+)','[^']+','[^']*',(true|false)\]", CONNECTIONS_JS)
        assert fields, "no provider fields found"
        for key, is_env in fields:
            assert (key.endswith("_env")) == (is_env == "true"), key

    def test_no_connection_field_is_a_password_input(self):
        assert "password" not in CONNECTIONS_JS.lower()

    @pytest.mark.parametrize("value", ["DATADOG_API_KEY", "DATADOG_APP_KEY", "PROM_TOKEN", "NEW_RELIC_API_KEY"])
    def test_a_variable_name_is_accepted(self, value):
        assert _regex("CONN_ENV_RE").match(value)

    @pytest.mark.parametrize("value", [
        "d41d8cd98f00b204e9800998ecf8427e",       # a hex key
        "sk-live-abc123def456",                   # a prefixed secret
        "AKIAIOSFODNN7EXAMPLE",                   # all capitals but no underscore
        "datadog_api_key",                        # lower case: a value, not the usual name
        "TOKEN", "A_", "_A", "A B",
        "A" + "_B" * 40,                          # too long
    ])
    def test_a_secret_or_a_non_name_is_refused(self, value):
        assert not _regex("CONN_ENV_RE").match(value)

    def test_the_page_tells_the_operator_the_credential_is_a_name(self):
        assert "never typed here" in PAGE and "never stored, shown or logged" in PAGE


# -- 56.3 ---------------------------------------------------------------------


class TestNamesAndTheBuiltInConnection:
    @pytest.mark.parametrize("name", ["datadog-prod", "prom_1", "a", "grafana-cloud-eu"])
    def test_plain_names_are_accepted(self, name):
        assert _regex("CONN_NAME_RE").match(name)

    @pytest.mark.parametrize("name", ["Datadog", "-x", "has space", "a" * 41, "", "x/y", "x;y"])
    def test_anything_else_is_refused(self, name):
        assert not _regex("CONN_NAME_RE").match(name)

    def test_a_name_must_be_new_and_not_the_built_in_one(self):
        assert "name===CONN_BUILTIN.name||list.some(c=>c.name===name)" in CONNECTIONS_JS

    def test_the_built_in_connection_has_no_remove_button(self):
        assert "if(!c.builtin){" in CONNECTIONS_JS
        body = CONNECTIONS_JS[CONNECTIONS_JS.index("if(!c.builtin){"):]
        assert "removeConnection" in body[:body.index("box.appendChild(row)")]
        assert "builtin:true" in CONNECTIONS_JS


# -- 56.4 ---------------------------------------------------------------------


class TestNothingReachesTheServer:
    def test_no_request_mentions_connections(self):
        assert not re.search(r"(fetch|apiPost)\([^)]*connection", PAGE, re.I)

    def test_the_wizard_body_does_not_carry_them(self):
        wizard_body = _block("function wizardBody()", "function _renderChecks")
        assert "conn" not in wizard_body.lower()

    def test_the_test_button_is_disabled_until_the_server_side_exists(self):
        assert "textContent:'Test'" in CONNECTIONS_JS and "disabled:true" in CONNECTIONS_JS
        assert "Phase 2" in CONNECTIONS_JS


# -- 56.5 ---------------------------------------------------------------------


class TestKeptInTheSessionOnly:
    def test_session_storage_not_local_storage(self):
        assert "sessionStorage" in CONNECTIONS_JS and "localStorage" not in CONNECTIONS_JS

    def test_every_storage_access_survives_a_failure(self):
        for call in ("sessionStorage.getItem", "sessionStorage.setItem"):
            at = CONNECTIONS_JS.index(call)
            assert "try{" in CONNECTIONS_JS[max(0, at - 120):at], call


# -- 56.6 ---------------------------------------------------------------------


class TestThePageIsHonest:
    def test_it_says_preview_session_only_and_that_runs_still_read_actuator(self):
        card = _block('id="conn-card"', 'id="connList"')
        assert "preview" in card
        assert "this browser session only" in card and "not sent to the server" in card
        assert "Every run still reads the target's own Actuator endpoint" in card
        assert "Phase 2" in card
