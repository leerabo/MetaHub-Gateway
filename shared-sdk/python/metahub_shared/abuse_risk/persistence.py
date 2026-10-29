"""Asynchronous persistence sink for privacy-safe risk counter rebuild facts."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from metahub_shared.db.connection import connect_database
from metahub_shared.runtime.config import AppConfig

from .runtime import CounterFact, DegradedFact, RiskFact


def persistence_sink(config: AppConfig):
    async def sink(facts: tuple[RiskFact, ...]) -> None:
        await asyncio.to_thread(_persist, config, facts)

    return sink


def _persist(config: AppConfig, facts: tuple[RiskFact, ...]) -> None:
    if not facts:
        return
    connection = connect_database(config.database_url)
    try:
        connection.execute("BEGIN IMMEDIATE")
        available = connection.execute(
            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='abuse_counters'"
        ).fetchone()
        if available is None:
            connection.rollback()
            return
        degraded = {
            "privilege_fail_closed": sum(
                isinstance(fact, DegradedFact) and fact.kind == "privilege_fail_closed"
                for fact in facts
            ),
            "gateway_fail_open": sum(
                isinstance(fact, DegradedFact) and fact.kind == "gateway_fail_open"
                for fact in facts
            ),
        }
        if degraded["privilege_fail_closed"] or degraded["gateway_fail_open"]:
            head = connection.execute("SELECT * FROM risk_degraded_counters").fetchone()
            if head is not None:
                changed = max(
                    datetime.now(UTC),
                    _time(head["updated_at"]) + timedelta(milliseconds=1),
                )
                connection.execute(
                    """UPDATE risk_degraded_counters
                       SET privilege_fail_closed_count=privilege_fail_closed_count+?,
                           gateway_fail_open_count=gateway_fail_open_count+?,
                           revision=revision+1,updated_at=?
                       WHERE risk_degraded_counter_id=?""",
                    (
                        degraded["privilege_fail_closed"],
                        degraded["gateway_fail_open"],
                        _db_time(changed),
                        head["risk_degraded_counter_id"],
                    ),
                )
        for fact in facts:
            if not isinstance(fact, CounterFact):
                continue
            stamp = _db_time(fact.window_started_at)
            row = connection.execute(
                """SELECT * FROM abuse_counters
                   WHERE counter_domain=? AND subject_kind=? AND subject_hmac=?
                     AND window_started_at=?""",
                (fact.action, fact.subject_kind, fact.subject_hmac, stamp),
            ).fetchone()
            now = datetime.now(UTC)
            if row is None:
                counter_id = "abc_" + uuid4().hex
                connection.execute(
                    """INSERT INTO abuse_counters(
                         abuse_counter_id,counter_domain,subject_kind,subject_hmac,
                         window_started_at,window_seconds,current_count,expires_at,
                         revision,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,1,?,?)""",
                    (
                        counter_id,
                        fact.action,
                        fact.subject_kind,
                        fact.subject_hmac,
                        stamp,
                        fact.window_seconds,
                        fact.count_after,
                        _db_time(
                            fact.window_started_at + timedelta(seconds=fact.window_seconds + 5)
                        ),
                        _db_time(now),
                        _db_time(now),
                    ),
                )
            else:
                counter_id = str(row["abuse_counter_id"])
                if fact.count_after > int(row["current_count"]):
                    changed = max(now, _time(row["updated_at"]) + timedelta(milliseconds=1))
                    connection.execute(
                        """UPDATE abuse_counters SET current_count=?,revision=revision+1,
                             updated_at=? WHERE abuse_counter_id=?""",
                        (fact.count_after, _db_time(changed), counter_id),
                    )
            connection.execute(
                """INSERT INTO abuse_counter_facts(
                     abuse_counter_fact_id,abuse_counter_id,count_after,source_code,
                     request_id,trace_id,occurred_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING""",
                (
                    "abf_" + uuid4().hex,
                    counter_id,
                    fact.count_after,
                    fact.source_code,
                    fact.request_id,
                    fact.trace_id,
                    _db_time(now),
                ),
            )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _time(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _db_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


__all__ = ["persistence_sink"]
