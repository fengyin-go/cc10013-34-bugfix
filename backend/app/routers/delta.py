"""增量采集接口：增量游标、批次数据集、增量待处理清单与恢复文件的导入导出。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.delta import DeltaService

router = APIRouter(prefix="/api/delta", tags=["增量采集"])

service = DeltaService()

LIST_FIELDS = ["游标编号", "目标端点", "增量字段", "当前水位", "回退步数", "批次上限", "去重窗口", "重跑开关"]
STATUSES = ["草稿", "待校验", "运行中", "已暂停"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按游标编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、运行中、已暂停"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按游标编号与状态过滤增量采集列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/pending", response_model=PageResult[dict])
def list_pending() -> PageResult[dict]:
    """增量待处理清单：装载暂停滞留与恢复结果的未完成游标。"""
    items = service.list_pending()
    return PageResult(items=items, total=len(items), page=1, size=len(items) or 1)


@router.get("/batches")
def list_batches(keyword: str | None = Query(default=None, description="按批次编号或游标编号检索")) -> dict[str, Any]:
    """增量批次数据集：更新节点，未完成游标与确认水位都以这里为准。"""
    items = service.list_batches(keyword=keyword)
    return {"items": items, "total": len(items)}


@router.get("/batches/{batch_id}")
def get_batch(batch_id: str) -> dict[str, Any]:
    batch = service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在或已结束归档")
    return batch


@router.get("/recovery-files")
def list_recovery_files(
    keyword: str | None = Query(default=None, description="按文件编号、批次编号检索"),
) -> dict[str, Any]:
    """恢复文件清单：文件清单按批次核对。"""
    items = service.list_recovery_files(keyword=keyword)
    return {"items": items, "total": len(items)}


@router.get("/recovery-files/{file_id}")
def get_recovery_file(file_id: str) -> dict[str, Any]:
    record = service.get_recovery_file(file_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"恢复文件 {file_id} 不存在")
    return record


@router.get("/recovery-files/{file_id}/download")
def download_recovery_file(file_id: str) -> JSONResponse:
    """下载恢复文件：返回文件内容（恢复文件身份、水位参数、失败边界）。"""
    result = service.download_recovery_file(file_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"恢复文件 {file_id} 不存在")
    payload, filename = result
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/recovery-files/{file_id}/package", response_model=ActionResult)
def package_recovery_files(file_id: str, payload: EntryPayload | None = None) -> ActionResult:
    """打包恢复文件；可在 values.file_ids 里附带同批次其它文件一起打包。"""
    file_ids = [file_id]
    if payload and payload.values.get("file_ids"):
        extra = payload.values.get("file_ids")
        if isinstance(extra, list):
            file_ids.extend(str(item) for item in extra)
    result = service.package_recovery_files(file_ids)
    if not result.get("ok"):
        return ActionResult(ok=False, message=str(result.get("message")))
    return ActionResult(ok=True, message=str(result.get("message")), entry=result)


@router.post("/recovery-files/upload", response_model=ActionResult)
def upload_recovery_file(payload: EntryPayload) -> ActionResult:
    """上传恢复文件：登记文件身份并绑定原批次，不触发恢复。"""
    record, message = service.upload_recovery_file(payload.values)
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=record)


@router.post("/recovery-files/{file_id}/import", response_model=ActionResult)
def import_recovery_file(file_id: str) -> ActionResult:
    """导入恢复文件并恢复增量批次数据集；重复导入同一文件只恢复原批次一次。"""
    view, message = service.import_recovery_file(file_id)
    if view is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=view)


@router.post("/ingest", response_model=ActionResult)
def ingest_cursor(payload: EntryPayload) -> ActionResult:
    """新记录到达入口：更晚记录进入明确补偿边界，不覆盖确认水位。"""
    values = payload.values
    try:
        cursor_id = int(values["cursor_id"])
    except (KeyError, TypeError, ValueError):
        return ActionResult(ok=False, message="缺少 cursor_id 或格式不正确")
    cursor_code = str(values.get("cursor_code") or "").strip()
    position = str(values.get("position") or "").strip()
    if not cursor_code or not position:
        return ActionResult(ok=False, message="缺少 cursor_code 或 position")
    view, message = service.register_later_cursor(cursor_id, cursor_code, position)
    if view is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=view)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出增量采集清单：返回当前过滤条件下的全量数据。"""
    return service.export_snapshot()


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条增量游标明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"增量游标 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条增量游标，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="增量游标已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条增量游标执行提交校验、启动增量、暂停增量；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
