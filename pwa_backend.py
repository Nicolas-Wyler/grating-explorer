"""Synchronous browser backend: the same scalar solver and saved-result library."""
import io, json, math, os, threading, time
import numpy as np
from PIL import Image
from matplotlib import colormaps
import engine
import library_transfer
from result_store import Store
from js import pwaProgress

STORE=Store('/persist/results')
RESULTS={'overview':None,'detail':None,'annulus':None,'overview_id':None,'detail_id':None,'annulus_id':None}
ACTIVE=None

def progress(f,msg):
    pwaProgress(float(f),str(msg))

def mark_last(kind,run_id,parent=None):
    last=STORE.get_setting('last_runs',{})
    if kind=='overview': last['overview']=run_id;last['detail']=None
    elif kind=='detail':
        last['detail']=run_id
        if parent:
            r=STORE.last_overview_run(parent)
            if r:last['overview']=r
    elif kind=='annulus': last['annulus']=run_id
    STORE.set_setting('last_runs',last)

def effective(kind,raw,region):
    if kind not in ('overview','detail','annulus'):raise ValueError('Unknown calculation type.')
    if kind=='detail':
        if RESULTS['overview'] is None:raise ValueError('Calculate or restore an overview first.')
        p=dict(RESULTS['overview']['params'])
        for k in ('resolution','samples','filter_strength','phase_threshold','source_cutoff','dynamic_range'):p[k]=raw.get(k,p[k])
        p=engine.params(p);g=engine.grid_info(p,region)
    elif kind=='overview':p=engine.params(raw);g=engine.grid_info(p)
    else:p=engine.params(raw);g=None
    return p,g

def plan(kind,p,g):
    if kind in ('overview','detail'):
        hit=STORE.find('heatmap',p,g['bounds']);cost=g['nz']*g['nf']*math.log2(max(g['nf'],2))
        factor=STORE.duration_factor(kind,cost) or STORE.duration_factor('overview',cost) or STORE.duration_factor('detail',cost)
        if factor is None:
            base=engine.params(dict(pitch=48,wavelength=13.5,periods=100,side=10,resolution=768,distance=10,band=.1,band_unit='L'))
            bg=engine.grid_info(base);factor=180/(bg['nz']*bg['nf']*math.log2(bg['nf']))
        return dict(cached=bool(hit),seconds=.5 if hit else max(1,cost*factor),cost=cost,cells=g['cells'],dx=g['dx'],available_resolution=hit['quality2'] if hit else None,match=hit)
    L=p['L'];R=p['distance']*L;dr=p['band']*L if p['band_unit']=='L' else p['band'];w=p['wavelength'];span=(p['periods']-1)*p['pitch']
    if R<=span/2+w/8:raise ValueError('Integration radius must exceed half the source span (plus λ/8).')
    step=min(math.radians(.1),w/(p['angular_samples']*max(L,w)));nt=math.ceil(math.pi/step)+1;nr=math.ceil(dr/(w/p['radial_samples']))+1;cost=nt*nr*p['periods']
    if nt>250000 or nr>5000 or cost>5_000_000_000:raise ValueError('Integration needs too many samples. Reduce the band or source count.')
    hit=STORE.find('annulus',p);factor=STORE.duration_factor('annulus',cost) or (30/(11172*286*100))
    return dict(cached=bool(hit),seconds=.5 if hit else max(1,cost*factor),cost=cost,angles=nt,radii=nr,match=hit)

def status(kind=None):
    data=dict(running=False,progress=1.,message='Ready',job=kind or '',error=None,cache_hit=False,active_params=ACTIVE)
    for view in ('overview','detail','annulus'):
        r=RESULTS[view];data[view]=r is not None
        if r is not None and view in ('overview','detail'):data[view+'_bounds']=r['bounds'];data[view+'_params']=r['params']
    return data

def restore(run_id):
    global ACTIVE
    row=STORE.by_run(run_id);kind=row['kind'];p=json.loads(row['params']);artifact=STORE.artifact(row['artifact_id'])
    if kind=='legacy':return dict(kind='legacy',run_id=run_id,meta=json.loads(row['meta']))
    obj=STORE.load(artifact,p);ACTIVE=p
    if kind=='detail':
        parent=STORE.artifact(row['parent_id']) if row['parent_id'] else None
        if parent is None:raise ValueError('The related overview is missing from the library.')
        RESULTS.update(overview=STORE.load(parent),overview_id=parent['id'],detail=obj,detail_id=artifact['id'])
    elif kind=='overview':RESULTS.update(overview=obj,overview_id=artifact['id'],detail=None,detail_id=None)
    elif kind=='annulus':RESULTS.update(annulus=obj,annulus_id=artifact['id'])
    mark_last(kind,int(run_id),row['parent_id'])
    ov=RESULTS['overview']
    return dict(kind=kind,status=row['status'],params=p,bounds=obj.get('bounds'),overview_bounds=ov['bounds'] if ov else None,overview_params=ov['params'] if ov else None)

def restore_last():
    last=STORE.get_setting('last_runs',{})
    for kind in ('overview','detail','annulus'):
        if last.get(kind):
            try:restore(last[kind])
            except Exception as e:print('Could not restore '+kind+': '+str(e))

def calculate(kind,raw,region):
    global ACTIVE
    p,g=effective(kind,raw,region);est=plan(kind,p,g);ACTIVE=p;parent=RESULTS['overview_id'] if kind=='detail' else None
    cached=False;record=est.get('match');obj=None
    if record:
        progress(.02,'Loading saved result')
        try:obj=STORE.load(record,p);cached=True
        except Exception:record=None;progress(.02,'Saved data unavailable; calculating again')
    if obj is None:
        if kind in ('overview','detail'):obj=engine.heatmap(p,region if kind=='detail' else None,progress,threading.Event())
        else:obj=engine.annulus(p,progress,threading.Event())
        progress(.97,'Saving result to library');record=STORE.save('heatmap' if kind in ('overview','detail') else 'annulus',obj)
    run=STORE.record(kind,record['id'],parent,p,obj.get('bounds'),'cached' if cached else 'computed',None if cached else obj['seconds'],est['cost'])
    RESULTS[kind]=obj;RESULTS[kind+'_id']=record['id']
    if kind=='overview':RESULTS.update(detail=None,detail_id=None)
    mark_last(kind,run,parent)
    result=status(kind);result.update(progress=.99,message='Saving to device',cache_hit=cached,seconds=obj['seconds'])
    if kind in ('overview','detail'):result.update(bounds=obj['bounds'],dx=obj['dx'],cells=obj['cells'])
    else:result.update(angle_samples=obj['nt'],radial_samples=obj['nr'])
    return result

def image(q):
    r=RESULTS.get(q.get('view','overview'))
    if r is None:raise ValueError('No heatmap is available.')
    return engine.image_data(r,q.get('mode','intensity'),q.get('scale','log'),float(q.get('ceiling','100')))

def legend(q):
    v=np.linspace(1,0,256)[:,None];rgb=colormaps['twilight' if q.get('mode')=='phase' else 'magma'](v,bytes=True)[...,:3]
    b=io.BytesIO();Image.fromarray(rgb).resize((24,256)).save(b,format='PNG');return b.getvalue()

def plot(q):
    r=RESULTS['annulus']
    if r is None:raise ValueError('No angular plot is available.')
    return engine.plot_annulus(r,q.get('scale','linear'))

def csvdata():
    r=RESULTS['annulus']
    if r is None:raise ValueError('No angular plot is available.')
    b=io.StringIO();np.savetxt(b,np.column_stack([r['degrees'],r['I'],r['norm'],r['norm']*r['cos'],r['norm']*r['cos']**2]),delimiter=',',header='angle_degrees,area_mean_intensity,normalized_intensity,normalized_intensity_times_cos,normalized_intensity_times_cos_squared',comments='')
    return b.getvalue().encode()

def stats():
    size=0
    for root,dirs,names in os.walk(STORE.root):
        for name in names:
            try:size+=os.stat(os.path.join(root,name)).st_size
            except OSError:pass
    with STORE.connect() as c:runs=c.execute('SELECT count(*) FROM runs').fetchone()[0]
    return dict(bytes=size,runs=runs,path='This browser’s on-device storage')

def dispatch(request):
    d=json.loads(request);name=d['name'];body=d.get('body') or {};q=d.get('query') or {}
    if name=='init':restore_last();value=status()
    elif name=='status':value=status()
    elif name=='ui-state':
        if d.get('body') is not None:STORE.set_setting('ui',body);value={'ok':True}
        else:value=STORE.get_setting('ui',{})
    elif name=='stats':value=stats()
    elif name=='history':value=STORE.history(50,max(0,int(q.get('offset','0'))))
    elif name=='restore':value=restore(body['id'])
    elif name=='estimate':
        p,g=effective(body.get('kind'),body.get('params'),body.get('region'));value=plan(body.get('kind'),p,g);value.pop('match',None)
    elif name=='calculate':value=calculate(body.get('kind'),body.get('params'),body.get('region'))
    elif name=='image':return image(q)
    elif name=='legend':return legend(q)
    elif name=='plot':return plot(q)
    elif name=='csv':return csvdata()
    elif name=='backup':return library_transfer.backup(STORE)
    elif name=='import':value=library_transfer.import_backup(STORE,'/tmp/import.zip')
    elif name=='legacy':
        row=STORE.by_run(int(q['id']));mode=q.get('mode','intensity');scale=q.get('scale','log')
        if row['artifact_kind']!='legacy' or mode not in ('amplitude','intensity','phase') or scale not in ('linear','log'):raise ValueError('Invalid saved snapshot.')
        return (STORE.root/row['filename']/(mode+'_'+('linear' if mode=='phase' else scale)+'.png')).read_bytes()
    else:raise ValueError('Unknown request: '+name)
    return json.dumps(value,allow_nan=False)
