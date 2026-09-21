"""Push records to the destination, and be honest about what happened.

Three failure classes, because they need three different responses:

* transient — retry with backoff; the destination was briefly unwell.
* permanent — do NOT retry. The destination rejected the content; sending the same
  bytes again produces the same rejection and burns the consultant's time.
* uncertain — the request left but the outcome is unknown. Retrying blind risks a
  duplicate, so reconcile by identity first and only send if the record is absent.

Delivery identity is (run_id, natural_key, generation). Schema version deliberately
stays out of it: with it in the key, a failed delivery followed by a schema edit
produces a different key, and a first attempt that was uncertain-but-succeeded then
double-delivers.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

MAX_ATTEMPTS = 3          # first attempt plus two retries
BACKOFF_SECONDS = (0.5, 1.5)


class Outcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"          # permanent
    TRANSIENT_FAILED = "transient_failed"
    UNCERTAIN = "uncertain"
    CONFLICT = "conflict"


@dataclass
class Attempt:
    outcome: Outcome
    attempt: int
    status_code: int | None = None
    target_record_id: str | None = None
    response: Any = None
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome in (Outcome.ACCEPTED, Outcome.DUPLICATE)

    @property
    def retryable(self) -> bool:
        return self.outcome in (Outcome.TRANSIENT_FAILED, Outcome.UNCERTAIN)


def idempotency_key(run_id: str, natural_key: str, generation: int) -> str:
    return f"{run_id}|{natural_key}|gen{generation}"


class DestinationClient:
    def __init__(
        self, base_url: str, timeout: float = 5.0, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Tests route to the destination app in-process; nothing else changes, so the
        # integration test exercises the real client including its retry logic.
        self.transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url, timeout=self.timeout, transport=self.transport
        )

    def register_schema(self, spec: dict[str, Any]) -> None:
        with self._client() as client:
            client.post("/schemas", json=spec).raise_for_status()

    def set_failure_mode(
        self, mode: str, remaining: int, *, run_id: str | None = None
    ) -> dict[str, Any]:
        """Make the destination misbehave on purpose, for one run's deliveries.

        The retry and rollback paths are the half of the delivery story that only
        shows itself when something goes wrong, and a destination that always says
        yes can never demonstrate them.
        """
        with self._client() as client:
            response = client.post(
                "/admin/failure-mode",
                json={"mode": mode, "remaining": remaining, "run_id": run_id},
            )
            response.raise_for_status()
            return response.json()

    def reconcile(self, run_id: str, natural_key: str) -> dict[str, Any] | None:
        """Ask the destination whether it already holds this record.

        The only safe way out of an uncertain outcome. Without it the choice is
        between risking a duplicate and abandoning a record that may be fine.
        """
        try:
            with self._client() as client:
                response = client.get(
                    "/records", params={"run_id": run_id, "natural_key": natural_key}
                )
                response.raise_for_status()
                rows = response.json()
                return rows[0] if rows else None
        except httpx.HTTPError:
            return None

    def _send(
        self, run_id: str, natural_key: str, generation: int, schema_version: int,
        payload: dict[str, Any], attempt: int,
    ) -> Attempt:
        body = {
            "run_id": run_id, "natural_key": natural_key,
            "schema_version": schema_version, "generation": generation,
            "payload": payload,
        }
        headers = {"Idempotency-Key": idempotency_key(run_id, natural_key, generation)}
        try:
            with self._client() as client:
                response = client.post("/records", json=body, headers=headers)
        except httpx.TimeoutException as exc:
            # Timed out: the write may or may not have landed.
            return Attempt(Outcome.UNCERTAIN, attempt, error=str(exc))
        except httpx.HTTPError as exc:
            return Attempt(Outcome.TRANSIENT_FAILED, attempt, error=str(exc))

        if response.status_code == 409:
            return Attempt(Outcome.CONFLICT, attempt, status_code=409,
                           response=response.json())
        if response.status_code >= 500:
            outcome = Outcome.UNCERTAIN if response.status_code == 500 else Outcome.TRANSIENT_FAILED
            return Attempt(outcome, attempt, status_code=response.status_code,
                           response=_safe_json(response))
        if response.status_code >= 400:
            return Attempt(Outcome.TRANSIENT_FAILED, attempt, status_code=response.status_code,
                           response=_safe_json(response))

        data = response.json()
        outcome = {
            "accepted": Outcome.ACCEPTED,
            "duplicate": Outcome.DUPLICATE,
            "rejected": Outcome.REJECTED,
        }.get(data.get("outcome"), Outcome.TRANSIENT_FAILED)
        return Attempt(outcome, attempt, status_code=response.status_code,
                       target_record_id=data.get("record_id"), response=data)

    def deliver(
        self, run_id: str, natural_key: str, payload: dict[str, Any], *,
        schema_version: int = 1, generation: int = 1,
    ) -> list[Attempt]:
        """Deliver one record, returning every attempt made."""
        attempts: list[Attempt] = []
        for n in range(1, MAX_ATTEMPTS + 1):
            if n > 1:
                # An uncertain outcome must be reconciled before resending, or a retry
                # turns a possible success into a certain duplicate.
                if attempts[-1].outcome is Outcome.UNCERTAIN:
                    existing = self.reconcile(run_id, natural_key)
                    if existing:
                        attempts.append(Attempt(
                            Outcome.DUPLICATE, n, status_code=200,
                            target_record_id=existing["id"],
                            response={"reconciled": True, "note":
                                      "destination already held this record"},
                        ))
                        return attempts
                time.sleep(BACKOFF_SECONDS[min(n - 2, len(BACKOFF_SECONDS) - 1)])

            attempt = self._send(run_id, natural_key, generation, schema_version, payload, n)
            attempts.append(attempt)
            if attempt.succeeded or not attempt.retryable:
                return attempts
        return attempts

    def rollback(self, target_record_id: str) -> tuple[bool, Any]:
        try:
            with self._client() as client:
                response = client.post(f"/records/{target_record_id}/rollback")
                response.raise_for_status()
                return True, response.json()
        except httpx.HTTPError as exc:
            return False, {"error": str(exc)}


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"body": response.text[:200]}
