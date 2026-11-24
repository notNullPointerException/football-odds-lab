"""Menu and role-menu models for dynamic menu control."""
from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, Table
from sqlalchemy.orm import relationship
from app.db.base import Base

# Role-Menu association table
role_menus = Table(
    "role_menus",
    Base.metadata,
    Column("role", String(20), primary_key=True, index=True),
    Column("menu_id", Integer, ForeignKey("menus.id"), primary_key=True),
)

class Menu(Base):
    __tablename__ = "menus"
    
    id = Column(Integer, primary_key=True)
    name = Column(String(50), nullable=False)  # 显示名称
    code = Column(String(50), unique=True, nullable=False)  # 代码标识 (jc, sfc, predict, history, admin)
    icon = Column(String(50))  # 图标（可选）
    sort_order = Column(Integer, default=0)  # 排序
    is_active = Column(Boolean, default=True)
