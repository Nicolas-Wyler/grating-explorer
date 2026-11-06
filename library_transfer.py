"""Portable backups and non-destructive merging of results libraries."""
import hashlib, io, json, os, shutil, sqlite3, tempfile, zipfile
from pathlib import Path

MAX_UNPACKED=1_073_741_824

def backup(store):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED,compresslevel=3) as z:
        for base,dirs,files in os.walk(store.root):
            for name in files:
                if name.endswith(('-journal','-wal','-shm')) or name.startswith('.saving-'):continue
                path=Path(base)/name;z.write(path,path.relative_to(store.root).as_posix())
    return out.getvalue()

def safe_path(root,name):
    p=Path(name)
    if p.is_absolute() or '..' in p.parts or not p.parts:raise ValueError('The backup contains an unsafe file path.')
    resolved=(root/p).resolve()
    if root.resolve() not in resolved.parents:raise ValueError('The backup contains an unsafe file path.')
    return resolved

def import_backup(store,archive):
    with tempfile.TemporaryDirectory(prefix='grating-import-') as temp:
        root=Path(temp)
        try:
            with zipfile.ZipFile(archive) as z:
                entries=z.infolist()
                if len(entries)>20000 or sum(x.file_size for x in entries)>MAX_UNPACKED:raise ValueError('Backup is too large to import in the browser (1 GiB limit).')
                for info in entries:
                    if info.is_dir():continue
                    dest=safe_path(root,info.filename);dest.parent.mkdir(parents=True,exist_ok=True)
                    with z.open(info) as source,open(dest,'wb') as target:shutil.copyfileobj(source,target,1024*1024)
        except (zipfile.BadZipFile,OSError) as e:raise ValueError('Could not read that backup: '+str(e)) from e
        db=root/'results.sqlite3'
        if not db.is_file():raise ValueError('This is not a Grating Explorer results backup.')
        source=sqlite3.connect(str(db));source.row_factory=sqlite3.Row
        try:
            if source.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('The backup database is damaged.')
            artifacts=[dict(x) for x in source.execute('SELECT * FROM artifacts')]
            runs=[dict(x) for x in source.execute('SELECT * FROM runs ORDER BY id')]
            for a in artifacts:
                path=safe_path(root,a['filename'])
                if not path.exists():raise ValueError('The backup is missing saved result data: '+a['filename'])
            imported_artifacts=0;imported_runs=0
            with store.lock:
                with store.connect() as target:
                    for a in artifacts:
                        old=target.execute('SELECT * FROM artifacts WHERE id=?',(a['id'],)).fetchone()
                        if old:
                            if old['fingerprint']!=a['fingerprint'] or old['kind']!=a['kind']:raise ValueError('A saved-result identifier conflicts with this backup.')
                            continue
                        dest=safe_path(store.root,a['filename']);dest.parent.mkdir(parents=True,exist_ok=True);src=safe_path(root,a['filename'])
                        if src.is_dir():shutil.copytree(src,dest,dirs_exist_ok=True)
                        else:shutil.copy2(src,dest)
                        target.execute('INSERT INTO artifacts (id,kind,fingerprint,quality1,quality2,filename,meta,created) VALUES (?,?,?,?,?,?,?,?)',tuple(a[k] for k in ('id','kind','fingerprint','quality1','quality2','filename','meta','created')));imported_artifacts+=1
                    for r in runs:
                        found=target.execute('SELECT 1 FROM runs WHERE kind=? AND artifact_id=? AND created=? AND params=? AND status=?',(r['kind'],r['artifact_id'],r['created'],r['params'],r['status'])).fetchone()
                        if found:continue
                        target.execute('INSERT INTO runs (kind,artifact_id,parent_id,params,bounds,status,created,seconds,cost) VALUES (?,?,?,?,?,?,?,?,?)',tuple(r[k] for k in ('kind','artifact_id','parent_id','params','bounds','status','created','seconds','cost')));imported_runs+=1
            return dict(artifacts=imported_artifacts,runs=imported_runs)
        finally:source.close()
