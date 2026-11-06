import io, math, time
import numpy as np
from scipy.fft import fft, ifft, next_fast_len
from scipy.ndimage import gaussian_filter
from PIL import Image
from matplotlib import colormaps

class Cancelled(Exception): pass

def params(raw):
    try:
        p=float(raw['pitch']); w=float(raw['wavelength']); n=int(raw['periods']); side=float(raw['side']); res=int(raw['resolution']); distance=float(raw['distance']); width=float(raw['band']); unit=str(raw['band_unit']); samples=int(raw.get('samples',8)); strength=float(raw.get('filter_strength',.7)); threshold=float(raw.get('phase_threshold',.25)); cutoff=float(raw.get('source_cutoff',1)); dynamic=float(raw.get('dynamic_range',80)); angular=int(raw.get('angular_samples',10)); radial=int(raw.get('radial_samples',8))
    except (KeyError,TypeError,ValueError): raise ValueError('Enter a valid number in every field.')
    if not all(np.isfinite([p,w,side,distance,width])): raise ValueError('Values must be finite.')
    if p<=0 or w<=0 or side<=0 or distance<=0 or width<=0: raise ValueError('All lengths and distances must be positive.')
    if n<1 or n>2000: raise ValueError('Number of periods must be between 1 and 2000.')
    if res<64 or res>2048: raise ValueError('Heatmap resolution must be 64–2048 pixels per side.')
    if unit not in ('L','unit'): raise ValueError('Invalid band-width unit.')
    if samples<4 or samples>32: raise ValueError('Samples per wavelength must be 4–32.')
    if not np.isfinite([strength,threshold,cutoff,dynamic]).all() or not 0<=strength<=2 or not 0<=threshold<=.95: raise ValueError('Invalid filter settings.')
    if not .5<=cutoff<=20 or not 20<=dynamic<=200: raise ValueError('Source cutoff must be 0.5–20 wavelengths; dB range must be 20–200.')
    if not 4<=angular<=64 or not 4<=radial<=32: raise ValueError('Angular sampling must be 4–64; radial sampling must be 4–32.')
    return dict(pitch=p,wavelength=w,periods=n,side=side,resolution=res,distance=distance,band=width,band_unit=unit,L=n*p,samples=samples,filter_strength=strength,phase_threshold=threshold,source_cutoff=cutoff,dynamic_range=dynamic,angular_samples=angular,radial_samples=radial)

def grid_info(p,region=None):
    L=p['L']; side=p['side']*L
    if region is None: x0,x1,z0,z1=-side/2,side/2,0.,side
    else:
        x0,x1,z0,z1=map(float,region)
        if not np.isfinite([x0,x1,z0,z1]).all() or x1<=x0 or z1<=z0: raise ValueError('Invalid selected region.')
        if z0<0 or z1>side or x0< -side/2 or x1>side/2: raise ValueError('Selection must stay within the overview.')
    zstart=max(z0,p['wavelength']*p['source_cutoff'])
    if z1<=zstart: raise ValueError('The selected region is inside the source-plane cutoff. Select a region farther down or adjust the cutoff.')
    # A dense physical grid: >= 8 samples per wavelength.  Also do not
    # undersample pixels in very small selected regions.
    target=min(p['wavelength']/p['samples'], (x1-x0)/p['resolution'], (z1-z0)/p['resolution'])
    spp=math.ceil(p['pitch']/target); dx=p['pitch']/spp
    nx=math.ceil((x1-x0)/dx)+1; nz=math.floor((z1-zstart)/dx)+1
    span=(p['periods']-1)*spp; nf=next_fast_len(nx+2*span)
    if nx>160000 or nf>220000 or nx*nz>3_000_000_000: raise ValueError('This region requires too many samples. Reduce the viewer size or increase wavelength.')
    return dict(bounds=[x0,x1,z0,z1],zstart=zstart,dx=dx,nx=nx,nz=nz,spp=spp,span=span,nf=nf,cells=nx*nz)

def notify(callback,cancel,frac,msg):
    if cancel.is_set(): raise Cancelled()
    callback(min(.999,max(0.,frac)),msg)

def heatmap(p,region,callback,cancel):
    g=grid_info(p,region);n=p['periods'];p0=p['pitch'];w=p['wavelength'];res=p['resolution'];dx=g['dx'];nx=g['nx'];nz=g['nz'];span=g['span']; nf=g['nf']
    x0,x1,z0,z1=g['bounds']; zstart=g['zstart']
    xs=x0+np.arange(nx)*dx;zs=zstart+np.arange(nz)*dx
    # Guard last sample if the ceil would place it past the view.
    xs=xs[xs<=x1+1e-8*dx];nx=len(xs);g['nx']=nx
    offsets=x0-(n-1)*p0/2+np.arange(nx+span)*dx
    nf=next_fast_len(nx+2*span)
    comb=np.zeros(span+1);comb[::g['spp']]=1;comb_ft=fft(comb,nf,workers=1)
    # Aggregate the dense sampling into actual pixel-area averages.
    xbins=np.minimum(res-1,np.floor((xs-x0)/(x1-x0)*res).astype(int)); xb,starts,counts=np.unique(xbins,return_index=True,return_counts=True)
    ybins=np.minimum(res-1,np.floor((zs-z0)/(z1-z0)*res).astype(int));yc=np.bincount(ybins,minlength=res)
    amp=np.zeros((res,res),np.float64);power=np.zeros_like(amp);qsum=np.zeros((res,res),np.complex128)
    t=time.monotonic();batch=12
    for lo in range(0,nz,batch):
        hi=min(nz,lo+batch);notify(callback,cancel,.01+.94*lo/nz,f'Calculating rows {lo:,}–{hi:,} of {nz:,}')
        r=np.hypot(zs[lo:hi,None],offsets[None,:]);gfield=np.exp((2j*np.pi/w)*r)/r
        field=ifft(fft(gfield,nf,axis=1,workers=1)*comb_ft,axis=1,workers=1)[:,span:span+nx]
        mag=np.abs(field);pwr=mag*mag; unit=np.divide(field,mag,out=np.zeros_like(field),where=mag>0)
        # Each reduction is an area average in x; sum rows into y pixels.
        aa=np.add.reduceat(mag,starts,axis=1)/counts; ii=np.add.reduceat(pwr,starts,axis=1)/counts; qq=np.add.reduceat(unit,starts,axis=1)/counts
        for j,y in enumerate(ybins[lo:hi]):
            amp[y,xb]+=aa[j];power[y,xb]+=ii[j];qsum[y,xb]+=qq[j]
    valid=yc>0; amp[valid]/=yc[valid,None];power[valid]/=yc[valid,None];qsum[valid]/=yc[valid,None]
    # Suppress residual subpixel modulation after area integration. Phase is
    # averaged as complex phasors, not as angles across the wrap.
    sx=max(0.,(x1-x0)/res/(w/4)-1);sy=max(0.,(z1-z0)/res/(w/4)-1)
    if sx>0 or sy>0:
        sig=(min(p['filter_strength'],sy*.25),min(p['filter_strength'],sx*.25));amp=gaussian_filter(amp,sig);power=gaussian_filter(power,sig);qsum=gaussian_filter(qsum.real,sig)+1j*gaussian_filter(qsum.imag,sig)
    # Rows above the model's one-wavelength exclusion remain blank.
    q=np.abs(qsum);ph=np.angle(qsum)
    blank=(z0+(np.arange(res)+.5)*(z1-z0)/res)<w*p['source_cutoff']
    amp[blank]=np.nan;power[blank]=np.nan;ph[blank]=np.nan;q[blank]=0
    notify(callback,cancel,.98,'Preparing heatmap views')
    return dict(amplitude=amp.astype('float32'),intensity=power.astype('float32'),phase=ph.astype('float32'),coherence=q.astype('float32'),bounds=g['bounds'],dx=dx,cells=g['cells'],params=p,seconds=time.monotonic()-t)

def image_data(result,mode,scale='log',ceiling=100.):
    if mode not in ('intensity','amplitude','phase'): raise ValueError('Unknown heatmap view.')
    ceiling=float(ceiling)
    if mode!='phase' and scale=='linear' and (not np.isfinite(ceiling) or ceiling<=0 or ceiling>10000): raise ValueError('Linear ceiling must be greater than 0 and at most 10,000%.')
    a=result[mode];invalid=~np.isfinite(a)
    if mode=='phase':
        ref=float(np.nanmax(result['intensity']))
        invalid=invalid | (result['intensity'] < ref*1e-12)
        f=(a+np.pi)/(2*np.pi);rgb=(colormaps['twilight'](np.nan_to_num(f,nan=0),bytes=True)[...,:3]).copy()
        rgb[(result['coherence']<result['params']['phase_threshold'])|invalid]=[174,181,187]
    else:
        mx=float(np.nanmax(a));f=np.nan_to_num(a/max(mx,1e-300),nan=0)
        if scale=='log':
            factor=10 if mode=='intensity' else 20
            dynamic=result['params']['dynamic_range'];f=np.clip((factor*np.log10(np.maximum(f,1e-20))+dynamic)/dynamic,0,1)
        elif scale=='linear':
            f=f/(ceiling/100.)
        else:
            raise ValueError('Unknown heatmap scale.')
        rgb=colormaps['magma'](np.clip(f,0,1),bytes=True)[...,:3].copy();rgb[invalid]=[224,228,230]
    out=io.BytesIO();Image.fromarray(rgb).save(out,format='PNG');return out.getvalue()

def annulus(p,callback,cancel):
    L=p['L'];r0=p['distance']*L;dr=p['band']*L if p['band_unit']=='L' else p['band'];n=p['periods'];w=p['wavelength'];pitch=p['pitch'];span=(n-1)*pitch
    if r0<=span/2+w/8: raise ValueError('Integration radius must exceed half the source span (plus λ/8) to avoid point-source singularities at grazing angles.')
    # Angular and radial sampling resolve intensity, including near-field cases.
    dtheta=min(np.deg2rad(.1), w/(p['angular_samples']*max(L,w)));nt=math.ceil(np.pi/dtheta)+1
    nr=math.ceil(dr/(w/p['radial_samples']))+1
    if nt>250000 or nr>5000 or nt*nr*n>5_000_000_000: raise ValueError('Integration needs too many samples. Reduce the band or source count.')
    angles=np.linspace(-np.pi/2,np.pi/2,nt);deg=np.degrees(angles);s=np.sin(angles);c=np.cos(angles);radii=np.linspace(r0,r0+dr,nr)
    src=(np.arange(n)-(n-1)/2)*pitch;tot=np.zeros(nt);start=time.monotonic()
    for j,R in enumerate(radii):
        notify(callback,cancel,.02+.9*j/nr,f'Integrating radius {j+1:,} of {nr:,}')
        x=R*s;z=R*c;u=np.zeros(nt,dtype=complex)
        for pos in src:
            rr=np.hypot(x-pos,z);u+=np.exp((2j*np.pi/w)*rr)/rr
        f=.5 if j in (0,nr-1) else 1.;tot+=f*R*np.abs(u)**2
    denom=.5*radii[0]+sum(radii[1:-1])+.5*radii[-1];I=tot/denom;norm=I/max(float(I.max()),1e-300)
    cos=np.maximum(np.cos(angles),0);orders={m:np.degrees(np.arcsin(m*w/pitch)) for m in range(-int(np.floor(pitch/w)),int(np.floor(pitch/w))+1)}
    notify(callback,cancel,.95,'Preparing angular plot')
    return dict(degrees=deg,I=I,norm=norm,cos=cos,orders=orders,r0=r0,dr=dr,nr=nr,nt=nt,params=p,seconds=time.monotonic()-start)

def plot_annulus(a, scale='linear'):
    import matplotlib;matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    t=a['degrees'];I=a['norm'];c=a['cos'];p=a['params']
    fig,ax=plt.subplots(figsize=(12.5,5.8),layout='constrained');fig.patch.set_facecolor('white')
    def display(y):
        return np.maximum(-p['dynamic_range'],10*np.log10(np.maximum(y,1e-30))) if scale=='log' else y
    ax.plot(t,display(I),color='#aebdc4',lw=.95,label='Original');ax.plot(t,display(I*c),color='#7462b5',lw=1,label='× cos θ');ax.plot(t,display(I*c*c),color='#df733c',lw=1.15,label='× cos² θ')
    for m,angle in a['orders'].items():
        if len(a['orders'])<=25:
            ax.axvline(angle,color='#dce3e7',lw=.7,ls=(0,(3,4)))
            ax.text(angle,1.02 if scale=='linear' else 1.0,('−'+str(abs(m)) if m<0 else '+'+str(m) if m else '0'),ha='center',va='bottom',fontsize=9,fontweight='bold')
    ax.set(xlim=(-90,90),ylim=(0,1.08) if scale=='linear' else (-p['dynamic_range'],5),xlabel='Angle from grating normal (degrees)',ylabel='Intensity / unweighted maximum' if scale=='linear' else 'Intensity (dB relative to unweighted maximum)')
    ax.set_xticks(np.arange(-90,91,15));ax.grid(axis='y',color='#e9eef1');ax.spines['top'].set_visible(False);ax.spines['right'].set_visible(False)
    ax.set_title('Area-averaged angular intensity',loc='left',fontweight='bold',fontsize=15,pad=16)
    ax.legend(loc='lower right',ncol=1,frameon=True,framealpha=.94,fontsize=9)
    fig.text(.07,-.01,f"{p['periods']} point sources • pitch {p['pitch']:g} • wavelength {p['wavelength']:g} • radii {a['r0']:g}–{a['r0']+a['dr']:g}",fontsize=9,color='#64747b')
    f=io.BytesIO();fig.savefig(f,format='png',dpi=180,bbox_inches='tight');plt.close(fig);return f.getvalue()
