"""Initialize menu tables and default menus."""
import sys
sys.path.insert(0, ".")
from app.db.session import engine
from app.db.base import Base
from app.models.menu import Menu, role_menus
from sqlalchemy.orm import Session

def init():
    # Create tables
    Base.metadata.create_all(bind=engine)
    print("Tables created")
    
    db = Session(engine)
    
    # Default menus
    menus = [
        {"name": "体彩", "code": "jc", "sort_order": 1},
        {"name": "胜负14场", "code": "sfc", "sort_order": 2},
        {"name": "预测比赛", "code": "predict", "sort_order": 3},
        {"name": "历史预测", "code": "history", "sort_order": 4},
        {"name": "管理", "code": "admin", "sort_order": 5},
    ]
    
    for m in menus:
        if not db.query(Menu).filter(Menu.code == m["code"]).first():
            db.add(Menu(**m))
    db.commit()
    print("Menus created")
    
    # Role permissions
    # admin: all menus
    # user: jc, sfc, history (no predict, no admin)
    # guest: jc, sfc, history
    all_menus = db.query(Menu).all()
    admin_codes = ["jc", "sfc", "predict", "history", "admin"]
    user_codes = ["jc", "sfc", "history"]
    
    for menu in all_menus:
        if menu.code in admin_codes:
            db.execute(role_menus.insert().values(role="admin", menu_id=menu.id))
        if menu.code in user_codes:
            db.execute(role_menus.insert().values(role="user", menu_id=menu.id))
    db.commit()
    print("Role permissions set")
    db.close()

if __name__ == "__main__":
    init()
