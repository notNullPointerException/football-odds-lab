"""用户操作日志接口。

- POST /api/audit/click  前端菜单/按钮点击上报（登录用户）
- GET  /api/audit/logs   按用户名/类型/时间查询（仅管理员）
"""
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.auth import require_admin, require_login
from app.core.rate_limit import client_ip
from app.db.session import get_db
from app.models.user import User
from app.models.user_action_log import UserActionLog
from app.services.audit_service import log_action

router = APIRouter()


class ClickIn(BaseModel):
    name: str = Field(max_length=120)
    page: str = Field(default="", max_length=255)


@router.post("/audit/click")
async def audit_click(body: ClickIn, request: Request,
                      user: User = Depends(require_login)):
    """记录一次菜单/按钮点击。写失败不影响前端。"""
    log_action(
        user_id=user.id,
        username=user.username,
        action_type="menu",
        action=body.name,
        method="POST",
        path=(body.page or request.url.path)[:255],
        query="",
        status_code=200,
        ip=client_ip(request),
        ua=request.headers.get("user-agent", "")[:512],
        duration_ms=0,
    )
    return {"ok": True}


def _parse_date(value: str, end_of_day: bool = False) -> datetime:
    value = (value or "").strip()
    if not value:
        raise HTTPException(400, "日期不能为空")
    try:
        if len(value) == 10:  # YYYY-MM-DD
            d = datetime.strptime(value, "%Y-%m-%d")
            return d + timedelta(days=1) if end_of_day else d
        return datetime.fromisoformat(value.replace("T", " "))
    except ValueError:
        raise HTTPException(400, "日期格式应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM")


@router.get("/audit/logs")
def audit_logs(
    username: str = "",
    action_type: str = "",
    keyword: str = "",
    days: int = Query(7, ge=0, le=365),
    start: str = "",
    end: str = "",
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    q = db.query(UserActionLog)
    if username.strip():
        q = q.filter(UserActionLog.username == username.strip())
    if action_type.strip():
        q = q.filter(UserActionLog.action_type == action_type.strip())
    if keyword.strip():
        like = f"%{keyword.strip()}%"
        q = q.filter(or_(
            UserActionLog.path.like(like),
            UserActionLog.action.like(like),
            UserActionLog.query.like(like),
        ))
    if days > 0:
        # 「今天」= 本地零点起；近 7 天 = 含今天在内的 7 个自然日
        day0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        q = q.filter(UserActionLog.created_at >= day0 - timedelta(days=days - 1))
    elif start:
        q = q.filter(UserActionLog.created_at >= _parse_date(start))
        if end:
            q = q.filter(UserActionLog.created_at < _parse_date(end, end_of_day=True))

    total = q.count()
    rows = (
        q.order_by(UserActionLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "username": r.username,
                "action_type": r.action_type,
                "action": r.action,
                "method": r.method,
                "path": r.path,
                "query": r.query,
                "status_code": r.status_code,
                "ip": r.ip,
                "ua": r.ua,
                "duration_ms": r.duration_ms,
                "created_at": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else None,
            }
            for r in rows
        ],
    }
