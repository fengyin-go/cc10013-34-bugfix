"""增量批次恢复复测脚本。

复测口径（与需求单一致）：
1. DELTA-208 的恢复文件为 D88，确认水位停在 09:40；暂停批次不能被概览当成已处理。
2. 同一恢复文件导入两次：原批次只恢复一次，不生成第二份未结束记录，水位参数不丢。
3. 随后到达的一条更晚记录进入明确补偿边界，确认水位仍只有一个且失败边界完整。
4. 恢复前后的游标编号与失败片段连续可解释；待处理清单装载恢复结果，抽屉回显确认水位。
5. 恢复文件支持下载、上传、打包，文件清单按批次核对。

运行：backend/.venv/bin/python -m tests.test_delta_recovery
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app
from app.store import store

client = TestClient(app)

PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: object = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label} :: {detail}")


def reset_state() -> None:
    """每个断言组前重置内存仓库，保证复测可重复。"""
    from app.seed import SEED_ROWS

    # 直接替换底层表内容，保留同一个 store 单例。
    for name, rows in SEED_ROWS.items():
        table = store.rows(name)
        table.clear()
        table.extend(dict(row) for row in rows)


def main() -> int:
    # ------------------------------------------------------------ 1. 初始暂停态
    print("【1】暂停批次口径：DELTA-208 / D88 / 确认水位 09:40")
    reset_state()
    health = client.get("/api/health").json()
    check("内部恢复表不污染业务模块数（仍 18 个模块）", health["modules"] == 18, health)
    overview = client.get("/api/overview").json()
    check("概览模块清单不出现内部表", all(item["name"] not in {"delta_batch", "delta_recovery_file", "delta_pending"} for item in overview["modules"]), overview["modules"])
    delta_module = next(item for item in overview["modules"] if item["name"] == "delta")
    # 旧口径会把已暂停算成已处理（pending 只有 2）；修复后暂停的 DELTA-208 仍计入待处理（3）。
    check("概览把暂停批次计入待处理（pending 含 DELTA-208）", delta_module["pending"] == 3, delta_module)
    list_rows = client.get("/api/delta").json()["items"]
    paused_row = next(item for item in list_rows if item["游标编号"] == "DELTA-208")
    check("增量列表里暂停批次 pending 为真", paused_row["pending"] is True, paused_row)

    cursor = client.get("/api/delta/208").json()
    check("游标编号为 DELTA-208", cursor["游标编号"] == "DELTA-208", cursor.get("游标编号"))
    check("当前水位停在 09:40", cursor["当前水位"] == "2026-11-16 09:40", cursor.get("当前水位"))
    check("确认水位停在 09:40", cursor["确认水位"] == "2026-11-16 09:40", cursor.get("确认水位"))
    check("回退步数保留为 3", cursor["回退步数"] == 3, cursor.get("回退步数"))
    check("去重窗口保留为 500", cursor["去重窗口"] == 500, cursor.get("去重窗口"))
    check("游标 pending 为真（暂停不等于已处理）", cursor["pending"] is True, cursor.get("pending"))

    batches = client.get("/api/delta/batches").json()["items"]
    batch_208 = next(item for item in batches if item["游标编号"] == "DELTA-208")
    check("批次身份 B-DELTA-208-01", batch_208["批次编号"] == "B-DELTA-208-01", batch_208.get("批次编号"))
    check("批次处于已暂停", batch_208["状态"] == "已暂停", batch_208.get("状态"))
    watermarks = {item.get("确认水位") for item in batches if item["批次编号"] == "B-DELTA-208-01"}
    check("确认水位只有一个", watermarks == {"2026-11-16 09:40"}, watermarks)

    pending = client.get("/api/delta/pending").json()["items"]
    check("未完成游标落到增量待处理清单", {item["未完成游标"] for item in pending} == {"C-208-0943", "C-208-0944"}, pending)
    check("待处理清单回显确认水位 09:40", all(item["确认水位"] == "2026-11-16 09:40" for item in pending), pending)

    # ------------------------------------------------------------ 2. 第一次导入
    print("【2】第一次导入 D88：沿用原游标和批次身份恢复")
    before_batch_count = len(store.rows("delta_batch"))
    resp = client.post("/api/delta/recovery-files/D88/import").json()
    check("导入成功", resp["ok"] is True, resp)
    restored = resp["entry"]
    check("恢复沿用原批次身份", restored["批次编号"] == "B-DELTA-208-01", restored.get("批次编号"))
    check("恢复沿用原游标", restored["游标编号"] == "DELTA-208", restored.get("游标编号"))
    check("恢复后当前水位仍是 09:40", restored["当前水位"] == "2026-11-16 09:40", restored.get("当前水位"))
    check("恢复后确认水位仍是 09:40", restored["确认水位"] == "2026-11-16 09:40", restored.get("确认水位"))
    check("回退步数恢复为 3", restored["回退步数"] == 3, restored.get("回退步数"))
    check("去重窗口恢复为 500", restored["去重窗口"] == 500, restored.get("去重窗口"))
    check("失败边界完整（起点/终点/片段）", restored["失败边界"] == {
        "起点": "2026-11-16 09:41",
        "终点": "2026-11-16 09:45",
        "失败片段": ["F-208-0941", "F-208-0942"],
    }, restored.get("失败边界"))
    check("导入次数记为 1", restored["导入次数"] == 1, restored.get("导入次数"))
    check("没有生成第二份批次记录", len(store.rows("delta_batch")) == before_batch_count, "批次数量发生变化")

    cursor_after = client.get("/api/delta/208").json()
    check("游标抽屉回显确认水位 09:40", cursor_after["确认水位"] == "2026-11-16 09:40", cursor_after)
    check("游标抽屉回显失败片段", cursor_after["失败边界"]["失败片段"] == ["F-208-0941", "F-208-0942"], cursor_after)

    pending_after = client.get("/api/delta/pending").json()["items"]
    check("待处理清单装载恢复结果（仍 2 条未完成游标）", len(pending_after) == 2, pending_after)
    check("恢复结果来源标注为恢复装载", all(item["来源"] == "恢复装载" for item in pending_after), pending_after)

    # ------------------------------------------------------------ 3. 第二次导入
    print("【3】第二次导入同一恢复文件 D88：原批次只恢复一次")
    resp2 = client.post("/api/delta/recovery-files/D88/import").json()
    check("重复导入被幂等拦截（ok 但提示已导入）", resp2["ok"] is True and "仅恢复一次" in resp2["message"], resp2)
    check("重复导入仍指向原批次", resp2["entry"]["批次编号"] == "B-DELTA-208-01", resp2["entry"])
    check("导入次数仍为 1", resp2["entry"]["导入次数"] == 1, resp2["entry"].get("导入次数"))
    check("批次记录仍只有一份", len(store.rows("delta_batch")) == before_batch_count)
    batch_row = store.rows("delta_batch")[0]
    check("确认水位仍只有一个值", batch_row["确认水位"] == "2026-11-16 09:40", batch_row)

    # ------------------------------------------------------------ 4. 更晚记录
    print("【4】随后到达的更晚记录进入明确补偿边界")
    ingest = client.post("/api/delta/ingest", json={
        "values": {"cursor_id": 208, "cursor_code": "C-208-0950", "position": "2026-11-16 09:50"},
    }).json()
    check("更晚记录受理成功", ingest["ok"] is True, ingest)
    check("确认水位保持 09:40 不被覆盖", ingest["entry"]["确认水位"] == "2026-11-16 09:40", ingest["entry"])
    check("冲突游标进入补偿边界", "C-208-0950" in ingest["entry"]["冲突游标"], ingest["entry"])
    check("批次进入补偿中（优先级待定但边界明确）", ingest["entry"]["批次状态"] == "补偿中", ingest["entry"])

    pending_final = client.get("/api/delta/pending").json()["items"]
    conflict = next(item for item in pending_final if item["未完成游标"] == "C-208-0950")
    check("待处理清单含冲突补偿项且标注优先级待定", "优先级待定" in conflict["来源"], conflict)
    check("补偿边界起点接在失败边界终点之后", conflict["失败边界起点"] == "2026-11-16 09:45", conflict)
    check("补偿项确认水位仍为 09:40", conflict["确认水位"] == "2026-11-16 09:40", conflict)

    batch_final = client.get("/api/delta/batches/B-DELTA-208-01").json()
    check("失败边界未被推进（片段连续可解释）", batch_final["失败边界"]["失败片段"] == ["F-208-0941", "F-208-0942"], batch_final)
    check("批次确认水位字段仍唯一", batch_final["确认水位"] == "2026-11-16 09:40", batch_final)

    # 早于确认水位的记录必须被去重口径拒绝
    stale = client.post("/api/delta/ingest", json={
        "values": {"cursor_id": 208, "cursor_code": "C-208-0930", "position": "2026-11-16 09:30"},
    }).json()
    check("不晚于确认水位的记录被拒绝", stale["ok"] is False, stale)

    # ------------------------------------------------------------ 5. 文件能力
    print("【5】恢复文件下载 / 上传 / 打包，清单按批次核对")
    download = client.get("/api/delta/recovery-files/D88/download")
    check("下载返回 200", download.status_code == 200, download.status_code)
    payload = download.json()
    check("下载内容含水位参数", payload["水位参数"]["确认水位"] == "2026-11-16 09:40", payload)
    check("下载内容含失败边界", payload["失败边界"]["失败片段"] == ["F-208-0941", "F-208-0942"], payload)
    file_record = client.get("/api/delta/recovery-files/D88").json()
    check("下载次数累加", file_record["下载次数"] == 1, file_record.get("下载次数"))

    upload = client.post("/api/delta/recovery-files/upload", json={
        "values": {
            "文件编号": "D90",
            "文件名": "D90-DELTA-208-recovery.json",
            "批次编号": "B-DELTA-208-01",
            "游标编号": "DELTA-208",
            "确认水位": "2026-11-16 09:40",
            "文件清单": [{"文件名": "D90-DELTA-208-recovery.json", "批次编号": "B-DELTA-208-01"}],
        },
    }).json()
    check("上传成功并绑定原批次", upload["ok"] is True and upload["entry"]["批次编号"] == "B-DELTA-208-01", upload)
    upload_again = client.post("/api/delta/recovery-files/upload", json={
        "values": {"文件编号": "D90", "批次编号": "B-DELTA-208-01"},
    }).json()
    check("重复上传被拒绝", upload_again["ok"] is False, upload_again)
    upload_bad = client.post("/api/delta/recovery-files/upload", json={
        "values": {"文件编号": "D91", "批次编号": "B-NOT-EXIST"},
    }).json()
    check("绑定不存在批次被拒绝", upload_bad["ok"] is False, upload_bad)

    package = client.post("/api/delta/recovery-files/D88/package", json={"values": {"file_ids": ["D90"]}}).json()
    check("打包成功", package["ok"] is True, package)
    check("文件清单按批次核对（同批一组）", package["entry"]["文件清单"] == [
        {"批次编号": "B-DELTA-208-01", "恢复文件": ["D88", "D90"], "文件数": 2},
    ], package["entry"])
    files = client.get("/api/delta/recovery-files").json()["items"]
    check("两个文件均标记已打包", all(item["打包状态"] == "已打包" for item in files if item["文件编号"] in {"D88", "D90"}), files)

    # ------------------------------------------------------------ 6. 连续性
    print("【6】恢复前后游标编号与失败片段连续解释")
    snapshot = client.get("/api/delta/export").json()
    delta_208 = next(item for item in snapshot["items"] if item["游标编号"] == "DELTA-208")
    check("导出清单仍能查到 DELTA-208 原游标", delta_208 is not None)
    check("游标编号未被恢复改写", delta_208["游标编号"] == "DELTA-208", delta_208)
    check("失败片段编号连续（0941、0942）", batch_final["失败边界"]["失败片段"] == ["F-208-0941", "F-208-0942"])

    print(f"\n复测结果：通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
