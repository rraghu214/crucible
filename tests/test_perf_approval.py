"""Approval-gate assertions — new group, week 2. DESIGN.md principle 4, §19.4.

REVIEWED AND APPROVED by the operator, 20 September 2026.

The gate is on the CHANGE, not on the transport (§19.4). Once an operator has
approved a configuration change, deploying it is a mechanical consequence rather
than a second decision — so these tests are about what the approval is bound to,
not about how many times someone is asked.

The binding tests are the ones that matter. An approval of "raise the pool to 20"
must not be redeemable for "raise the pool to 100". Without that, the gate
authorises a CATEGORY of action rather than the action itself, which is not what
the person clicking it believes they are doing. The check is reused from the S12
coding loop rather than reimplemented, so the two cannot drift apart.

The default-deny tests are the other half: a missing gate must not mean "apply
freely". That is the configuration mistake that turns an unattended campaign into
an unsupervised one, and it is exactly the kind of thing that reads as harmless
in a diff.
"""

import json

import pytest

from crucible.perf.applicator import Change, Proposal
from crucible.perf.approval import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalTimeout,
    DenyingGate,
    FileApprovalGate,
    ManualStep,
    PreapprovedGate,
    confirm_manual_step,
    pending_approvals,
    pending_manual_steps,
    write_decision,
)

RUN = "run-20260914-090000"


def a_proposal(**over) -> Proposal:
    base = {
        "cause_family": "connection_pool_exhaustion",
        "changes": (
            Change(prop="spring.datasource.hikari.maximum-pool-size", value=20, previous="10"),
        ),
        "reasoning": "pending peaked at 43 against a pool of 10; acquire mean 1003 ms",
        "confidence": 0.82,
        "predicted_p99_ms": 70.0,
        "evidence_cited": ("hikaricp.pending_peak_connections", "hikaricp.acquire_mean_ms"),
    }
    return Proposal(**(base | over))


class TestWhatTheOperatorIsAskedToApprove:
    """The card has to carry enough to make the decision, and no more."""

    def test_the_params_are_the_concrete_property_values(self):
        """The prose persuades; the parameters execute. Only the second can be
        compared mechanically, so only the second is the binding surface."""
        request = ApprovalRequest.from_proposal(a_proposal(), RUN, 1)

        assert request.params == {"spring.datasource.hikari.maximum-pool-size": 20}

    def test_the_card_shows_the_prediction_as_a_claim_not_a_result(self):
        """§4.5. The prediction is tracked and scored, never used as the verdict.
        An operator reading the card must not come away thinking p99 has already
        been measured at 70 ms."""
        text = ApprovalRequest.from_proposal(a_proposal(), RUN, 1).render()

        assert "70" in text
        assert "re-measures" in text

    def test_the_card_shows_the_confidence_as_the_agents_own_estimate(self):
        text = ApprovalRequest.from_proposal(a_proposal(), RUN, 1).render()

        assert "0.82" in text
        assert "not a measurement" in text

    def test_the_reasoning_and_the_cited_evidence_both_appear(self):
        text = ApprovalRequest.from_proposal(a_proposal(), RUN, 1).render()

        assert "pending peaked at 43" in text
        assert "hikaricp.pending_peak_connections" in text

    def test_params_are_ordered_so_two_identical_proposals_serialise_identically(self):
        """Not cosmetic: the audit trail is diffed, and a params dict whose key
        order depended on model output would make two identical approvals look
        different."""
        proposal = a_proposal(
            changes=(
                Change(prop="spring.datasource.hikari.minimum-idle", value=5),
                Change(prop="spring.datasource.hikari.maximum-pool-size", value=20),
            )
        )

        params = ApprovalRequest.from_proposal(proposal, RUN, 1).params

        assert list(params) == [
            "spring.datasource.hikari.maximum-pool-size",
            "spring.datasource.hikari.minimum-idle",
        ]


class TestTheDefaultIsToRefuse:
    """A missing gate must never mean 'apply freely'."""

    def test_the_denying_gate_refuses_and_says_why(self):
        decision = DenyingGate().request(ApprovalRequest.from_proposal(a_proposal(), RUN, 1))

        assert not decision.approved
        assert "no approval gate is configured" in decision.reason

    def test_a_preapproved_gate_records_that_nobody_was_asked(self):
        """§11: a run with human intervention is not comparable to one without.
        The inverse matters just as much — a preflight nobody reviewed must not be
        readable later as an approved campaign."""
        decision = PreapprovedGate().request(ApprovalRequest.from_proposal(a_proposal(), RUN, 1))

        assert decision.approved
        assert decision.responder == "preapproved"


class TestTheApprovalIsBoundToTheParametersShown:
    """S12's sixth invariant, reused here. The whole point of the gate."""

    def test_a_matching_decision_is_honoured(self, tmp_path):
        gate = _instant_gate(tmp_path)
        request = ApprovalRequest.from_proposal(a_proposal(), RUN, 1)
        _park_then_answer(gate, request, action="approve", params=dict(request.params))

        decision = gate.request(request)

        assert decision.approved
        assert decision.responder == "operator"

    def test_a_decision_with_widened_parameters_is_refused(self, tmp_path):
        """The attack this exists to stop: approve 20, redeem for 100. The value
        is inside the profile's bounds, so the guard would let it through — only
        the binding check knows the operator never saw it."""
        gate = _instant_gate(tmp_path)
        request = ApprovalRequest.from_proposal(a_proposal(), RUN, 1)
        _park_then_answer(
            gate, request, action="approve",
            params={"spring.datasource.hikari.maximum-pool-size": 100},
        )

        decision = gate.request(request)

        assert not decision.approved
        assert "differ from the parked parameters" in decision.reason

    def test_a_decision_naming_an_extra_property_is_refused(self, tmp_path):
        """Adding a property to an approved change is widening it, even when each
        individual value is in bounds. One approval authorises one change set."""
        gate = _instant_gate(tmp_path)
        request = ApprovalRequest.from_proposal(a_proposal(), RUN, 1)
        gate.run_dir.mkdir(parents=True, exist_ok=True)
        gate.request_path(1).write_text(json.dumps(request.as_dict()), encoding="utf-8")
        gate.decision_path(1).write_text(
            json.dumps({
                "action": "approve", "responder": "operator",
                "params": {
                    "spring.datasource.hikari.maximum-pool-size": 20,
                    "perflab.cache.enabled": False,
                },
            }),
            encoding="utf-8",
        )

        assert not gate.request(request).approved

    def test_an_explicit_rejection_is_recorded_as_a_rejection_not_an_error(self, tmp_path):
        """Declining is a normal outcome. It must be distinguishable from a
        timeout and from a malformed file, because they call for different things
        from the operator."""
        gate = _instant_gate(tmp_path)
        request = ApprovalRequest.from_proposal(a_proposal(), RUN, 1)
        _park_then_answer(
            gate, request, action="reject", params=dict(request.params),
            reason="want to see the trace first",
        )

        decision = gate.request(request)

        assert decision.state == "rejected"
        assert "trace" in decision.reason

    def test_an_unreadable_decision_file_is_not_an_approval(self, tmp_path):
        """Fails closed. A truncated or corrupt file is 'we do not know what they
        said', and the only safe reading of that is no."""
        gate = _instant_gate(tmp_path)
        request = ApprovalRequest.from_proposal(a_proposal(), RUN, 1)
        gate.run_dir.mkdir(parents=True, exist_ok=True)
        gate.request_path(1).write_text(json.dumps(request.as_dict()), encoding="utf-8")
        gate.decision_path(1).write_text("{not json", encoding="utf-8")

        assert not gate.request(request).approved

    def test_an_unexpected_action_is_refused(self, tmp_path):
        gate = _instant_gate(tmp_path)
        request = ApprovalRequest.from_proposal(a_proposal(), RUN, 1)
        _park_then_answer(gate, request, action="maybe", params=dict(request.params))

        assert not gate.request(request).approved


class TestParkingAndAnswering:
    """The two-process path: `crucible run` parks, `crucible approve` answers."""

    def test_the_request_is_written_where_another_process_can_find_it(self, tmp_path):
        gate = _instant_gate(tmp_path)
        request = ApprovalRequest.from_proposal(a_proposal(), RUN, 1)
        with pytest.raises(ApprovalTimeout):
            gate.request(request)

        parked = pending_approvals(tmp_path, RUN)

        assert len(parked) == 1
        assert parked[0]["params"] == {"spring.datasource.hikari.maximum-pool-size": 20}

    def test_an_answered_request_stops_being_pending(self, tmp_path):
        gate = _instant_gate(tmp_path)
        request = ApprovalRequest.from_proposal(a_proposal(), RUN, 1)
        with pytest.raises(ApprovalTimeout):
            gate.request(request)
        write_decision(tmp_path, RUN, 1, action="approve", responder="operator")

        assert pending_approvals(tmp_path, RUN) == []

    def test_write_decision_copies_the_params_from_the_request(self, tmp_path):
        """`crucible approve` gives no way to type different values. An operator
        approves WHAT THEY WERE SHOWN; offering a value flag would turn a bound
        approval into a blank cheque."""
        gate = _instant_gate(tmp_path)
        request = ApprovalRequest.from_proposal(a_proposal(), RUN, 1)
        with pytest.raises(ApprovalTimeout):
            gate.request(request)

        path = write_decision(tmp_path, RUN, 1, action="approve", responder="operator")
        written = json.loads(path.read_text(encoding="utf-8"))

        assert written["params"] == request.params

    def test_answering_an_approval_that_was_never_parked_fails_loudly(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="no pending approval"):
            write_decision(tmp_path, RUN, 7, action="approve", responder="operator")

    def test_a_timeout_pauses_rather_than_applying(self, tmp_path):
        """§7's pause holds without discarding. An operator who was asleep has not
        destroyed the run, and nothing was applied on their behalf."""
        gate = _instant_gate(tmp_path)

        with pytest.raises(ApprovalTimeout, match="paused, not failed"):
            gate.request(ApprovalRequest.from_proposal(a_proposal(), RUN, 1))

    def test_a_stale_decision_from_another_experiment_cannot_authorise_this_one(self, tmp_path):
        """Experiments are numbered and each has its own file. A decision left
        over from experiment 1 must not silently approve experiment 2, whose
        change the operator never saw."""
        gate = _instant_gate(tmp_path)
        first = ApprovalRequest.from_proposal(a_proposal(), RUN, 1)
        _park_then_answer(gate, first, action="approve", params=dict(first.params))
        assert gate.request(first).approved

        second = ApprovalRequest.from_proposal(_proposal_with_value(40), RUN, 2)
        with pytest.raises(ApprovalTimeout):
            gate.request(second)


class TestManualStepsShareTheDirectoryButNotTheMeaning:
    """W2-Q8. Reuse the plumbing, never the decision type."""

    def test_a_manual_step_is_not_listed_as_a_pending_approval(self, tmp_path):
        """Found by running `crucible status`. `*.request.json` also matches
        `NNN.manual.request.json`, so a parked manual step appeared as a pending
        APPROVAL and then failed reading a field it does not have. The two share
        a directory on purpose, so anything globbing it must say which it wants."""
        ManualStep(run_id=RUN, experiment=1, instructions="restart it", state_dir=tmp_path).park()

        assert pending_approvals(tmp_path, RUN) == []
        assert len(pending_manual_steps(tmp_path, RUN)) == 1

    def test_an_approval_is_not_listed_as_a_pending_manual_step(self, tmp_path):
        """The inverse, which would be just as wrong."""
        gate = _instant_gate(tmp_path)
        with pytest.raises(ApprovalTimeout):
            gate.request(ApprovalRequest.from_proposal(a_proposal(), RUN, 1))

        assert pending_manual_steps(tmp_path, RUN) == []
        assert len(pending_approvals(tmp_path, RUN)) == 1

    def test_both_can_be_pending_for_the_same_experiment(self, tmp_path):
        """The reason the filenames differ. One experiment waits twice -- once
        for authorisation, later for a manual step -- and a shared name would let
        the answer to one satisfy the other."""
        gate = _instant_gate(tmp_path)
        with pytest.raises(ApprovalTimeout):
            gate.request(ApprovalRequest.from_proposal(a_proposal(), RUN, 1))
        ManualStep(run_id=RUN, experiment=1, instructions="restart it", state_dir=tmp_path).park()

        assert len(pending_approvals(tmp_path, RUN)) == 1
        assert len(pending_manual_steps(tmp_path, RUN)) == 1

    def test_confirming_the_manual_step_does_not_approve_the_change(self, tmp_path):
        """A confirmation authorises nothing. If it satisfied the approval gate,
        an operator reporting a restart would silently have consented to the
        change itself."""
        gate = _instant_gate(tmp_path)
        request = ApprovalRequest.from_proposal(a_proposal(), RUN, 1)
        with pytest.raises(ApprovalTimeout):
            gate.request(request)
        ManualStep(run_id=RUN, experiment=1, instructions="restart", state_dir=tmp_path).park()
        confirm_manual_step(tmp_path, RUN, 1, responder="operator")

        with pytest.raises(ApprovalTimeout):
            gate.request(request)

    def test_a_confirmed_step_stops_being_pending(self, tmp_path):
        ManualStep(run_id=RUN, experiment=1, instructions="restart", state_dir=tmp_path).park()
        confirm_manual_step(tmp_path, RUN, 1, responder="operator")

        assert pending_manual_steps(tmp_path, RUN) == []


class TestDecisionSerialisation:
    def test_a_decision_round_trips_for_the_manifest(self):
        decision = ApprovalDecision(
            state="approved", responder="operator", reason="reviewed", params={"a": 1}
        )

        assert decision.as_dict()["state"] == "approved"
        assert decision.approved is True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _instant_gate(state_dir):
    """A gate that does not sleep, so a timeout test takes microseconds."""
    return FileApprovalGate(
        run_id=RUN, state_dir=state_dir, timeout_s=0.0, poll_interval_s=0.0,
        _sleep=lambda _s: None,
    )



def _park_then_answer(gate, request, *, action, params, reason=""):
    gate.run_dir.mkdir(parents=True, exist_ok=True)
    gate.request_path(request.experiment).write_text(
        json.dumps(request.as_dict()), encoding="utf-8"
    )
    gate.decision_path(request.experiment).write_text(
        json.dumps({
            "action": action, "responder": "operator", "reason": reason, "params": params,
        }),
        encoding="utf-8",
    )


def _proposal_with_value(value):
    return a_proposal(
        changes=(Change(prop="spring.datasource.hikari.maximum-pool-size", value=value),)
    )
