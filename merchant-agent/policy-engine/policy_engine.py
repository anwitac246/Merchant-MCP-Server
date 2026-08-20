"""
Policy Engine — the Authority Layer.

Design principle: this module NEVER trusts values passed in by the agent.
Every "check" function below is expected to be called with values that the
Commerce Core has already independently computed (real stock, real price,
real quote hash) — not values echoed back by the agent. The engine's only
job is to decide ALLOW / REQUIRE_APPROVAL / BLOCK and produce a full,
explainable trace for the audit log.
"""

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable


class Decision(str, Enum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    BLOCK = "BLOCK"

    @property
    def severity(self) -> int:
        # Used to pick the strictest decision across all fired rules.
        return {"ALLOW": 0, "REQUIRE_APPROVAL": 1, "BLOCK": 2}[self.value]


@dataclass
class RuleResult:
    rule_id: str
    category: str
    passed: bool
    decision_if_failed: Decision
    reason: str | None = None


@dataclass
class PolicyContext:
    """
    Everything the engine needs, sourced ONLY from Commerce Core / server
    state — never from raw agent input. Build this in checkout.py by
    querying inventory_service, quote_service, and session state directly.
    """
    sku: str
    requested_qty: int
    available_stock: int
    stock_flags: list[str]
    quote_id: str | None
    quote_issued_at: float | None
    quote_ttl_seconds: int | None
    quote_cart_hash: str | None
    current_cart_hash: str
    transaction_total_inr: float
    session_cumulative_spend_inr: float
    session_verified: bool
    session_checkout_attempts: int
    session_window_seconds: int

    # --- added for comprehensive edge-case coverage ---
    quote_consumed: bool = False              # has this quote already been used in a checkout attempt?
    reservation_held: bool = True              # is the stock reservation from quote-issue time still valid?
    quote_total_inr: float | None = None       # authoritative total from the quote object itself
    payment_amount_inr: float | None = None    # amount actually being sent to the payment provider
    approver_session_id: str | None = None     # who is approving (None if no approval in play)
    requester_session_id: str | None = None    # who requested the transaction
    approval_snapshot_hash: str | None = None  # cart+total hash the approval was granted against
    current_snapshot_hash: str | None = None   # cart+total hash right now, at payment time

    now: float = field(default_factory=time.time)


class PolicyEngine:
    def __init__(self, rules_path: str | Path):
        with open(rules_path, "r") as f:
            self.config = json.load(f)
        self.rules = self.config["rules"]
        self._checks: dict[str, Callable[[dict, PolicyContext], RuleResult]] = {
            "qty_within_stock": self._check_qty_within_stock,
            "stock_nonzero": self._check_stock_nonzero,
            "quote_not_expired": self._check_quote_not_expired,
            "quote_matches_cart": self._check_quote_matches_cart,
            "transaction_total_under": self._check_transaction_total_under,
            "session_cumulative_under": self._check_session_cumulative_under,
            "sku_has_flag": self._check_sku_has_flag,
            "checkout_attempts_under": self._check_checkout_attempts_under,
            "verified_session_or_under": self._check_verified_session_or_under,
            "quote_not_consumed": self._check_quote_not_consumed,
            "reservation_still_held": self._check_reservation_still_held,
            "qty_is_positive_integer": self._check_qty_is_positive_integer,
            "payment_amount_matches_quote": self._check_payment_amount_matches_quote,
            "approver_not_requester": self._check_approver_not_requester,
            "approval_matches_snapshot": self._check_approval_matches_snapshot,
        }

    def evaluate(self, ctx: PolicyContext) -> dict[str, Any]:
        """
        Runs EVERY rule (never short-circuits) and returns the strictest
        decision plus a full trace. The trace is what gets written to the
        audit log — it's the "why" behind every ALLOW/APPROVAL/BLOCK.
        """
        results: list[RuleResult] = []
        for rule in self.rules:
            check_fn = self._checks[rule["check"]]
            result = check_fn(rule, ctx)
            results.append(result)

        final_decision = Decision.ALLOW
        for r in results:
            if not r.passed and r.decision_if_failed.severity > final_decision.severity:
                final_decision = r.decision_if_failed

        return {
            "decision": final_decision.value,
            "evaluated_at": ctx.now,
            "trace": [
                {
                    "rule_id": r.rule_id,
                    "category": r.category,
                    "passed": r.passed,
                    "decision_if_failed": r.decision_if_failed.value,
                    "reason": r.reason,
                }
                for r in results
            ],
            "failed_rules": [r.rule_id for r in results if not r.passed],
        }

    # ---- individual check implementations ----

    def _check_qty_within_stock(self, rule: dict, ctx: PolicyContext) -> RuleResult:
        passed = ctx.requested_qty <= ctx.available_stock
        reason = None if passed else rule["reason_template"].format(
            requested_qty=ctx.requested_qty, available_stock=ctx.available_stock, sku=ctx.sku
        )
        return RuleResult(rule["id"], rule["category"], passed, Decision(rule["on_fail"]), reason)

    def _check_stock_nonzero(self, rule: dict, ctx: PolicyContext) -> RuleResult:
        passed = ctx.available_stock > 0
        reason = None if passed else rule["reason_template"].format(sku=ctx.sku)
        return RuleResult(rule["id"], rule["category"], passed, Decision(rule["on_fail"]), reason)

    def _check_quote_not_expired(self, rule: dict, ctx: PolicyContext) -> RuleResult:
        if ctx.quote_id is None or ctx.quote_issued_at is None or ctx.quote_ttl_seconds is None:
            return RuleResult(rule["id"], rule["category"], False, Decision(rule["on_fail"]),
                               "No valid quote present for checkout")
        passed = (ctx.now - ctx.quote_issued_at) <= ctx.quote_ttl_seconds
        reason = None if passed else rule["reason_template"].format(
            quote_id=ctx.quote_id, issued_at=ctx.quote_issued_at, ttl_seconds=ctx.quote_ttl_seconds
        )
        return RuleResult(rule["id"], rule["category"], passed, Decision(rule["on_fail"]), reason)

    def _check_quote_matches_cart(self, rule: dict, ctx: PolicyContext) -> RuleResult:
        passed = ctx.quote_cart_hash == ctx.current_cart_hash
        reason = None if passed else rule["reason_template"].format(quote_id=ctx.quote_id)
        return RuleResult(rule["id"], rule["category"], passed, Decision(rule["on_fail"]), reason)

    def _check_transaction_total_under(self, rule: dict, ctx: PolicyContext) -> RuleResult:
        passed = ctx.transaction_total_inr <= rule["threshold_inr"]
        reason = None if passed else rule["reason_template"].format(
            total=ctx.transaction_total_inr, threshold_inr=rule["threshold_inr"]
        )
        return RuleResult(rule["id"], rule["category"], passed, Decision(rule["on_fail"]), reason)

    def _check_session_cumulative_under(self, rule: dict, ctx: PolicyContext) -> RuleResult:
        projected = ctx.session_cumulative_spend_inr + ctx.transaction_total_inr
        passed = projected <= rule["threshold_inr"]
        reason = None if passed else rule["reason_template"].format(
            session_total=projected, threshold_inr=rule["threshold_inr"]
        )
        return RuleResult(rule["id"], rule["category"], passed, Decision(rule["on_fail"]), reason)

    def _check_sku_has_flag(self, rule: dict, ctx: PolicyContext) -> RuleResult:
        flagged = rule["flag"] in ctx.stock_flags
        passed = not flagged
        reason = None if passed else rule["reason_template"].format(sku=ctx.sku)
        return RuleResult(rule["id"], rule["category"], passed, Decision(rule["on_fail"]), reason)

    def _check_checkout_attempts_under(self, rule: dict, ctx: PolicyContext) -> RuleResult:
        passed = ctx.session_checkout_attempts <= rule["max_attempts"]
        reason = None if passed else rule["reason_template"].format(
            max_attempts=rule["max_attempts"], window_seconds=rule["window_seconds"]
        )
        return RuleResult(rule["id"], rule["category"], passed, Decision(rule["on_fail"]), reason)

    def _check_verified_session_or_under(self, rule: dict, ctx: PolicyContext) -> RuleResult:
        passed = ctx.session_verified or ctx.transaction_total_inr <= rule["unverified_threshold_inr"]
        reason = None if passed else rule["reason_template"].format(
            total=ctx.transaction_total_inr, unverified_threshold_inr=rule["unverified_threshold_inr"]
        )
        return RuleResult(rule["id"], rule["category"], passed, Decision(rule["on_fail"]), reason)

    def _check_quote_not_consumed(self, rule: dict, ctx: PolicyContext) -> RuleResult:
        passed = not ctx.quote_consumed
        reason = None if passed else rule["reason_template"].format(quote_id=ctx.quote_id)
        return RuleResult(rule["id"], rule["category"], passed, Decision(rule["on_fail"]), reason)

    def _check_reservation_still_held(self, rule: dict, ctx: PolicyContext) -> RuleResult:
        passed = ctx.reservation_held
        reason = None if passed else rule["reason_template"].format(
            sku=ctx.sku, requested_qty=ctx.requested_qty
        )
        return RuleResult(rule["id"], rule["category"], passed, Decision(rule["on_fail"]), reason)

    def _check_qty_is_positive_integer(self, rule: dict, ctx: PolicyContext) -> RuleResult:
        passed = isinstance(ctx.requested_qty, int) and ctx.requested_qty > 0
        reason = None if passed else rule["reason_template"].format(
            requested_qty=ctx.requested_qty, sku=ctx.sku
        )
        return RuleResult(rule["id"], rule["category"], passed, Decision(rule["on_fail"]), reason)

    def _check_payment_amount_matches_quote(self, rule: dict, ctx: PolicyContext) -> RuleResult:
        # If no payment is being attempted yet, this check is vacuously fine.
        if ctx.payment_amount_inr is None or ctx.quote_total_inr is None:
            return RuleResult(rule["id"], rule["category"], True, Decision(rule["on_fail"]), None)
        passed = abs(ctx.payment_amount_inr - ctx.quote_total_inr) < 0.01
        reason = None if passed else rule["reason_template"].format(
            payment_amount=ctx.payment_amount_inr, quote_total=ctx.quote_total_inr, quote_id=ctx.quote_id
        )
        return RuleResult(rule["id"], rule["category"], passed, Decision(rule["on_fail"]), reason)

    def _check_approver_not_requester(self, rule: dict, ctx: PolicyContext) -> RuleResult:
        # Only relevant once an approval is actually in play.
        if ctx.approver_session_id is None:
            return RuleResult(rule["id"], rule["category"], True, Decision(rule["on_fail"]), None)
        passed = ctx.approver_session_id != ctx.requester_session_id
        reason = None if passed else rule["reason_template"].format(quote_id=ctx.quote_id)
        return RuleResult(rule["id"], rule["category"], passed, Decision(rule["on_fail"]), reason)

    def _check_approval_matches_snapshot(self, rule: dict, ctx: PolicyContext) -> RuleResult:
        if ctx.approval_snapshot_hash is None:
            return RuleResult(rule["id"], rule["category"], True, Decision(rule["on_fail"]), None)
        passed = ctx.approval_snapshot_hash == ctx.current_snapshot_hash
        reason = None if passed else rule["reason_template"].format(quote_id=ctx.quote_id)
        return RuleResult(rule["id"], rule["category"], passed, Decision(rule["on_fail"]), reason)


if __name__ == "__main__":
    # Quick smoke test using two scenarios from the mock inventory.
    engine = PolicyEngine(Path(__file__).parent / "rules.json")

    # Scenario 1: normal small purchase, should ALLOW
    ctx_allow = PolicyContext(
        sku="APP-TSHIRT-001", requested_qty=1, available_stock=150, stock_flags=[],
        quote_id="q_123", quote_issued_at=time.time() - 10, quote_ttl_seconds=300,
        quote_cart_hash="abc123", current_cart_hash="abc123",
        transaction_total_inr=599, session_cumulative_spend_inr=0,
        session_verified=True, session_checkout_attempts=1, session_window_seconds=600,
    )
    print("Scenario 1 (small purchase):", engine.evaluate(ctx_allow)["decision"])

    # Scenario 2: out-of-stock high-value item, should BLOCK
    ctx_block = PolicyContext(
        sku="ELEC-CAMERA-001", requested_qty=1, available_stock=0, stock_flags=["high_value"],
        quote_id="q_456", quote_issued_at=time.time() - 10, quote_ttl_seconds=300,
        quote_cart_hash="def456", current_cart_hash="def456",
        transaction_total_inr=54999, session_cumulative_spend_inr=0,
        session_verified=True, session_checkout_attempts=1, session_window_seconds=600,
    )
    result = engine.evaluate(ctx_block)
    print("Scenario 2 (out-of-stock high-value):", result["decision"])
    print("Failed rules:", result["failed_rules"])