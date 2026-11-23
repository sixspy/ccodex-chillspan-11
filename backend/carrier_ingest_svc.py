"""承运商批量导入空桩：单轨门闸 + 影子导入意图。

门闸纪要见 fixtures/carrier_gate_memo.md（当班封死）。改批只允许两路：

- 甲路：导入结果未过门闸 → 正式批次零改、拒写、桩记录零增。
- 乙路：过门闸 → 只写影子导入意图（待审核），正式批次零改；
  审核通过后在同一事务内晋升，否则意图拒收。

两路不得混用；同一承运商空桩改批须抢 `carrier-import:<承运商>` 导入锁。
空桩页、批次页、仓间页、线路页、对账页的审阅态均由本模块 `pending_review_map`
与 `gate_snapshot` 同源导出。
"""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import FIXTURES, connect, now_iso, write_audit
from locks import release
from models import (
    CarrierBatchChangeIn,
    CarrierIntentReviewIn,
    CarrierStubIn,
)
from validators import summarize_points, validate_temp_point

router = APIRouter(tags=["stubs"])

MEMO_PATH = FIXTURES / "carrier_gate_memo.md"
ALLOWED_CHANGE_FIELDS = {"cargo_ref", "temp_points"}
PROMOTABLE_STATUS = ("已入库", "超温处理中")


def acquire_import_lock(conn, carrier_name: str, holder: str) -> bool:
    """原子抢导入锁：并发下最多一名持有者，后到只记争用。

    `INSERT OR IGNORE` 由主键（lock_key）裁决，避免两个连接同时
    SELECT 到空再双双 INSERT 的窗口；抢不到即落争用日志并返回 False。
    """
    lock_key = _lock_key(carrier_name)
    now = now_iso()
    cur = conn.execute(
        """INSERT OR IGNORE INTO chamber_locks(lock_key,holder,acquired_at)
           VALUES (?,?,?)""",
        (lock_key, holder, now),
    )
    if cur.rowcount == 1:
        return True
    row = conn.execute(
        "SELECT holder FROM chamber_locks WHERE lock_key=?", (lock_key,)
    ).fetchone()
    if row and row["holder"] == holder:
        return True
    conn.execute(
        """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
           VALUES (?,?,?,?,?)""",
        (lock_key, holder, "争用", f"持有者={row['holder'] if row else '未知'}", now),
    )
    conn.commit()
    return False


def memo_meta() -> dict:
    """读取当班门闸纪要；缺失或未封死时一律按偏航处理。"""
    version = 0
    status = "未封死"
    if MEMO_PATH.exists():
        text = MEMO_PATH.read_text(encoding="utf-8")
        m = re.search(r"版本\s*(\d+)", text)
        if m:
            version = int(m.group(1))
        s = re.search(r"状态：\s*([^\s·]+)", text)
        if s:
            status = s.group(1).strip()
    return {
        "version": version,
        "status": status,
        "sealed": status == "当班封死",
        "source": "fixtures/carrier_gate_memo.md",
    }


def _lock_key(carrier_name: str) -> str:
    return f"carrier-import:{carrier_name}"


def gate_snapshot(conn) -> dict:
    pending = conn.execute(
        "SELECT COUNT(*) c FROM carrier_import_intents WHERE status='待审核'"
    ).fetchone()["c"]
    return {"pending_intents": int(pending), "memo": memo_meta()}


def pending_review_map(conn) -> dict[int, dict]:
    """批次审阅态唯一来源：待审核意图 → 页面上统一的 review_state。"""
    rows = conn.execute(
        """SELECT id,batch_id,status FROM carrier_import_intents
           WHERE status='待审核' ORDER BY id DESC"""
    ).fetchall()
    by_batch: dict[int, dict] = {}
    for r in rows:
        by_batch.setdefault(
            int(r["batch_id"]),
            {"review_state": "待审核", "pending_intent_id": int(r["id"])},
        )
    return by_batch


def review_fields(conn, batch_id: int) -> dict:
    """单批次审阅态，语义同 pending_review_map，供五页同源复用。"""
    return pending_review_map(conn).get(
        int(batch_id), {"review_state": "无待审", "pending_intent_id": None}
    )


def pending_intent_count(conn, chamber_id: int | None = None) -> int:
    if chamber_id is None:
        return int(
            conn.execute(
                "SELECT COUNT(*) c FROM carrier_import_intents WHERE status='待审核'"
            ).fetchone()["c"]
        )
    return int(
        conn.execute(
            """SELECT COUNT(*) c FROM carrier_import_intents i
               JOIN temp_batches b ON b.id=i.batch_id
               WHERE i.status='待审核' AND b.chamber_id=?""",
            (chamber_id,),
        ).fetchone()["c"]
    )


def _name(value: str) -> str:
    return value.strip()


def _deny(conn, user, action, target, payload, status, detail) -> None:
    write_audit(conn, user, action, target, payload)
    conn.commit()
    raise HTTPException(status, detail)


def _path_a(reason: str, **extra) -> dict:
    detail = {
        "path": "A",
        "reason": reason,
        "rejected": True,
        "batches_changed": 0,
        "batches_unchanged": True,
        "stub_records_added": 0,
    }
    detail.update(extra)
    return detail


def _check_points(ch, points) -> tuple[str, str]:
    if not isinstance(points, list):
        return "basetype", "温度点序列须为数组"
    for pt in points:
        try:
            value = float(pt.get("c", pt.get("temp")))
        except (AttributeError, TypeError, ValueError):
            return "basetype", "温度点格式非法"
        try:
            validate_temp_point(
                value, float(ch["target_min"]), float(ch["target_max"])
            )
        except HTTPException as exc:
            return "gate", str(exc.detail)
    return "", ""


def _proposal_problem(conn, batch, body: CarrierBatchChangeIn) -> tuple[str, str]:
    """门闸校验。返回 (reason, human)，空 reason 表示过闸。"""
    memo = memo_meta()
    if not memo["sealed"]:
        return "memo", "门闸纪要未封死，禁止写导入意图"
    forbidden = set(body.changes) - ALLOWED_CHANGE_FIELDS
    if forbidden:
        return "drift", f"偏离纪要：正式字段不可由导入改写 {sorted(forbidden)}"
    if "temp_points" in body.changes and not body.changes["temp_points"]:
        return "gate", "提议温度点不可为空"
    ch = conn.execute(
        "SELECT * FROM cold_chambers WHERE id=?", (batch["chamber_id"],)
    ).fetchone()
    if not ch:
        return "basetype", "批次所属仓间不存在"
    # 导入结果（import_rows）与拟回写序列（changes.temp_points）都须过闸。
    for points in (body.import_rows, body.changes.get("temp_points")):
        if points is None:
            continue
        reason, message = _check_points(ch, points)
        if reason:
            return reason, message
    return "", ""


def _proposal_drift(conn, batch, body) -> bool:
    """同一批次只允许一条待审核意图；后到者按争用处理，不落第二条。"""
    row = conn.execute(
        """SELECT id FROM carrier_import_intents
           WHERE batch_id=? AND status='待审核'""",
        (batch["id"],),
    ).fetchone()
    return row is not None


@router.get("/api/carrier-ingest/jobs")
def list_jobs(authorization: AuthHeader = None):
    require_writer(authorization)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM carrier_ingest_jobs ORDER BY id DESC LIMIT 100"
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


def _already_effective(batch, changes: dict) -> bool:
    """待审核意图的键若已出现在正式批，即“未审已改”的安全网信号。"""
    for key, value in changes.items():
        if key not in batch.keys():
            continue
        actual = batch[key]
        if key == "temp_points":
            try:
                actual = json.loads(actual or "[]")
            except (TypeError, ValueError):
                continue
        if actual == value:
            return True
    return False


@router.get("/api/carrier-ingest/state")
def carrier_state(authorization: AuthHeader = None):
    """空桩页数据源：审阅态与批次页等同源。"""
    require_user(authorization)
    with connect() as conn:
        review = pending_review_map(conn)
        rows = conn.execute(
            """SELECT id,batch_code,status,cargo_ref,generation FROM temp_batches
               ORDER BY id"""
        ).fetchall()
        batches = []
        for r in rows:
            d = dict(r)
            f = review.get(int(d["id"]), {})
            d["review_state"] = f.get("review_state", "无待审")
            d["pending_intent_id"] = f.get("pending_intent_id")
            batches.append(d)
        intents = conn.execute(
            "SELECT * FROM carrier_import_intents ORDER BY id DESC LIMIT 100"
        ).fetchall()
        unreviewed = 0
        for intent in intents:
            if intent["status"] != "待审核":
                continue
            batch = conn.execute(
                "SELECT * FROM temp_batches WHERE id=?", (intent["batch_id"],)
            ).fetchone()
            if not batch:
                continue
            changes = json.loads(intent["changes"] or "{}")
            if changes and _already_effective(batch, changes):
                unreviewed += 1
        return {
            "memo": memo_meta(),
            "batches": batches,
            "intents": [dict(i) for i in intents],
            "unreviewed_effective": unreviewed,
            **gate_snapshot(conn),
        }


@router.get("/api/carrier-ingest/intents")
def list_intents(authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        rows = conn.execute(
            """SELECT i.*, b.batch_code FROM carrier_import_intents i
               JOIN temp_batches b ON b.id=i.batch_id ORDER BY i.id DESC LIMIT 100"""
        ).fetchall()
        return {"items": [dict(r) for r in rows]}


@router.post("/api/carrier-ingest/batch-change")
def carrier_batch_change(body: CarrierBatchChangeIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    carrier = _name(body.carrier_name)
    lock_key = _lock_key(carrier)
    with connect() as conn:
        if not acquire_import_lock(conn, carrier, user):
            _deny(
                conn,
                user,
                "carrier_import_lock_contended",
                carrier,
                {"batch_id": body.batch_id, "lock_key": lock_key},
                409,
                {
                    "path": "lock",
                    "reason": "contention",
                    "batches_unchanged": True,
                    "batches_changed": 0,
                    "stub_records_added": 0,
                },
            )
        try:
            batch = conn.execute(
                "SELECT * FROM temp_batches WHERE id=?", (body.batch_id,)
            ).fetchone()
            if not batch:
                _deny(
                    conn, user, "carrier_import_rejected", carrier,
                    {"batch_id": body.batch_id},
                    404, _path_a("missing", message="批次不存在"),
                )
            reason, message = _proposal_problem(conn, batch, body)
            if reason:
                action = "导入偏航" if reason == "drift" else "carrier_import_rejected"
                _deny(
                    conn, user, action, carrier,
                    {"batch_id": body.batch_id, "reason": reason},
                    409, _path_a(reason, message=message),
                )
            if _proposal_drift(conn, batch, body):
                conn.execute(
                    """INSERT INTO contention_log(lock_key,actor,action,detail,created_at)
                       VALUES (?,?,?,?,?)""",
                    (
                        lock_key,
                        user,
                        "争用",
                        "同批次已有待审核意图",
                        now_iso(),
                    ),
                )
                _deny(
                    conn, user, "carrier_import_lock_contended", carrier,
                    {"batch_id": body.batch_id, "reason": "duplicate_pending"},
                    409,
                    {
                        "path": "lock",
                        "reason": "contention",
                        "reason_detail": "duplicate_pending",
                        "batches_unchanged": True,
                        "batches_changed": 0,
                        "stub_records_added": 0,
                        "message": "同批次已有待审核意图，后到只记争用",
                    },
                )
            now = now_iso()
            cur = conn.execute(
                """INSERT INTO carrier_import_intents(carrier_name,batch_id,row_count,
                   changes,import_rows,status,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    carrier,
                    body.batch_id,
                    body.row_count,
                    json.dumps(body.changes, ensure_ascii=False),
                    json.dumps(body.import_rows, ensure_ascii=False),
                    "待审核",
                    now,
                ),
            )
            write_audit(
                conn,
                user,
                "carrier_import_intent",
                carrier,
                {"intent_id": cur.lastrowid, "batch_id": body.batch_id},
            )
            conn.commit()
            return {
                "path": "B",
                "intent_id": cur.lastrowid,
                "intent_status": "待审核",
                "batch_changed": False,
                "batches_changed": 0,
                "stub_records_added": 0,
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            release(conn, lock_key, user)
            conn.commit()


def _promotion_problem(conn, batch, intent) -> str:
    if not memo_meta()["sealed"]:
        return "门闸纪要未封死"
    if batch["status"] not in PROMOTABLE_STATUS:
        return f"批次状态 {batch['status']} 不允许导入晋升"
    changes = json.loads(intent["changes"] or "{}")
    if set(changes) - ALLOWED_CHANGE_FIELDS:
        return "意图含正式字段改写"
    points = changes.get("temp_points")
    if points is not None:
        if not points:
            return "提议温度点为空，禁止晋升"
        ch = conn.execute(
            "SELECT target_min,target_max FROM cold_chambers WHERE id=?",
            (batch["chamber_id"],),
        ).fetchone()
        reason, message = _check_points(ch, points)
        if reason:
            return message
    return ""


@router.post("/api/carrier-ingest/intents/{intent_id}/review")
def review_intent(
    intent_id: int,
    body: CarrierIntentReviewIn,
    authorization: AuthHeader = None,
):
    user = require_writer(authorization)
    with connect() as conn:
        intent = conn.execute(
            "SELECT * FROM carrier_import_intents WHERE id=?", (intent_id,)
        ).fetchone()
        if not intent:
            raise HTTPException(404, "意图不存在")
        lock_key = _lock_key(intent["carrier_name"])
        if not acquire_import_lock(conn, intent["carrier_name"], user):
            _deny(
                conn, user, "carrier_import_lock_contended",
                intent["carrier_name"], {"intent_id": intent_id},
                409,
                {
                    "path": "lock",
                    "reason": "contention",
                    "promoted": False,
                    "batches_changed": 0,
                },
            )
        try:
            if body.memo_version is not None and body.memo_version != memo_meta()["version"]:
                _deny(
                    conn, user, "导入偏航", intent["carrier_name"],
                    {"intent_id": intent_id, "reason": "memo_version"},
                    409,
                    {
                        "path": "A",
                        "reason": "drift",
                        "promoted": False,
                        "batches_changed": 0,
                    },
                )
            if intent["status"] != "待审核":
                raise HTTPException(409, f"意图已 {intent['status']}，不可重复审核")
            now = now_iso()
            if not body.approve:
                conn.execute(
                    """UPDATE carrier_import_intents SET status='已拒收',
                       review_note=?, reviewed_at=? WHERE id=?""",
                    (body.review_note or "审核拒收", now, intent_id),
                )
                write_audit(
                    conn, user, "carrier_import_reject", intent["carrier_name"],
                    {"intent_id": intent_id},
                )
                conn.commit()
                return {
                    "intent_id": intent_id,
                    "promoted": False,
                    "intent_status": "已拒收",
                    "batches_changed": 0,
                }
            batch = conn.execute(
                "SELECT * FROM temp_batches WHERE id=?", (intent["batch_id"],)
            ).fetchone()
            problem = "批次不存在" if not batch else _promotion_problem(conn, batch, intent)
            if problem:
                conn.execute(
                    """UPDATE carrier_import_intents SET status='已拒收',
                       review_note=?, reviewed_at=? WHERE id=?""",
                    (problem, now, intent_id),
                )
                write_audit(
                    conn, user, "carrier_import_reject", intent["carrier_name"],
                    {"intent_id": intent_id, "reason": problem},
                )
                conn.commit()
                raise HTTPException(
                    409,
                    {
                        "path": "B",
                        "reason": "promotion_rejected",
                        "promoted": False,
                        "intent_status": "已拒收",
                        "batches_changed": 0,
                        "message": problem,
                    },
                )
            changes = json.loads(intent["changes"] or "{}")
            fields, values = [], []
            for key in ("cargo_ref", "temp_points"):
                if key in changes:
                    fields.append(key)
                    if key == "temp_points":
                        values.append(
                            json.dumps(changes[key], ensure_ascii=False)
                        )
                    else:
                        values.append(changes[key])
            if "temp_points" in changes:
                tmin, tmax = summarize_points(changes["temp_points"])
                fields.extend(["min_observed", "max_observed"])
                values.extend([tmin, tmax])
            if fields:
                assignments = ", ".join(f"{f}=?" for f in fields)
                conn.execute(
                    f"UPDATE temp_batches SET {assignments} WHERE id=?",
                    (*values, intent["batch_id"]),
                )
            conn.execute(
                """UPDATE carrier_import_intents SET status='已晋升',
                   review_note=?, reviewed_at=?, applied_at=? WHERE id=?""",
                (body.review_note or "审核通过", now, now, intent_id),
            )
            write_audit(
                conn, user, "carrier_import_promote", intent["carrier_name"],
                {"intent_id": intent_id, "batch_id": intent["batch_id"],
                 "fields": fields},
            )
            conn.commit()
            return {
                "intent_id": intent_id,
                "promoted": True,
                "intent_status": "已晋升",
                "batches_changed": 1 if fields else 0,
            }
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            release(conn, lock_key, user)
            conn.commit()


@router.post("/api/carrier-ingest-stub")
def stub_ingest(body: CarrierStubIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        before_batches = conn.execute(
            "SELECT COUNT(*) c FROM temp_batches"
        ).fetchone()["c"]
        cur = conn.execute(
            """INSERT INTO carrier_ingest_jobs(carrier_name,row_count,mode,status,detail,created_at)
               VALUES (?,?,?,?,?,?)""",
            (
                body.carrier_name,
                body.row_count,
                body.mode,
                "空桩登记",
                "未写入批次主表",
                now_iso(),
            ),
        )
        after_batches = conn.execute(
            "SELECT COUNT(*) c FROM temp_batches"
        ).fetchone()["c"]
        write_audit(
            conn,
            user,
            "carrier_ingest_stub",
            body.carrier_name,
            {"job_id": cur.lastrowid, "row_count": body.row_count},
        )
        conn.commit()
        return {
            "job_id": cur.lastrowid,
            "status": "空桩登记",
            "batches_unchanged": before_batches == after_batches,
        }
