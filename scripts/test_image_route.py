try:
    import sys, os
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
    import app
    c = app.app.test_client()
    resp = c.get('/image/2')
    print('status', resp.status_code)
    print('content-type', resp.headers.get('Content-Type'))
    print('content-length', resp.headers.get('Content-Length'))
    print('body-length', len(resp.data))
    out = 'scripts/out_image_2.bin'
    with open(out, 'wb') as f:
        f.write(resp.data)
    print('wrote', out)
    # Inspect via app.get_db()
    db = app.get_db()
    row = db.execute('SELECT id, manager_id, filename, mimetype, data IS NOT NULL as has_data, length(data) as data_len, file_path FROM images WHERE id=?', (2,)).fetchone()
    # write direct DB inspection to file for debugging
    with open('scripts/image_2_db_info.txt', 'w', encoding='utf-8') as infof:
        infof.write(repr(row))
    print('wrote DB inspection to scripts/image_2_db_info.txt')
except Exception as e:
    import traceback
    traceback.print_exc()
