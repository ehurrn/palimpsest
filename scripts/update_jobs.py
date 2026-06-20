import sqlite3

def update_jobs():
    db_path = '/home/herren/palimpsest-data/db/palimpsest.db'
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE jobs SET priority = 2 WHERE type = 'features' AND state = 'pending';")
        conn.commit()
        print(f"Updated {cursor.rowcount} jobs.")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    update_jobs()
