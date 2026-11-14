import hashlib, hmac, io, re
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

st.set_page_config(page_title='MD Rappresentanze | Clienti', page_icon='📊', layout='wide')
st.markdown('''<style>
.stApp{background:#f7f7f7;color:#202124}.block-container{padding-top:1rem;max-width:1500px}
[data-testid="stHeader"]{background:#087dac}[data-testid="stSidebar"]{background:#f3f4f6!important;border-right:1px solid #d6dbe1}
[data-testid="stSidebar"] *{color:#1f2937!important}.brandline{background:#087dac;color:#fff;padding:12px 18px;border-radius:4px}
.titleband{background:#dff27d;text-align:center;font-size:28px;font-weight:900;font-style:italic;padding:5px;margin:8px 0 16px}
.months{display:grid;grid-template-columns:repeat(12,1fr);gap:2px}.month{background:#3d3d3f;color:#fff;text-align:center;padding:7px 2px;font-size:12px}
.stMetric{background:#fff!important;border:1px solid #d7dde5;border-radius:14px;padding:14px}[data-testid="stMetricLabel"] p{color:#3f4854!important;font-weight:700}[data-testid="stMetricValue"]{color:#4F8FC5!important}
</style>''', unsafe_allow_html=True)
CATS=['Applied','Fancoil','Home','Split','VRF']
NUM=['Fatturato APP','% AC / APP','Fatturato AP','% AC / AP','Fatturato AC','Budget','% AC / Budget','K Lordo AP','K Lordo AC','Quantità AP (No-Acc)','Quantità AC (No-Acc)']

def pwd_hash(p): return hashlib.sha256(p.encode()).hexdigest()
def users():
    raw=st.secrets.get('users',{})
    return {k:dict(v) for k,v in raw.items()}
def authenticate(username,password):
    u=users().get(username.strip())
    if not u:return None
    stored=str(u.get('password_sha256',''))
    return u if hmac.compare_digest(pwd_hash(password),stored) else None

def get_db(): return st.connection('postgresql',type='sql')
def ensure_table():
    with get_db().session as s:
        s.execute('''CREATE TABLE IF NOT EXISTS customer_exports(customer_code TEXT PRIMARY KEY, customer_name TEXT NOT NULL, filename TEXT NOT NULL, content BYTEA NOT NULL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        s.commit()
def save_file(code,name,filename,data):
    ensure_table()
    with get_db().session as s:
        s.execute('''INSERT INTO customer_exports(customer_code,customer_name,filename,content,updated_at) VALUES(:c,:n,:f,:b,CURRENT_TIMESTAMP) ON CONFLICT(customer_code) DO UPDATE SET customer_name=:n,filename=:f,content=:b,updated_at=CURRENT_TIMESTAMP''',dict(c=code,n=name,f=filename,b=data));s.commit()
def load_file(code):
    ensure_table();df=get_db().query('SELECT customer_name,filename,content,updated_at FROM customer_exports WHERE customer_code=:c',params={'c':code},ttl=0)
    if df.empty:return None
    r=df.iloc[0];return r

def read_export(file_obj):
    raw=pd.read_excel(file_obj,sheet_name='Export',header=None,engine='openpyxl');h=next((i for i,r in raw.iterrows() if r.astype(str).str.strip().eq('Market Catalog').any()),None)
    if h is None:raise ValueError('Intestazione Market Catalog non trovata')
    text=' '.join(raw.fillna('').astype(str).values.flatten());m=re.search(r'Customer Full\s+è\s+(.+?)\s+Anno Corrente',text,re.I);code=name=''
    if m: parts=m.group(1).strip().split(' - ',1);code=parts[0].strip();name=parts[-1].strip()
    if hasattr(file_obj,'seek'):file_obj.seek(0)
    df=pd.read_excel(file_obj,sheet_name='Export',header=h,engine='openpyxl');df.columns=[str(x).strip() for x in df.columns];df['Market Catalog']=df['Market Catalog'].astype(str).str.strip();df=df[df['Market Catalog'].isin(CATS+['Total'])].copy()
    for x in NUM:
        if x in df:df[x]=pd.to_numeric(df[x],errors='coerce').fillna(0)
    return df,code,name
def eur(v):return f"€ {v:,.0f}".replace(',','.')
def num(v):return f"{v:,.0f}".replace(',','.')
def pct(v):return f"{v*100:.1f}%".replace('.',',')

def dashboard(df,code,name):
    cats=df[df['Market Catalog'].isin(CATS)].copy();tr=df[df['Market Catalog']=='Total'];total=tr.iloc[0] if not tr.empty else cats.sum(numeric_only=True);ac=float(total.get('Fatturato AC',0));budget=float(total.get('Budget',0));q=float(total.get('Quantità AC (No-Acc)',0))
    st.markdown(f'<div class="brandline"><b>MD Rappresentanze</b>&nbsp;&nbsp; Fatturato &nbsp;&nbsp; Cliente: {code} - {name}</div><div class="titleband">FATTURATO - Prodotti</div>',unsafe_allow_html=True)
    st.markdown('<div class="months">'+''.join(f'<div class="month">{m}</div>' for m in ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'])+'</div>',unsafe_allow_html=True)
    a,b,c,d=st.columns(4);a.metric('Fatturato AC',eur(ac));b.metric('Budget',eur(budget));c.metric('Raggiungimento budget',pct(ac/budget if budget else 0));d.metric('Quantità AC',num(q))
    fig=go.Figure(go.Pie(labels=cats['Market Catalog'],values=cats['Fatturato AC'],hole=.48,marker_colors=['#168af4','#163eae','#ec6b2d','#760080','#dd469d'],textinfo='label+percent'));fig.update_layout(title='Fatturato AC - Catalogo',height=400,paper_bgcolor='white');st.plotly_chart(fig,use_container_width=True)
    out=cats[['Market Catalog','Fatturato AC','Budget','% AC / Budget','K Lordo AC','Quantità AC (No-Acc)']].copy();out['Fatturato AC']=out['Fatturato AC'].map(eur);out['Budget']=out['Budget'].map(eur);out['% AC / Budget']=out['% AC / Budget'].map(pct);out['K Lordo AC']=out['K Lordo AC'].map(pct);out['Quantità AC (No-Acc)']=out['Quantità AC (No-Acc)'].map(num);st.dataframe(out,use_container_width=True,hide_index=True)

st.image('logo_md.png',width=300)
st.session_state.setdefault('user',None)
if not st.session_state.user:
    with st.form('login'):
        un=st.text_input('Nome utente');pw=st.text_input('Password',type='password')
        if st.form_submit_button('Accedi',type='primary'):
            u=authenticate(un,pw)
            if u:st.session_state.user={'username':un,**u};st.rerun()
            else:st.error('Credenziali non valide')
    st.stop()
u=st.session_state.user
with st.sidebar:
    st.image('logo_md.png',use_container_width=True);st.write(f"Utente: **{u['username']}**")
    if st.button('Esci'):st.session_state.user=None;st.rerun()
    page=st.radio('Sezione',['Dashboard','Aggiorna dati'] if u.get('role')=='admin' else ['Dashboard'])
if page=='Aggiorna dati':
    up=st.file_uploader('Carica Excel cliente',type=['xlsx'])
    if up:
        data=up.getvalue()
        try:
            _,code,name=read_export(io.BytesIO(data));save_file(code,name,up.name,data);st.success(f'Dati aggiornati per {code} - {name}')
        except Exception as e:st.error(str(e))
    st.stop()
code=str(u.get('customer_code','')).strip()
if not code:st.error('Nessun cliente associato a questo utente');st.stop()
row=load_file(code)
if row is None:st.warning('Dati non ancora pubblicati per questo cliente');st.stop()
try:
    df,fc,fn=read_export(io.BytesIO(bytes(row['content'])));dashboard(df,fc,fn);st.caption(f"Ultimo aggiornamento dati: {row['updated_at']}")
except Exception as e:st.error(f'Errore lettura dati: {e}')
