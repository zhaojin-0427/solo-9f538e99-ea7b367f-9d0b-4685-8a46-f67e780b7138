"""Service / persistence operations with optimistic revision checks."""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone

from . import db
from .errors import ApiError, BadPayload, Conflict, NotFound, StaleRevision
from .instance import parse_instance
from .solver import check_facts
from .taxonomy import parse_package


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------- packages
def create_package(name: str, xsd: bytes, calc: bytes) -> dict:
    info = parse_package(xsd, calc)
    conn = db.get_conn()
    pid = _new_id("pkg")
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO packages(id, name, latest_rev, created_at, updated_at) "
            "VALUES (?,?,1,?,?)",
            (pid, name, now, now),
        )
        conn.execute(
            "INSERT INTO package_revisions(package_id, revision, xsd_raw, calc_raw, "
            "target_ns, prefix, concepts_json, equations_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, 1, xsd, calc, info["target_ns"], info["prefix"],
             json.dumps(info["concepts"]), json.dumps(info["equations"]), now),
        )
    return get_package(pid)


def revise_package(pid: str, expected_revision: int, xsd: bytes, calc: bytes) -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT latest_rev FROM packages WHERE id=?", (pid,)).fetchone()
    if row is None:
        raise NotFound(f"package '{pid}' not found")
    if row["latest_rev"] != expected_revision:
        raise StaleRevision(
            f"expected package revision {expected_revision}, server has {row['latest_rev']}"
        )
    info = parse_package(xsd, calc)
    new_rev = expected_revision + 1
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO package_revisions(package_id, revision, xsd_raw, calc_raw, "
            "target_ns, prefix, concepts_json, equations_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, new_rev, xsd, calc, info["target_ns"], info["prefix"],
             json.dumps(info["concepts"]), json.dumps(info["equations"]), now),
        )
        conn.execute(
            "UPDATE packages SET latest_rev=?, updated_at=? WHERE id=?",
            (new_rev, now, pid),
        )
    return get_package(pid)


def _package_revision_row(pid: str, revision: int) -> dict:
    row = db.get_conn().execute(
        "SELECT * FROM package_revisions WHERE package_id=? AND revision=?",
        (pid, revision),
    ).fetchone()
    if row is None:
        raise NotFound(f"package '{pid}' revision {revision} not found")
    return dict(row)


def get_package(pid: str) -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM packages WHERE id=?", (pid,)).fetchone()
    if row is None:
        raise NotFound(f"package '{pid}' not found")
    rev = _package_revision_row(pid, row["latest_rev"])
    return {
        "package_id": row["id"],
        "name": row["name"],
        "latest_revision": row["latest_rev"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "latest": {
            "package_id": pid,
            "revision": rev["revision"],
            "target_ns": rev["target_ns"],
            "prefix": rev["prefix"],
            "concepts": json.loads(rev["concepts_json"]),
            "equations": json.loads(rev["equations_json"]),
            "created_at": rev["created_at"],
        },
    }


def list_packages() -> list[dict]:
    rows = db.get_conn().execute("SELECT id FROM packages ORDER BY created_at").fetchall()
    return [get_package(r["id"]) for r in rows]


# ---------------------------------------------------------------- instances
def create_instance(pid: str, raw: bytes) -> dict:
    conn = db.get_conn()
    if conn.execute("SELECT 1 FROM packages WHERE id=?", (pid,)).fetchone() is None:
        raise NotFound(f"package '{pid}' not found")
    pkg = get_package(pid)
    info = parse_instance(raw, pkg["latest"])
    iid = _new_id("inst")
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO instances(id, package_id, latest_rev, created_at, updated_at) "
            "VALUES (?,?,1,?,?)",
            (iid, pid, now, now),
        )
        conn.execute(
            "INSERT INTO instance_revisions(instance_id, revision, instance_raw, entity, "
            "period, currency, facts_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (iid, 1, raw, info["entity"], info["period"], info["currency"],
             json.dumps(info["facts"]), now),
        )
    return get_instance(iid)


def revise_instance(iid: str, expected_package_revision: int, raw: bytes) -> dict:
    conn = db.get_conn()
    row = conn.execute(
        "SELECT package_id, latest_rev FROM instances WHERE id=?", (iid,)
    ).fetchone()
    if row is None:
        raise NotFound(f"instance '{iid}' not found")
    pkg = get_package(row["package_id"])
    # 受理时校验两侧所见修订：实例提交必须针对当前 package 修订。
    if pkg["latest_revision"] != expected_package_revision:
        raise StaleRevision(
            f"expected package revision {expected_package_revision}, "
            f"server has {pkg['latest_revision']}"
        )
    info = parse_instance(raw, pkg["latest"])
    new_rev = row["latest_rev"] + 1
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO instance_revisions(instance_id, revision, instance_raw, entity, "
            "period, currency, facts_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (iid, new_rev, raw, info["entity"], info["period"], info["currency"],
             json.dumps(info["facts"]), now),
        )
        conn.execute(
            "UPDATE instances SET latest_rev=?, updated_at=? WHERE id=?",
            (new_rev, now, iid),
        )
    return get_instance(iid)


def _instance_revision_row(iid: str, revision: int) -> dict:
    row = db.get_conn().execute(
        "SELECT * FROM instance_revisions WHERE instance_id=? AND revision=?",
        (iid, revision),
    ).fetchone()
    if row is None:
        raise NotFound(f"instance '{iid}' revision {revision} not found")
    return dict(row)


def get_instance(iid: str) -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    if row is None:
        raise NotFound(f"instance '{iid}' not found")
    rev = _instance_revision_row(iid, row["latest_rev"])
    return {
        "instance_id": row["id"],
        "package_id": row["package_id"],
        "latest_revision": row["latest_rev"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "latest": {
            "instance_id": iid,
            "package_id": row["package_id"],
            "revision": rev["revision"],
            "entity": rev["entity"],
            "period": rev["period"],
            "currency": rev["currency"],
            "facts": json.loads(rev["facts_json"]),
            "created_at": rev["created_at"],
        },
    }


def list_instances(pid: str | None = None) -> list[dict]:
    conn = db.get_conn()
    if pid:
        rows = conn.execute(
            "SELECT id FROM instances WHERE package_id=? ORDER BY created_at", (pid,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT id FROM instances ORDER BY created_at").fetchall()
    return [get_instance(r["id"]) for r in rows]


# ---------------------------------------------------------------- reports
def _report_out(row: dict) -> dict:
    return {
        "report_id": row["id"],
        "instance_id": row["instance_id"],
        "package_id": row["package_id"],
        "instance_revision": row["instance_revision"],
        "package_revision": row["package_revision"],
        "status": row["status"],
        "is_correction": bool(row["is_correction"]),
        "superseded": bool(row["superseded"]),
        "late": bool(row["late"]),
        "witness": json.loads(row["witness_json"]) if row["witness_json"] else None,
        "conflicts": json.loads(row["conflicts_json"]) if row["conflicts_json"] else None,
        "missing_facts": json.loads(row["missing_json"]) if row["missing_json"] else None,
        "detail": json.loads(row["detail_json"]),
        "created_at": row["created_at"],
        "has_instance_snapshot": bool(row["instance_snapshot"]),
        "has_xsd_snapshot": bool(row["xsd_snapshot"]),
        "has_calc_snapshot": bool(row["calc_snapshot"]),
    }


def run_check(iid: str, expected_instance_revision: int,
              expected_package_revision: int, late: bool = False) -> dict:
    conn = db.get_conn()
    inst_row = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    if inst_row is None:
        raise NotFound(f"instance '{iid}' not found")

    pkg = get_package(inst_row["package_id"])

    # 受理时校验两侧所见修订：
    # - 客户端所见修订领先于服务器 → 过期拒绝（412）
    # - 落后于服务器：默认仍拒绝；仅当显式声明为迟到补报时受理，仅存历史。
    if expected_instance_revision > inst_row["latest_rev"]:
        raise StaleRevision(
            f"expected instance revision {expected_instance_revision} is ahead of "
            f"server latest {inst_row['latest_rev']}"
        )
    if expected_package_revision > pkg["latest_revision"]:
        raise StaleRevision(
            f"expected package revision {expected_package_revision} is ahead of "
            f"server latest {pkg['latest_revision']}"
        )
    behind = (expected_instance_revision < inst_row["latest_rev"]
              or expected_package_revision < pkg["latest_revision"])
    if behind and not late:
        raise StaleRevision(
            f"revisions ({expected_instance_revision},"
            f"{expected_package_revision}) are behind server latest "
            f"({inst_row['latest_rev']},{pkg['latest_revision']}); "
            f"re-check with late=true to file a late historical report"
        )

    i_rev = _instance_revision_row(iid, expected_instance_revision)
    p_rev = _package_revision_row(inst_row["package_id"], expected_package_revision)

    # 迟到报告：依据的修订已落后于两侧最新组合，仅存历史，不取代当前报告。
    is_late = behind
    facts = json.loads(i_rev["facts_json"])
    equations = json.loads(p_rev["equations_json"])

    result = check_facts(facts, equations)

    now = _now()
    report_id = _new_id("rpt")
    prior = conn.execute(
        "SELECT id FROM reports WHERE instance_id=? ORDER BY created_at, rowid", (iid,)
    ).fetchall()
    is_correction = len(prior) > 0

    detail = {
        "entity": i_rev["entity"],
        "period": i_rev["period"],
        "currency": i_rev["currency"],
        "fact_count": len(facts),
        "equation_count": len(equations),
        "solver": "scipy.optimize.linprog(highs)",
    }
    payload = {
        "id": report_id,
        "instance_id": iid,
        "package_id": inst_row["package_id"],
        "instance_revision": i_rev["revision"],
        "package_revision": p_rev["revision"],
        "status": result["status"],
        "is_correction": 1 if is_correction else 0,
        "late": 1 if is_late else 0,
        "witness_json": json.dumps(result["witness"]) if "witness" in result else None,
        "conflicts_json": json.dumps(result["conflicts"]) if "conflicts" in result else None,
        "missing_json": json.dumps(result["missing_facts"]) if "missing_facts" in result else None,
        "detail_json": json.dumps(detail),
        "instance_snapshot": i_rev["instance_raw"],
        "xsd_snapshot": p_rev["xsd_raw"],
        "calc_snapshot": p_rev["calc_raw"],
        "created_at": now,
    }

    superseded_ids: list[str] = []
    with conn:
        conn.execute(
            "INSERT INTO reports(id, instance_id, package_id, instance_revision, "
            "package_revision, status, is_correction, superseded, late, witness_json, "
            "conflicts_json, missing_json, detail_json, instance_snapshot, xsd_snapshot, "
            "calc_snapshot, created_at) VALUES (:id,:instance_id,:package_id,"
            ":instance_revision,:package_revision,:status,:is_correction,0,:late,"
            ":witness_json,:conflicts_json,:missing_json,:detail_json,:instance_snapshot,"
            ":xsd_snapshot,:calc_snapshot,:created_at)",
            payload,
        )
        if not is_late:
            old = conn.execute(
                "SELECT id FROM reports WHERE instance_id=? AND id!=? AND superseded=0",
                (iid, report_id),
            ).fetchall()
            superseded_ids = [r["id"] for r in old]
            conn.execute(
                "UPDATE reports SET superseded=1 WHERE instance_id=? AND id!=?",
                (iid, report_id),
            )

    out = _report_out(dict(conn.execute(
        "SELECT * FROM reports WHERE id=?", (report_id,)
    ).fetchone()))
    return {"report": out, "superseded_report_ids": superseded_ids}


def get_report(rid: str) -> dict:
    row = db.get_conn().execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise NotFound(f"report '{rid}' not found")
    return _report_out(dict(row))


def get_report_snapshot(rid: str, kind: str) -> tuple[bytes, str]:
    row = db.get_conn().execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise NotFound(f"report '{rid}' not found")
    column = {"instance": "instance_snapshot", "xsd": "xsd_snapshot",
              "calc": "calc_snapshot"}[kind]
    media = {"instance": "application/xml", "xsd": "application/xml",
             "calc": "application/xml"}[kind]
    return bytes(row[column]), media


def list_reports(iid: str) -> dict:
    conn = db.get_conn()
    if conn.execute("SELECT 1 FROM instances WHERE id=?", (iid,)).fetchone() is None:
        raise NotFound(f"instance '{iid}' not found")
    rows = conn.execute(
        "SELECT * FROM reports WHERE instance_id=? ORDER BY created_at, rowid", (iid,)
    ).fetchall()
    history = [_report_out(dict(r)) for r in rows]
    # 迟到报告仅存历史，永远不作为当前报告。
    current = next(
        (r for r in reversed(history) if not r["superseded"] and not r["late"]), None
    )
    return {"instance_id": iid, "current": current, "history": history}


# ---------------------------------------------------------------- quarantine intakes
# One revalidation at a time per intake. Concurrent or duplicate revalidations
# collapse into the single in-flight run, so at most one instance is ever
# created for an intake.
_intake_locks_guard = threading.Lock()
_intake_locks: dict[str, threading.Lock] = {}


def _intake_lock(intake_id: str) -> threading.Lock:
    with _intake_locks_guard:
        lock = _intake_locks.get(intake_id)
        if lock is None:
            lock = threading.Lock()
            _intake_locks[intake_id] = lock
        return lock


def _before_intake_commit() -> None:
    """Hook invoked inside the publication transaction right before COMMIT.

    Production code performs no extra work here; tests replace it to prove
    that any failure inside the transaction rolls the whole publication back.
    """


def _intake_row(intake_id: str) -> dict:
    row = db.get_conn().execute(
        "SELECT * FROM intakes WHERE id=?", (intake_id,)
    ).fetchone()
    if row is None:
        raise NotFound(f"intake '{intake_id}' not found")
    return dict(row)


def _intake_out(row: dict) -> dict:
    return {
        "intake_id": row["id"],
        "package_id": row["package_id"],
        "package_revision": row["package_revision"],
        "status": row["status"],
        "entity": row["entity"],
        "period": row["period"],
        "currency": row["currency"],
        "facts": json.loads(row["facts_json"]),
        "unknown_concepts": json.loads(row["unknown_concepts_json"]),
        "last_revalidation": (
            json.loads(row["last_revalidate_json"]) if row["last_revalidate_json"] else None
        ),
        "instance_id": row["instance_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_intake(pid: str, raw: bytes) -> dict:
    """Quarantine an upload that fails *only* because of undeclared concepts.

    All other structural rules are enforced exactly as for the normal intake
    and rejected with the usual 422. A filing that the current package already
    accepts has nothing to wait for and is rejected too.
    """
    conn = db.get_conn()
    if conn.execute("SELECT 1 FROM packages WHERE id=?", (pid,)).fetchone() is None:
        raise NotFound(f"package '{pid}' not found")
    pkg = get_package(pid)
    info = parse_instance(raw, pkg["latest"], allow_unknown_concepts=True)
    if not info["unknown_concepts"]:
        raise BadPayload(
            "instance declares no concept missing from the current package XSD; "
            "submit it through the normal instance endpoint"
        )

    intake_id = _new_id("intake")
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO intakes(id, package_id, package_revision, status, instance_raw, "
            "entity, period, currency, facts_json, unknown_concepts_json, "
            "last_revalidate_json, instance_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?)",
            (intake_id, pid, pkg["latest_revision"], "pending", raw,
             info["entity"], info["period"], info["currency"],
             json.dumps(info["facts"]), json.dumps(info["unknown_concepts"]),
             now, now),
        )
    return _intake_out(_intake_row(intake_id))


def get_intake(intake_id: str) -> dict:
    return _intake_out(_intake_row(intake_id))


def list_intakes(pid: str | None = None, status: str | None = None) -> list[dict]:
    conn = db.get_conn()
    query = "SELECT id FROM intakes"
    clauses: list[str] = []
    params: list = []
    if pid is not None:
        clauses.append("package_id=?")
        params.append(pid)
    if status is not None:
        clauses.append("status=?")
        params.append(status)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at, rowid"
    rows = conn.execute(query, params).fetchall()
    return [_intake_out(_intake_row(r["id"])) for r in rows]


def get_intake_raw(intake_id: str) -> tuple[bytes, str]:
    row = _intake_row(intake_id)
    return bytes(row["instance_raw"]), "application/xml"


def revalidate_intake(intake_id: str, expected_package_revision: int) -> dict:
    """Re-validate the stored original bytes against the current package.

    The client names the package revision it has seen; a mismatch means the
    package moved under it and the publication is refused (412). On success
    the exact original bytes become a normal instance and the intake points
    at it. On failure the bytes and the reason stay in quarantine. The whole
    publication is one transaction: no half-created instance or accepted
    intake can survive a failure.
    """
    conn = db.get_conn()
    lock = _intake_lock(intake_id)
    if not lock.acquire(blocking=False):
        raise Conflict(
            f"a revalidation of intake '{intake_id}' is already in progress",
            code="revalidation_in_progress",
        )
    try:
        row = _intake_row(intake_id)
        if row["status"] == "accepted":
            # Idempotent: a repeated revalidation never creates a second instance.
            return {"intake": _intake_out(row),
                    "instance": get_instance(row["instance_id"])}

        pkg = get_package(row["package_id"])
        if pkg["latest_revision"] != expected_package_revision:
            raise StaleRevision(
                f"expected package revision {expected_package_revision}, "
                f"server has {pkg['latest_revision']}"
            )
        # Re-check inside the write transaction so a package revision committed
        # concurrently cannot slip through.
        now = _now()
        try:
            info = parse_instance(row["instance_raw"], pkg["latest"])
        except ApiError as exc:
            record = {
                "attempted_at": now,
                "package_revision": pkg["latest_revision"],
                "result": "failed",
                "error_code": exc.code,
                "message": exc.message,
                "instance_id": None,
            }
            with conn:
                conn.execute(
                    "UPDATE intakes SET last_revalidate_json=?, updated_at=? WHERE id=? "
                    "AND status='pending'",
                    (json.dumps(record), now, intake_id),
                )
            return {"intake": get_intake(intake_id), "instance": None}

        iid = _new_id("inst")
        record = {
            "attempted_at": now,
            "package_revision": pkg["latest_revision"],
            "result": "accepted",
            "error_code": None,
            "message": None,
            "instance_id": iid,
        }
        # One atomic publication transaction. BEGIN IMMEDIATE takes the write
        # lock up front; any error rolls back instance rows, intake link and
        # accepted state together — no half-created instance can survive.
        conn.execute("BEGIN IMMEDIATE")
        try:
            latest = conn.execute(
                "SELECT latest_rev FROM packages WHERE id=?", (row["package_id"],)
            ).fetchone()
            if latest is None or latest["latest_rev"] != expected_package_revision:
                raise StaleRevision(
                    f"package changed during revalidation; expected "
                    f"{expected_package_revision}, server has "
                    f"{latest['latest_rev'] if latest is not None else 'none'}"
                )
            existing = conn.execute(
                "SELECT instance_id FROM intakes WHERE id=? AND status='accepted'",
                (intake_id,),
            ).fetchone()
            if existing is not None:  # lost a race despite the process lock
                conn.execute("COMMIT")
                return {"intake": get_intake(intake_id),
                        "instance": get_instance(existing["instance_id"])}
            conn.execute(
                "INSERT INTO instances(id, package_id, latest_rev, created_at, updated_at) "
                "VALUES (?,?,1,?,?)",
                (iid, row["package_id"], now, now),
            )
            conn.execute(
                "INSERT INTO instance_revisions(instance_id, revision, instance_raw, "
                "entity, period, currency, facts_json, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (iid, 1, row["instance_raw"], info["entity"], info["period"],
                 info["currency"], json.dumps(info["facts"]), now),
            )
            conn.execute(
                "UPDATE intakes SET status='accepted', instance_id=?, "
                "last_revalidate_json=?, updated_at=? WHERE id=? AND status='pending'",
                (iid, json.dumps(record), now, intake_id),
            )
            updated = conn.execute("SELECT changes() AS n").fetchone()["n"]
            if updated < 1:  # pragma: no cover - defensive; process lock should prevent
                raise Conflict("intake state changed during revalidation")
            # Verify inside the transaction that the formal instance stores
            # the exact original intake bytes; a mismatch rolls everything back.
            stored = conn.execute(
                "SELECT instance_raw FROM instance_revisions "
                "WHERE instance_id=? AND revision=1", (iid,)
            ).fetchone()["instance_raw"]
            if bytes(stored) != bytes(row["instance_raw"]):
                raise Conflict("published instance bytes differ from intake original")
            _before_intake_commit()
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        return {"intake": get_intake(intake_id), "instance": get_instance(iid)}
    finally:
        lock.release()
