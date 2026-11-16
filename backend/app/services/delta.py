"""增量采集业务规则：状态流转、批次数据集、恢复文件导入与失败边界都收在这里。

修复口径（与复测样例对齐）：

1. 暂停批次仍是未结束批次，``pending`` 保持为真，概览不得再把它算成已处理。
2. 恢复文件导入必须沿用原游标和批次身份，把恢复文件、水位参数（当前水位、回退步数、
   去重窗口）与失败边界一起恢复；同一恢复文件重复导入只能恢复原批次一次。
3. 每个批次的确认水位全系统只能有一个；暂停后到达的更晚记录进入明确的补偿边界，
   不允许覆盖确认水位，也不允许生成第二份未结束批次记录。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "delta"
BATCH_MODULE = "delta_batch"
FILE_MODULE = "delta_recovery_file"
PENDING_MODULE = "delta_pending"

REQUIRED_FIELDS = ["游标编号", "目标端点", "增量字段"]
STATUS_ORDER = ["草稿", "待校验", "运行中", "已暂停"]
ACTION_RULES = {"提交校验": "待校验", "启动增量": "运行中", "暂停增量": "已暂停"}
NEGATIVE_ACTIONS = []

# 批次数据集的状态：运行中 -> 已暂停（可恢复）-> 已恢复 -> 补偿中 -> 已结束
BATCH_RUNNING = "运行中"
BATCH_PAUSED = "已暂停"
BATCH_RECOVERED = "已恢复"
BATCH_COMPENSATING = "补偿中"
BATCH_FINISHED = "已结束"
UNFINISHED_BATCH_STATUSES = {BATCH_RUNNING, BATCH_PAUSED, BATCH_RECOVERED, BATCH_COMPENSATING}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class DeltaService:
    # ------------------------------------------------------------------ 游标清单
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("游标编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        # 游标明细抽屉回显：确认水位、回退步数、去重窗口必须与批次数据集一致。
        batch = self._find_batch_by_cursor_id(entry_id)
        result = dict(entry)
        if batch is not None:
            result.setdefault("确认水位", batch.get("确认水位"))
            result["批次编号"] = batch.get("批次编号")
            result["恢复文件编号"] = batch.get("恢复文件编号")
            result["批次状态"] = batch.get("状态")
            result["失败边界"] = self._failure_boundary(batch)
        return result

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 水位参数在登记时一并落库，避免后续恢复时丢失。
        entry["当前水位"] = values.get("当前水位", "")
        entry["回退步数"] = values.get("回退步数", 0)
        entry["批次上限"] = values.get("批次上限", 100)
        entry["去重窗口"] = values.get("去重窗口", 0)
        entry["重跑开关"] = values.get("重跑开关", "关闭")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"增量游标 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于增量采集可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        # 暂停不是处理完成：暂停批次仍有未完成游标，概览待处理量必须继续统计它。
        entry["pending"] = target != "运行中" or self._has_unfinished_batch(entry_id)
        entry["abnormal"] = action in NEGATIVE_ACTIONS

        batch = self._find_batch_by_cursor_id(entry_id)
        if action == "暂停增量":
            if batch is not None:
                batch["状态"] = BATCH_PAUSED
                # 确认水位以当前水位为准，且全批次只保留这一个。
                batch["确认水位"] = entry.get("当前水位")
                batch["当前水位"] = entry.get("当前水位")
                entry["确认水位"] = entry.get("当前水位")
                self._rebuild_pending_list()
        elif action == "启动增量" and batch is not None:
            batch["状态"] = BATCH_RUNNING
            self._rebuild_pending_list()
        return entry, f"增量游标已{action}"

    # ------------------------------------------------------------------ 批次数据集
    def list_batches(self, keyword: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(BATCH_MODULE)
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("批次编号", "")) or keyword in str(row.get("游标编号", ""))
            ]
        return [dict(row) for row in rows]

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_MODULE):
            if row.get("批次编号") == batch_id:
                result = dict(row)
                result["失败边界"] = self._failure_boundary(row)
                return result
        return None

    def _find_batch_by_cursor_id(self, cursor_id: int) -> dict[str, Any] | None:
        for row in store.rows(BATCH_MODULE):
            if int(row.get("游标id", 0)) == cursor_id:
                return row
        return None

    def _find_batch(self, batch_id: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_MODULE):
            if row.get("批次编号") == batch_id:
                return row
        return None

    @staticmethod
    def _failure_boundary(batch: dict[str, Any]) -> dict[str, Any]:
        return {
            "起点": batch.get("失败边界起点"),
            "终点": batch.get("失败边界终点"),
            "失败片段": list(batch.get("失败片段", [])),
        }

    def _has_unfinished_batch(self, cursor_id: int) -> bool:
        batch = self._find_batch_by_cursor_id(cursor_id)
        return batch is not None and batch.get("状态") in UNFINISHED_BATCH_STATUSES

    # -------------------------------------------------------------- 增量待处理清单
    def list_pending(self) -> list[dict[str, Any]]:
        """未完成游标（含暂停批次、恢复结果、补偿边界）统一落到增量待处理清单。"""
        self._rebuild_pending_list()
        return [dict(row) for row in store.rows(PENDING_MODULE)]

    def _rebuild_pending_list(self) -> None:
        """按现有取数逻辑（批次数据集 -> 未完成游标）重建待处理清单。

        更新节点固定在批次数据集：只有批次状态或未完成游标变化时才重建，
        保证恢复结果装载与游标抽屉回显用的是同一份确认水位。
        """
        table = store.rows(PENDING_MODULE)
        table.clear()
        seq = 1
        for batch in store.rows(BATCH_MODULE):
            if batch.get("状态") not in UNFINISHED_BATCH_STATUSES:
                continue
            watermark = batch.get("确认水位")
            for cursor_code in batch.get("未完成游标", []):
                table.append({
                    "id": seq,
                    "待处理编号": f"P-{batch.get('批次编号')}-{seq:03d}",
                    "批次编号": batch.get("批次编号"),
                    "游标编号": batch.get("游标编号"),
                    "未完成游标": cursor_code,
                    "确认水位": watermark,
                    "失败边界起点": batch.get("失败边界起点"),
                    "失败边界终点": batch.get("失败边界终点"),
                    "恢复文件编号": batch.get("恢复文件编号"),
                    "批次状态": batch.get("状态"),
                    "来源": "恢复装载" if batch.get("恢复状态") == "已恢复" else "暂停滞留",
                })
                seq += 1
            # 冲突游标单独标注补偿边界，处理优先级保持待定，不覆盖确认水位。
            for cursor_code in batch.get("冲突游标", []):
                table.append({
                    "id": seq,
                    "待处理编号": f"P-{batch.get('批次编号')}-{seq:03d}",
                    "批次编号": batch.get("批次编号"),
                    "游标编号": batch.get("游标编号"),
                    "未完成游标": cursor_code,
                    "确认水位": watermark,
                    "失败边界起点": batch.get("失败边界终点"),
                    "失败边界终点": batch.get("冲突边界终点"),
                    "恢复文件编号": batch.get("恢复文件编号"),
                    "批次状态": batch.get("状态"),
                    "来源": "冲突补偿（优先级待定）",
                })
                seq += 1

    # ------------------------------------------------------------------ 恢复文件
    def list_recovery_files(self, keyword: str | None = None) -> list[dict[str, Any]]:
        rows = store.rows(FILE_MODULE)
        if keyword:
            rows = [
                row for row in rows
                if keyword in str(row.get("文件编号", ""))
                or keyword in str(row.get("批次编号", ""))
                or keyword in str(row.get("游标编号", ""))
            ]
        return [dict(row) for row in rows]

    def get_recovery_file(self, file_id: str) -> dict[str, Any] | None:
        for row in store.rows(FILE_MODULE):
            if row.get("文件编号") == file_id:
                return dict(row)
        return None

    def build_recovery_payload(self, file_id: str) -> dict[str, Any] | None:
        """组装恢复文件内容：恢复文件身份、水位参数与失败边界打包在一起。"""
        record = self.get_recovery_file(file_id)
        if record is None:
            return None
        batch = self._find_batch(str(record.get("批次编号")))
        payload = {
            "文件编号": record.get("文件编号"),
            "批次编号": record.get("批次编号"),
            "游标编号": record.get("游标编号"),
            "内容版本": record.get("内容版本"),
            "导出时间": _now(),
            "水位参数": {
                "当前水位": batch.get("当前水位") if batch else None,
                "确认水位": batch.get("确认水位") if batch else None,
                "回退步数": batch.get("回退步数") if batch else None,
                "去重窗口": batch.get("去重窗口") if batch else None,
                "批次上限": batch.get("批次上限") if batch else None,
            },
            "失败边界": self._failure_boundary(batch) if batch else None,
            "未完成游标": list(batch.get("未完成游标", [])) if batch else [],
        }
        return payload

    def download_recovery_file(self, file_id: str) -> tuple[dict[str, Any], str] | None:
        live = self._find_recovery_row(file_id)
        if live is None:
            return None
        live["下载次数"] = int(live.get("下载次数", 0)) + 1
        payload = self.build_recovery_payload(file_id)
        assert payload is not None
        return payload, f"{live.get('文件名')}"

    def _find_recovery_row(self, file_id: str) -> dict[str, Any] | None:
        for row in store.rows(FILE_MODULE):
            if row.get("文件编号") == file_id:
                return row
        return None

    def package_recovery_files(self, file_ids: list[str] | None = None) -> dict[str, Any]:
        """打包恢复文件：文件清单按批次核对，返回一个可下载的归档描述。"""
        records = store.rows(FILE_MODULE)
        if file_ids:
            wanted = set(file_ids)
            records = [row for row in records if row.get("文件编号") in wanted]
        if not records:
            return {"ok": False, "message": "没有可打包的恢复文件", "items": []}
        package_items: list[dict[str, Any]] = []
        batches: dict[str, list[str]] = {}
        for record in records:
            record["打包状态"] = "已打包"
            payload = self.build_recovery_payload(str(record["文件编号"]))
            package_items.append(payload)
            batches.setdefault(str(record.get("批次编号")), []).append(str(record.get("文件编号")))
        # 文件清单按批次核对：同批次的文件归在一组，跨批次不允许串档。
        manifest = [
            {"批次编号": batch_id, "恢复文件": files, "文件数": len(files)}
            for batch_id, files in sorted(batches.items())
        ]
        return {
            "ok": True,
            "message": f"已打包 {len(records)} 个恢复文件",
            "打包时间": _now(),
            "文件清单": manifest,
            "items": package_items,
        }

    def upload_recovery_file(self, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """上传恢复文件：只登记文件身份并绑定批次，导入动作由 import_recovery_file 完成。"""
        file_id = str(payload.get("文件编号") or "").strip()
        batch_id = str(payload.get("批次编号") or "").strip()
        if not file_id or not batch_id:
            return None, "恢复文件缺少文件编号或批次编号"
        if self._find_batch(batch_id) is None:
            return None, f"批次 {batch_id} 不存在，恢复文件无法绑定"
        existing = self.get_recovery_file(file_id)
        if existing is not None:
            return None, f"恢复文件 {file_id} 已存在，重复上传不会生成新文件"
        records = store.rows(FILE_MODULE)
        record = {
            "id": max((int(row.get("id", 0)) for row in records), default=0) + 1,
            "文件编号": file_id,
            "文件名": str(payload.get("文件名") or f"{file_id}-recovery.json"),
            "批次编号": batch_id,
            "游标编号": str(payload.get("游标编号") or ""),
            "确认水位": payload.get("确认水位"),
            "文件格式": str(payload.get("文件格式") or "json"),
            "打包状态": "未打包",
            "下载次数": 0,
            "上传时间": _now(),
            "内容版本": int(payload.get("内容版本") or 1),
            "文件清单": list(payload.get("文件清单") or []),
        }
        records.append(record)
        return record, f"恢复文件 {file_id} 已上传并绑定批次 {batch_id}"

    # -------------------------------------------------------------- 导入与恢复
    def import_recovery_file(self, file_id: str) -> tuple[dict[str, Any] | None, str]:
        """导入恢复文件并恢复增量批次数据集。

        幂等口径：
        - 文件不存在 / 未绑定批次：拒绝导入。
        - 同一文件第二次导入：只回显原批次恢复结果，不重复恢复、不生成第二份未结束记录。
        - 恢复沿用原游标和批次身份，水位参数（当前水位、回退步数、去重窗口）与失败边界
          整体还原；确认水位全批次保持唯一。
        """
        record = self.get_recovery_file(file_id)
        if record is None:
            return None, f"恢复文件 {file_id} 不存在，请先上传"
        batch = self._find_batch(str(record.get("批次编号")))
        if batch is None:
            return None, f"恢复文件 {file_id} 绑定的批次 {record.get('批次编号')} 不存在"

        # 幂等：重复导入同一文件只能恢复原批次一次。
        if batch.get("恢复文件编号") == file_id and batch.get("恢复状态") == "已恢复":
            return self._recovery_view(batch), f"恢复文件 {file_id} 已导入过，原批次 {batch['批次编号']} 仅恢复一次"

        payload = self.build_recovery_payload(file_id)
        assert payload is not None
        watermark_params = payload.get("水位参数") or {}

        # 沿用原游标与批次身份：只还原参数，不新建批次、不换批次编号。
        batch["恢复文件编号"] = file_id
        batch["恢复状态"] = "已恢复"
        batch["导入次数"] = int(batch.get("导入次数", 0)) + 1
        batch["状态"] = BATCH_RECOVERED if batch.get("状态") == BATCH_PAUSED else batch.get("状态")
        batch["当前水位"] = watermark_params.get("当前水位")
        # 确认水位只能有一个：恢复时以文件携带的确认水位覆盖回原值，绝不新增第二个。
        batch["确认水位"] = watermark_params.get("确认水位")
        batch["回退步数"] = watermark_params.get("回退步数")
        batch["去重窗口"] = watermark_params.get("去重窗口")
        batch["批次上限"] = watermark_params.get("批次上限")
        boundary = payload.get("失败边界") or {}
        batch["失败边界起点"] = boundary.get("起点")
        batch["失败边界终点"] = boundary.get("终点")
        batch["失败片段"] = list(boundary.get("失败片段", []))
        if payload.get("未完成游标"):
            batch["未完成游标"] = list(payload["未完成游标"])

        # 同步游标主记录：抽屉与列表看到的水位参数必须和批次数据集一致。
        entry = store.find(MODULE, int(batch["游标id"]))
        if entry is not None:
            entry["当前水位"] = batch["当前水位"]
            entry["确认水位"] = batch["确认水位"]
            entry["回退步数"] = batch["回退步数"]
            entry["去重窗口"] = batch["去重窗口"]
            entry["批次上限"] = batch["批次上限"]
            entry["恢复文件编号"] = file_id
            entry["批次编号"] = batch["批次编号"]
            entry["pending"] = True
            if entry.get("status") == "已暂停":
                entry["status"] = "运行中"

        # 恢复结果装载进增量待处理清单。
        self._rebuild_pending_list()
        return self._recovery_view(batch), f"恢复文件 {file_id} 已导入，批次 {batch['批次编号']} 沿用原游标恢复"

    def register_later_cursor(self, cursor_id: int, cursor_code: str, position: str) -> tuple[dict[str, Any] | None, str]:
        """暂停/恢复后到达的更晚记录：进入明确补偿边界，不覆盖确认水位。

        处理优先级保持待定，但确认水位仍只有一个，失败边界保持完整（起点不被推进）。
        """
        batch = self._find_batch_by_cursor_id(cursor_id)
        if batch is None:
            return None, f"游标 {cursor_id} 没有关联批次数据集"
        if position <= str(batch.get("确认水位")):
            return None, f"记录位置 {position} 不晚于确认水位 {batch.get('确认水位')}，按去重窗口丢弃"
        if cursor_code in batch.get("冲突游标", []):
            return self._recovery_view(batch), f"记录 {cursor_code} 已在补偿边界内，不重复登记"
        batch.setdefault("冲突游标", []).append(cursor_code)
        # 补偿边界从失败边界终点之后开始，确认水位不动。
        batch["冲突边界终点"] = position
        if batch.get("状态") == BATCH_RECOVERED:
            batch["状态"] = BATCH_COMPENSATING
        entry = store.find(MODULE, cursor_id)
        if entry is not None:
            entry["pending"] = True
        self._rebuild_pending_list()
        return self._recovery_view(batch), f"更晚记录 {cursor_code} 已进入补偿边界，确认水位保持 {batch.get('确认水位')}"

    def _recovery_view(self, batch: dict[str, Any]) -> dict[str, Any]:
        return {
            "批次编号": batch.get("批次编号"),
            "游标编号": batch.get("游标编号"),
            "批次状态": batch.get("状态"),
            "恢复文件编号": batch.get("恢复文件编号"),
            "恢复状态": batch.get("恢复状态"),
            "导入次数": batch.get("导入次数"),
            "当前水位": batch.get("当前水位"),
            "确认水位": batch.get("确认水位"),
            "回退步数": batch.get("回退步数"),
            "去重窗口": batch.get("去重窗口"),
            "失败边界": self._failure_boundary(batch),
            "未完成游标": list(batch.get("未完成游标", [])),
            "冲突游标": list(batch.get("冲突游标", [])),
        }

    # ------------------------------------------------------------------ 测试辅助
    def snapshot(self) -> dict[str, Any]:
        """供复测脚本核对：恢复前后的游标编号与失败片段要能连续解释。"""
        return {
            "cursors": [
                {
                    "游标编号": row.get("游标编号"),
                    "当前水位": row.get("当前水位"),
                    "确认水位": row.get("确认水位"),
                    "回退步数": row.get("回退步数"),
                    "去重窗口": row.get("去重窗口"),
                    "pending": row.get("pending"),
                }
                for row in store.rows(MODULE)
            ],
            "batches": [self.get_batch(str(row["批次编号"])) for row in store.rows(BATCH_MODULE)],
            "pending": self.list_pending(),
            "files": self.list_recovery_files(),
        }

    def export_snapshot(self) -> dict[str, Any]:
        items, total = self.list_entries(page=1, size=10000)
        return {"module": "delta", "total": total, "items": items}
