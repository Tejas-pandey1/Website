import sqlite3, os
DB = os.path.join(os.path.dirname(__file__), '..', 'database.db')
DB = os.path.normpath(DB)
print('DB path:', DB)
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
print('\n--- image-type cell_values (first 50) ---')
cur.execute("SELECT cv.id, cv.row_id, cv.column_id, cv.value FROM cell_values cv JOIN columns c ON cv.column_id=c.id WHERE c.type='image' LIMIT 50")
rows = cur.fetchall()
print('count:', len(rows))
for r in rows:
    print(r['id'], r['row_id'], r['column_id'], repr(r['value']))

print('\n--- images table (last 50) ---')
cur.execute("SELECT id, manager_id, filename, file_path, mimetype, length(data) as data_len FROM images ORDER BY id DESC LIMIT 50")
rows = cur.fetchall()
print('count:', len(rows))
for r in rows:
    print(r['id'], r['manager_id'], r['filename'], repr(r['file_path']), r['mimetype'], r['data_len'])

print('\n--- example resolved URLs via images table ---')
cur.execute("SELECT id, file_path FROM images LIMIT 20")
for r in cur.fetchall():
    fp = r['file_path'] or ''
    norm = fp.replace('\\\\','/').replace('\\','/')
    print(r['id'], repr(fp), '->', norm)

conn.close()