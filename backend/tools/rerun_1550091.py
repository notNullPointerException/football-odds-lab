import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.db.session import SessionLocal
from prediction.predict import predict_fixture

db = SessionLocal()
try:
    predict_fixture(1550091, db=db)
finally:
    db.close()
