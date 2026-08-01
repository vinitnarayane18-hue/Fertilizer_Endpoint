import sqlite3
from pathlib import Path

paths = [
    Path(__file__).resolve().parent / 'fungicide.db',
    Path(__file__).resolve().parent.parent / 'fungicide.db',
    Path('D:/local_db/local_db_I/fungicide.db'),
]

for p in paths:
    print('\nChecking:', p)
    if not p.exists():
        print('  (not found)')
        continue
    con = sqlite3.connect(p)
    tables = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print('  tables:', tables)
    if tables:
        print('  schema:')
        for r in con.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"):
            print('   -', r[0])
            print('     ', r[1])
    con.close()
