"""Persistent, lossless output-grid and annular-result storage."""
import json, math, os, sqlite3, tempfile, time, uuid, hashlib, shutil, threading
from pathlib import Path
import numpy as np

VERSION='scalar-point-v1'
class Store:
 def __init__(self,path=None):
    self.root=Path(path or os.environ.get('GRATING_RESULTS_DIR') or (Path.home()/'Library/Application Support/Grating Explorer/Results'))
    self.root.mkdir(parents=True,exist_ok=True);(self.root/'fields').mkdir(exist_ok=True);(self.root/'legacy').mkdir(exist_ok=True)
    self.db=self.root/'results.sqlite3';self.lock=threading.RLock()
    with self.connect() as c:
     c.executescript('''CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, kind TEXT NOT NULL, fingerprint TEXT NOT NULL, quality1 INTEGER NOT NULL, quality2 INTEGER NOT NULL, filename TEXT NOT NULL, meta TEXT NOT NULL, created REAL NOT NULL); CREATE INDEX IF NOT EXISTS artifacts_lookup ON artifacts(kind,fingerprint,quality1,quality2); CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, artifact_id TEXT NOT NULL, parent_id TEXT, params TEXT NOT NULL, bounds TEXT, status TEXT NOT NULL, created REAL NOT NULL, seconds REAL, cost REAL); CREATE INDEX IF NOT EXISTS runs_time ON runs(created DESC); CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);''')
 def connect(self):
    c=sqlite3.connect(self.db,timeout=15);c.row_factory=sqlite3.Row;return c
 def get_setting(self,key,default=None):
    with self.connect() as c:r=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    return json.loads(r['value']) if r else default
 def set_setting(self,key,value):
    encoded=json.dumps(value,allow_nan=False)
    with self.connect() as c:c.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,encoded))
 def last_overview_run(self,artifact_id):
    with self.connect() as c:r=c.execute("SELECT id FROM runs WHERE kind='overview' AND artifact_id=? ORDER BY id DESC LIMIT 1",(artifact_id,)).fetchone()
    return int(r['id']) if r else None
 def number(self,x):return format(float(x),'.15g')
 def heat_fingerprint(self,p,bounds):
    d=dict(algorithm=VERSION,pitch=self.number(p['pitch']),wavelength=self.number(p['wavelength']),periods=p['periods'],bounds=[self.number(v) for v in bounds],filter_strength=self.number(p['filter_strength']),source_cutoff=self.number(p['source_cutoff']))
    return json.dumps(d,sort_keys=True,separators=(',',':'))
 def ann_fingerprint(self,p):
    L=p['L'];band=p['band']*L if p['band_unit']=='L' else p['band']
    d=dict(algorithm=VERSION,pitch=self.number(p['pitch']),wavelength=self.number(p['wavelength']),periods=p['periods'],radius=self.number(p['distance']*L),band=self.number(band))
    return json.dumps(d,sort_keys=True,separators=(',',':'))
 def find(self,kind,p,bounds=None):
    if kind=='heatmap':fp=self.heat_fingerprint(p,bounds);q1=p['samples'];q2=p['resolution']
    else:fp=self.ann_fingerprint(p);q1=p['angular_samples'];q2=p['radial_samples']
    with self.connect() as c:
     rows=c.execute('SELECT * FROM artifacts WHERE kind=? AND fingerprint=? AND quality1>=? AND quality2>=? ORDER BY quality1,quality2,created DESC',(kind,fp,q1,q2)).fetchall()
    for r in rows:
     if (self.root/r['filename']).is_file():return dict(r)
    return None
 def _write(self,artifact_id,arrays):
    dst=self.root/'fields'/f'{artifact_id}.npz';fd,tmp=tempfile.mkstemp(prefix='.saving-',suffix='.npz',dir=dst.parent)
    try:
     with os.fdopen(fd,'wb') as f:np.savez_compressed(f,**arrays);f.flush();os.fsync(f.fileno())
     os.replace(tmp,dst)
    except BaseException:
     try:os.unlink(tmp)
     except OSError:pass
     raise
    return str(dst.relative_to(self.root))
 def save(self,kind,result):
    p=result['params'];now=time.time()
    if kind=='heatmap':
     fp=self.heat_fingerprint(p,result['bounds']);q1=p['samples'];q2=p['resolution']
     arrays={k:result[k] for k in ('amplitude','intensity','phase','coherence')}
     meta={k:result[k] for k in ('bounds','dx','cells','params','seconds')}
    else:
     fp=self.ann_fingerprint(p);q1=p['angular_samples'];q2=p['radial_samples']
     arrays={k:result[k] for k in ('degrees','I','norm','cos')}
     meta={k:result[k] for k in ('r0','dr','nr','nt','params','seconds')};meta['orders']={str(k):v for k,v in result['orders'].items()}
    ident=hashlib.sha256((fp+f'|{q1}|{q2}|{uuid.uuid4().hex}').encode()).hexdigest()
    with self.lock:
     filename=self._write(ident,arrays)
     with self.connect() as c:c.execute('INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?,?,?,?,?)',(ident,kind,fp,q1,q2,filename,json.dumps(meta),now))
     return dict(id=ident,kind=kind,fingerprint=fp,quality1=q1,quality2=q2,filename=filename,meta=json.dumps(meta),created=now)
 def load(self,record,params=None):
    meta=json.loads(record['meta']);p=params or meta['params'];filename=self.root/record['filename']
    try:
     with np.load(filename,allow_pickle=False) as z: arrays={k:z[k] for k in z.files}
    except Exception as e:raise ValueError(f'Saved result could not be read: {e}') from e
    if record['kind']=='heatmap':
     stored_resolution=int(meta['params']['resolution'])
     if stored_resolution<int(p['resolution']):raise ValueError('Saved heatmap resolution is too low.')
     for k in ('amplitude','intensity','phase','coherence'):
      if k not in arrays or arrays[k].shape!=(stored_resolution,stored_resolution):raise ValueError('Saved heatmap has an invalid shape.')
     effective=dict(p,resolution=stored_resolution)
     return dict(**arrays,bounds=meta['bounds'],dx=meta['dx'],cells=meta['cells'],params=effective,seconds=meta['seconds'])
    if record['kind']=='annulus':
     for k in ('degrees','I','norm','cos'):
      if k not in arrays or arrays[k].ndim!=1:raise ValueError('Saved angular profile is incomplete.')
     return dict(**arrays,orders={int(k):v for k,v in meta['orders'].items()},r0=meta['r0'],dr=meta['dr'],nr=meta['nr'],nt=meta['nt'],params=p,seconds=meta['seconds'])
    raise ValueError('Snapshot has no numerical result to load.')
 def record(self,kind,artifact_id,parent,p,bounds,status,seconds=None,cost=None):
    with self.connect() as c:
     cur=c.execute('INSERT INTO runs(kind,artifact_id,parent_id,params,bounds,status,created,seconds,cost) VALUES (?,?,?,?,?,?,?,?,?)',(kind,artifact_id,parent,json.dumps(p),json.dumps(bounds) if bounds is not None else None,status,time.time(),seconds,cost));return cur.lastrowid
 def history(self,limit=50,offset=0):
    with self.connect() as c:
     rows=c.execute('SELECT r.*, a.kind AS artifact_kind,a.filename,a.meta FROM runs r JOIN artifacts a ON a.id=r.artifact_id ORDER BY r.id DESC LIMIT ? OFFSET ?', (limit,offset)).fetchall()
    out=[]
    for r in rows:
     d=dict(r);d['params']=json.loads(d['params']);d['bounds']=json.loads(d['bounds']) if d['bounds'] else None;meta=json.loads(d.pop('meta'));d['legacy_source']=meta.get('source') if d['artifact_kind']=='legacy' else None;out.append(d)
    return out
 def by_run(self,run_id):
    with self.connect() as c:
     r=c.execute('SELECT r.*,a.kind AS artifact_kind,a.fingerprint,a.quality1,a.quality2,a.filename,a.meta,a.created AS artifact_created FROM runs r JOIN artifacts a ON a.id=r.artifact_id WHERE r.id=?',(int(run_id),)).fetchone()
    if not r:raise ValueError('Saved run not found.')
    return dict(r)
 def artifact(self,ident):
    with self.connect() as c:r=c.execute('SELECT * FROM artifacts WHERE id=?',(ident,)).fetchone()
    return dict(r) if r else None
 def stats(self):
    size=0;files=0
    for root,dirs,names in os.walk(self.root):
     for name in names:
      try:st=os.stat(os.path.join(root,name));size+=st.st_blocks*512;files+=1
      except OSError:pass
    with self.connect() as c:
     runs=c.execute('SELECT count(*) FROM runs').fetchone()[0];arts=c.execute('SELECT count(*) FROM artifacts').fetchone()[0]
    return dict(bytes=size,files=files,runs=runs,artifacts=arts,free_bytes=shutil.disk_usage(self.root).free,path=str(self.root))
 def duration_factor(self,kind,target_cost):
    with self.connect() as c:rows=c.execute("SELECT seconds,cost FROM runs WHERE kind=? AND status='computed' AND seconds>0 AND cost>=? AND cost<=? ORDER BY id DESC LIMIT 8",(kind,target_cost/10,target_cost*10)).fetchall()
    return float(np.median([r['seconds']/r['cost'] for r in rows])) if rows else None
 def import_legacy(self,source,metadata,images):
    fingerprint='legacy:'+hashlib.sha256((source+json.dumps(metadata,sort_keys=True)).encode()).hexdigest()
    with self.connect() as c:
     old=c.execute("SELECT id FROM artifacts WHERE kind='legacy' AND fingerprint=?",(fingerprint,)).fetchone()
    if old:return old['id']
    ident='legacy-'+uuid.uuid4().hex;directory=self.root/'legacy'/ident;directory.mkdir()
    for name,data in images.items():
     with open(directory/name,'wb') as f:f.write(data)
    meta=dict(source=source,params=metadata.get('params'),bounds=metadata.get('bounds'),modes=sorted(images))
    with self.connect() as c:
     c.execute('INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?)',(ident,'legacy',fingerprint,0,0,str(directory.relative_to(self.root)),json.dumps(meta),time.time()))
    p=metadata.get('params') or {};self.record('legacy',ident,None,p,metadata.get('bounds'),'snapshot')
    return ident
