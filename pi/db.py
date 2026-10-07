"""
사용자별 EQ 저장·조회 (MariaDB + PyMySQL)

사용법:
    from db import load_eq, save_eq
    eq = load_eq(1)                 # {"low": 0.0, "mid": 0.0, "high": 0.0, "vol": -20.0} 또는 None
    save_eq(1, {"low": 3.0, "mid": 0.0, "high": -2.0, "vol": -10.0})

단독 실행하면 저장 -> 다시 불러오기 테스트를 함:
    python db.py
"""
import pymysql   # pip install pymysql

try:
    import config
except ImportError:
    raise SystemExit("config.py가 없습니다.")

# ---------------- 접속 정보 (MariaDB에서 만든 계정과 같아야 함) ----------------
DB_CONFIG = {
    "host": "localhost",     # DB가 같은 라즈베리파이에 있으면 localhost
    "user": "eq",
    "password": config.DB_PASSWORD,
    "database": "audio",
    "charset": "utf8mb4",
    "autocommit": True,      # 쿼리 실행 즉시 저장 확정
}

# 파이썬 쪽 이름 -> 테이블 컬럼 이름
COLUMNS = {"low": "low_db", "mid": "mid_db", "high": "high_db", "vol": "vol_db"}


def _connect():
    return pymysql.connect(**DB_CONFIG)


def load_eq(user_id):
    """사용자의 EQ를 dict로 돌려줌. 등록되지 않은 사용자면 None"""
    sql = "SELECT low_db, mid_db, high_db, vol_db FROM user_eq WHERE user_id = %s"
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (user_id,))
        row = cur.fetchone()          # 한 줄 가져오기. 없으면 None
    if row is None:
        return None
    return {name: float(value) for name, value in zip(COLUMNS, row)}


def save_eq(user_id, eq, name=None):
    """사용자의 EQ를 저장. 이미 있으면 값만 바꾸고, 없으면 새로 추가"""
    if name is None:
        name = f"user{user_id}"
    sql = """
        INSERT INTO user_eq (user_id, name, low_db, mid_db, high_db, vol_db)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            low_db  = VALUES(low_db),
            mid_db  = VALUES(mid_db),
            high_db = VALUES(high_db),
            vol_db  = VALUES(vol_db)
    """
    values = (user_id, name, eq["low"], eq["mid"], eq["high"], eq["vol"])
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, values)


# ---------------- 단독 실행 테스트 ----------------
if __name__ == "__main__":
    print("1) 저장 전 불러오기:", load_eq(1))

    test_eq = {"low": 3.0, "mid": -1.0, "high": 2.0, "vol": -12.0}
    save_eq(1, test_eq)
    print("2) 저장한 값:      ", test_eq)

    loaded = load_eq(1)
    print("3) 다시 불러온 값:  ", loaded)
    print("   -> 일치" if loaded == test_eq else "   -> 불일치! 확인 필요")

    print("4) 없는 사용자(99):", load_eq(99))
