"""承运商导入改批门闸 — 三红夹具（先红后绿）。

失败测甲（test_a）：门闸未过却写正式批 —— 盯“未审已改正式批”事故。
失败测乙（test_b）：影子意图未晋升却生效 —— 盯“意图未审先落地”。
失败测丙（test_c）：双人同桩抢改 —— 盯“同一承运商空桩并发改批”。

夹具只用本仓文件库（fixtures/*.json）播种的正式批次，不新增外部依赖。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import app
from db import connect, init_db

INCIDENT = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "carrier_incident.json")
    .read_text(encoding="utf-8")
)


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _batch_row(batch_id: int = 1) -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM temp_batches WHERE id=?", (batch_id,)
        ).fetchone()
        return dict(row) if row else {}


def _count(table: str) -> int:
    with connect() as conn:
        return conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]


def _payload(**over) -> dict:
    body = {
        "carrier_name": "顺冷物流",
        "batch_id": 1,
        "row_count": 3,
        "import_rows": [{"t": "08:00", "c": -19.1}],
        "changes": {"cargo_ref": "承运商回写货载"},
    }
    body.update(over)
    return body


def test_a_gate_fail_zero_official_change_and_zero_stub_record():
    """失败测甲：导入结果未过门闸，正式批次零改、拒写、桩记录零增。

    夹具 fixtures/carrier_incident.json 复现“空桩改到正式批”的假事故：
    承运商回写 99℃ 越量程，期望甲路径拒写并冻结全部正式字段。
    """
    c = TestClient(app)
    h = _auth(c)
    batch_id = 1
    before_row = _batch_row(batch_id)
    before_batches = _count("temp_batches")
    before_jobs = _count("carrier_ingest_jobs")
    before_intents = _count("carrier_import_intents")

    # 90℃/99℃ 明显越量程：门闸应当拒绝，绝不允许直接落到正式批。
    r = c.post(
        "/api/carrier-ingest/batch-change",
        headers=h,
        json={
            "carrier_name": INCIDENT["carrier_name"],
            "batch_id": batch_id,
            "row_count": INCIDENT["row_count"],
            "import_rows": INCIDENT["incident_import_rows"],
            "changes": INCIDENT["incident_changes"],
        },
    )
    assert r.status_code == INCIDENT["expected"]["status"], r.text
    detail = r.json()["detail"]
    assert detail["path"] == INCIDENT["expected"]["path"]
    assert detail["rejected"] is True
    assert detail["batches_changed"] == INCIDENT["expected"]["batches_changed"]
    assert (
        detail["stub_records_added"]
        == INCIDENT["expected"]["stub_records_added"]
    )

    # 正式批次表零改（逐字段比对夹具列出的冻结字段，不只是数量）
    after_row = _batch_row(batch_id)
    assert after_row == before_row
    for field in INCIDENT["expected"]["official_fields_frozen"]:
        assert after_row[field] == before_row[field]
    assert _count("temp_batches") == before_batches
    # 桩记录零增、影子意图零增
    assert _count("carrier_ingest_jobs") == before_jobs
    assert _count("carrier_import_intents") == before_intents


def test_b_shadow_intent_not_effective_until_promoted():
    """失败测乙：影子意图未晋升不得生效；晋升与拒收均须单事务落定。"""
    c = TestClient(app)
    h = _auth(c)
    before_row = _batch_row(1)

    r = c.post("/api/carrier-ingest/batch-change", headers=h, json=_payload())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["path"] == "B"
    assert data["intent_status"] == "待审核"
    assert data["batch_changed"] is False
    assert data["batches_changed"] == 0

    # 未晋升：正式批字段一点不动
    assert _batch_row(1) == before_row

    state = c.get("/api/carrier-ingest/state", headers=h).json()
    state_row = next(b for b in state["batches"] if b["id"] == 1)
    assert state_row["review_state"] == "待审核"
    assert state_row["cargo_ref"] == before_row["cargo_ref"]
    assert state["unreviewed_effective"] == 0

    # 批次页与空桩页同源：正式字段一致
    batch_page = c.get("/api/batches", headers=h).json()["items"]
    page_row = next(b for b in batch_page if b["id"] == 1)
    assert page_row["status"] == state_row["status"]
    assert page_row["cargo_ref"] == state_row["cargo_ref"]

    # 同事务晋升后才可生效
    intent_id = data["intent_id"]
    rr = c.post(
        f"/api/carrier-ingest/intents/{intent_id}/review",
        headers=h,
        json={"approve": True, "memo_version": state["memo"]["version"]},
    )
    assert rr.status_code == 200, rr.text
    assert rr.json()["promoted"] is True
    after_row = _batch_row(1)
    assert after_row["cargo_ref"] == "承运商回写货载"

    intents = c.get("/api/carrier-ingest/intents", headers=h).json()["items"]
    promoted = next(i for i in intents if i["id"] == intent_id)
    assert promoted["status"] == "已晋升"
    assert promoted["applied_at"]

    # 晋升失败路径：先落意图，再封批，审核时必须拒收且正式批零改。
    r2 = c.post(
        "/api/carrier-ingest/batch-change",
        headers=h,
        json=_payload(changes={"cargo_ref": "封批后强改"}),
    )
    assert r2.status_code == 200, r2.text
    seal = c.post("/api/seals", headers=h, json={"batch_id": 1})
    assert seal.status_code == 200, seal.text
    frozen = _batch_row(1)

    rr2 = c.post(
        f"/api/carrier-ingest/intents/{r2.json()['intent_id']}/review",
        headers=h,
        json={"approve": True},
    )
    assert rr2.status_code == 409, rr2.text
    rejected = rr2.json()["detail"]
    assert rejected["promoted"] is False
    assert rejected["intent_status"] == "已拒收"
    assert _batch_row(1) == frozen

    state2 = c.get("/api/carrier-ingest/state", headers=h).json()
    assert state2["unreviewed_effective"] == 0


def test_c_two_users_same_carrier_stub_contend_for_import_lock():
    """失败测丙：双人同桩抢改，后到者只记争用，业务零写。

    另开两个 TestClient 窗口，模拟两人同时对同一承运商空桩发起改批。
    """
    win_a = TestClient(app)
    win_b = TestClient(app)
    h_admin = _auth(win_a, "admin")
    h_ops = _auth(win_b, "ops")
    carrier = "顺冷物流"
    lock_key = f"carrier-import:{carrier}"

    before_row = _batch_row(1)
    jobs_before = _count("carrier_ingest_jobs")
    intents_before = _count("carrier_import_intents")

    # 窗口甲先抢到导入锁。
    assert (
        win_a.post(
            "/api/locks/acquire", headers=h_admin, json={"lock_key": lock_key}
        ).status_code
        == 200
    )
    # 窗口乙同桩抢改：只记争用，零写。
    r = win_b.post(
        "/api/carrier-ingest/batch-change",
        headers=h_ops,
        json=_payload(carrier_name=carrier),
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["path"] == "lock"
    assert detail["batches_unchanged"] is True
    assert detail["stub_records_added"] == 0
    assert detail["batches_changed"] == 0

    assert _batch_row(1) == before_row
    assert _count("carrier_ingest_jobs") == jobs_before
    assert _count("carrier_import_intents") == intents_before

    cont = win_a.get("/api/contention", headers=h_admin).json()["items"]
    assert any(
        x["lock_key"] == lock_key and x["actor"] == "ops" for x in cont
    )

    # 释放后同一空桩可落影子意图，但仍不得改正式批。
    assert (
        win_a.post(
            "/api/locks/release", headers=h_admin, json={"lock_key": lock_key}
        ).status_code
        == 200
    )
    r2 = win_b.post(
        "/api/carrier-ingest/batch-change",
        headers=h_ops,
        json=_payload(carrier_name=carrier),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["path"] == "B"
    assert r2.json()["batch_changed"] is False
    assert _batch_row(1) == before_row


def test_d_five_pages_share_same_review_source():
    """失败测丁：空桩页、批次页、仓间页、线路页、对账页审阅态须同源。"""
    c = TestClient(app)
    h = _auth(c)
    before_row = _batch_row(1)
    intent = c.post(
        "/api/carrier-ingest/batch-change", headers=h, json=_payload()
    ).json()
    assert intent["path"] == "B"

    stubs = c.get("/api/carrier-ingest/state", headers=h).json()
    batches = c.get("/api/batches", headers=h).json()["items"]
    chamber = c.get(
        f"/api/chambers/{before_row['chamber_id']}", headers=h
    ).json()
    routes = c.get("/api/routes", headers=h).json()
    recon = c.get("/api/reconcile", headers=h).json()

    state_row = next(b for b in stubs["batches"] if b["id"] == 1)
    batch_row = next(b for b in batches if b["id"] == 1)
    recon_row = next(
        r for r in recon["items"] if r["chamber_code"] == "CK-HD-01"
    )

    assert state_row["review_state"] == "待审核"
    assert state_row["pending_intent_id"] == intent["intent_id"]
    assert batch_row["review_state"] == state_row["review_state"]
    assert batch_row["pending_intent_id"] == state_row["pending_intent_id"]
    assert chamber["carrier_pending_intents"] == 1
    assert recon_row["carrier_pending_intents"] == 1
    assert routes["carrier_gate"]["pending_intents"] == 1
    assert routes["carrier_gate"]["memo"]["version"] == stubs["memo"]["version"]
    assert routes["carrier_gate"]["memo"]["status"] == "当班封死"
    assert recon["summary"]["carrier_gate"]["pending_intents"] == 1

    # 五页同源的代表性正式字段：待审期间处处不得体现影子意图。
    assert stubs["unreviewed_effective"] == 0
    assert batch_row["cargo_ref"] == before_row["cargo_ref"]
    assert state_row["cargo_ref"] == before_row["cargo_ref"]


def test_f_parallel_same_carrier_only_one_lands():
    """真并发：两窗同桩同时改批，只允许一条意图落定，后到者拒写。

    不借助公共锁端点——直接并发打承运商导入改批，导入锁与
    “同批次只允许一条待审核意图”双保险须让后到者只记 409。
    """
    carrier = "顺冷物流"

    def attempt(user: str) -> tuple[int, dict]:
        win = TestClient(app)
        h = _auth(win, user)
        r = win.post(
            "/api/carrier-ingest/batch-change",
            headers=h,
            json=_payload(carrier_name=carrier),
        )
        return r.status_code, r.json()

    before_row = _batch_row(1)
    before_intents = _count("carrier_import_intents")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["admin", "ops"]))

    codes = sorted(code for code, _ in results)
    assert codes == [200, 409], results
    failed = next(body for code, body in results if code == 409)["detail"]
    # 同桩争用的语义须是 lock/contention（不是“导入偏航”），且零写。
    assert failed["path"] == "lock"
    assert failed["reason"] == "contention"
    assert failed["batches_changed"] == 0
    assert failed["stub_records_added"] == 0
    # 无论后到者撞的是导入锁还是重复待审，都不得多落一条意图、不得改正式批。
    assert _count("carrier_import_intents") - before_intents == 1
    assert _batch_row(1) == before_row

    c = TestClient(app)
    state = c.get("/api/carrier-ingest/state", headers=_auth(c)).json()
    assert state["unreviewed_effective"] == 0
    assert any(i["status"] == "待审核" for i in state["intents"])
    cont = c.get("/api/contention", headers=_auth(c)).json()["items"]
    assert any(
        x["lock_key"] == f"carrier-import:{carrier}" for x in cont
    )


def test_g_drift_on_official_field_records_deviation_and_refuses():
    """偏离纪要：试图改正式字段 → 记“导入偏航”并拒写，正式批零改。"""
    c = TestClient(app)
    h = _auth(c)
    before_row = _batch_row(1)
    before_intents = _count("carrier_import_intents")

    r = c.post(
        "/api/carrier-ingest/batch-change",
        headers=h,
        json=_payload(changes={"status": "在批", "generation": 9}),
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["path"] == "A"
    assert detail["reason"] == "drift"
    assert detail["batches_changed"] == 0

    assert _batch_row(1) == before_row
    assert _count("carrier_import_intents") == before_intents

    audit = c.get("/api/audit?limit=50", headers=h).json()["items"]
    assert any(a["action"] == "导入偏航" for a in audit)


def test_h_gate_covers_empty_change_points_and_import_rows():
    """门闸不得被空 temp_points 或越界 import_rows 绕过。"""
    c = TestClient(app)
    h = _auth(c)
    before = _batch_row(1)
    before_intents = _count("carrier_import_intents")

    # 空 temp_points：晋升会把极值写成 0.0，必须在门闸处拒。
    r1 = c.post(
        "/api/carrier-ingest/batch-change",
        headers=h,
        json=_payload(changes={"temp_points": []}, import_rows=[]),
    )
    assert r1.status_code == 409, r1.text
    assert r1.json()["detail"]["batches_changed"] == 0

    # import_rows 越界而 changes 干净：导入结果本身也须过闸。
    r2 = c.post(
        "/api/carrier-ingest/batch-change",
        headers=h,
        json=_payload(import_rows=[{"c": 150.0}], changes={"cargo_ref": "仅改货载"}),
    )
    assert r2.status_code == 409, r2.text
    assert r2.json()["detail"]["reason"] == "gate"

    assert _batch_row(1) == before
    assert _count("carrier_import_intents") == before_intents


def test_e_closing_scan_no_unreviewed_change_across_pages():
    """收尾核：空桩/批次/仓间/线路/对账五页均无“未审已改”。"""
    c = TestClient(app)
    h = _auth(c)

    # 先制造一条待审意图，确保“无未审改”不是因为没数据。
    made = c.post(
        "/api/carrier-ingest/batch-change", headers=h, json=_payload()
    ).json()
    assert made["path"] == "B"

    stubs = c.get("/api/carrier-ingest/state", headers=h).json()
    assert stubs["pending_intents"] >= 1
    assert stubs["unreviewed_effective"] == 0

    batches = c.get("/api/batches", headers=h).json()["items"]
    for b in batches:
        with connect() as conn:
            row = conn.execute(
                "SELECT status,cargo_ref FROM temp_batches WHERE id=?", (b["id"],)
            ).fetchone()
        assert b["status"] == row["status"]
        assert b["cargo_ref"] == row["cargo_ref"]

    routes = c.get("/api/routes", headers=h).json()["carrier_gate"]
    recon = c.get("/api/reconcile", headers=h).json()["summary"]["carrier_gate"]
    assert routes == recon
    assert routes["pending_intents"] == stubs["pending_intents"]
    assert routes["memo"]["sealed"] is True
    assert routes["memo"]["status"] == "当班封死"

    # 拒收路径：影子意图拒收后正式批仍应零改。
    before = _batch_row(1)
    rr = c.post(
        f"/api/carrier-ingest/intents/{made['intent_id']}/review",
        headers=h,
        json={"approve": False, "review_note": "对账页复核拒收"},
    )
    assert rr.status_code == 200, rr.text
    assert rr.json()["intent_status"] == "已拒收"
    assert _batch_row(1) == before
    assert c.get("/api/carrier-ingest/state", headers=h).json()[
        "unreviewed_effective"
    ] == 0
