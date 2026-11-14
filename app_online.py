import hashlib
import hmac
import io
import re

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import text

st.set_page_config(page_title="MD Rappresentanze | Clienti", page_icon="📊", layout="wide")
st.markdown("""<style>
.stApp{background:#f7f7f7;color:#202124}.block-container{padding-top:3.6rem;max-width:1500px}
[data-testid="stHeader"]{background:#087dac}
[data-testid="stSidebar"]{background:#f3f4f6!important;border-right:1px solid #d6dbe1}
[data-testid="stSidebar"] *{color:#1f2937!important}
[data-testid="stSidebar"] button{background:#2f2f2f!important;border:1px solid #2f2f2f!important}
[data-testid="stSidebar"] button p,[data-testid="stSidebar"] button span,[data-testid="stSidebar"] button div{color:#ffffff!important;font-weight:700!important}
[data-testid="stSidebar"] button:hover{background:#087dac!important;border-color:#087dac!important}
.brandline{background:#087dac;color:#fff;padding:12px 18px;border-radius:4px}
.titleband{background:#dff27d;text-align:center;font-size:28px;font-weight:900;font-style:italic;padding:5px;margin:8px 0 16px}
.months{display:grid;grid-template-columns:repeat(12,1fr);gap:2px}
.month{background:#3d3d3f;color:#fff;text-align:center;padding:7px 2px;font-size:12px}
.stMetric{background:#fff!important;border:1px solid #d7dde5;border-radius:14px;padding:14px}
[data-testid="stMetricLabel"] p{color:#3f4854!important;font-weight:700}
[data-testid="stMetricValue"]{color:#4F8FC5!important}
/* Login: etichette e testo digitato sempre neri */
[data-testid="stForm"] label p{color:#000000!important;font-weight:700!important}
[data-testid="stForm"] input{color:#000000!important;background:#ffffff!important;-webkit-text-fill-color:#000000!important}
[data-testid="stForm"] input::placeholder{color:#6b7280!important;opacity:1!important}
[data-testid="stForm"] [data-testid="stTextInput"] div{color:#000000!important}
.login-logo{text-align:center;margin-bottom:12px}.client-card{background:#fff;border:1px solid #d7dde5;border-left:5px solid #087dac;border-radius:9px;padding:14px;margin-bottom:10px}
</style>""", unsafe_allow_html=True)

CATEGORIES = ["Applied", "Fancoil", "Home", "Split", "VRF"]
NUMERIC_COLUMNS = ["Fatturato APP", "% AC / APP", "Fatturato AP", "% AC / AP", "Fatturato AC", "Budget", "% AC / Budget", "K Lordo AP", "K Lordo AC", "Quantità AP (No-Acc)", "Quantità AC (No-Acc)"]
CLIENTS = {
    "M7003635": {"name": "ERRECLIMA SRL", "username": "erreclima"},
    "M7009353": {"name": "M.C. TECNOFORNITURE SRL", "username": "mctecno"},
    "M7011384": {"name": "ADDESSI COMMERCIALE SRL", "username": "addessi"},
    "M7010315": {"name": "Centro Edile De Gregoris S.r.l.", "username": "degregoris"},
}

def password_hash(password): return hashlib.sha256(password.encode("utf-8")).hexdigest()
def configured_users(): return {name: dict(values) for name, values in st.secrets.get("users", {}).items()}
def authenticate(username, password):
    user = configured_users().get(username.strip())
    if not user: return None
    return user if hmac.compare_digest(password_hash(password), str(user.get("password_sha256", ""))) else None

def db_connection(): return st.connection("postgresql", type="sql")
def ensure_table():
    with db_connection().session as session:
        session.execute(text("""CREATE TABLE IF NOT EXISTS customer_exports(customer_code TEXT PRIMARY KEY,customer_name TEXT NOT NULL,filename TEXT NOT NULL,content BYTEA NOT NULL,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""))
        session.commit()
def save_file(code, name, filename, content):
    ensure_table()
    with db_connection().session as session:
        session.execute(text("""INSERT INTO customer_exports(customer_code,customer_name,filename,content,updated_at) VALUES(:code,:name,:filename,:content,CURRENT_TIMESTAMP) ON CONFLICT(customer_code) DO UPDATE SET customer_name=:name,filename=:filename,content=:content,updated_at=CURRENT_TIMESTAMP"""), {"code":code,"name":name,"filename":filename,"content":content})
        session.commit()
def load_file(code):
    ensure_table()
    with db_connection().session as session:
        return session.execute(text("SELECT customer_name,filename,content,updated_at FROM customer_exports WHERE customer_code=:code"), {"code":code}).mappings().first()
def dataset_status():
    ensure_table()
    with db_connection().session as session:
        return {r["customer_code"]:r for r in session.execute(text("SELECT customer_code,customer_name,filename,updated_at FROM customer_exports")).mappings().all()}

def normalize(value):
    if value is None or pd.isna(value): return ""
    return re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()

def read_export(file_obj):
    if hasattr(file_obj, "seek"): file_obj.seek(0)
    raw = pd.read_excel(file_obj, sheet_name="Export", header=None, engine="openpyxl")
    if raw.empty: raise ValueError("Il foglio Export è vuoto.")
    header_idx = next((idx for idx,row in raw.iterrows() if "Market Catalog" in [normalize(v) for v in row.tolist()]), None)
    if header_idx is None: raise ValueError("Intestazione 'Market Catalog' non trovata nel foglio Export.")
    all_text = " ".join(normalize(v) for v in raw.fillna("").to_numpy().flatten())
    match = re.search(r"Customer\s+Full\s*(?:è|=|:)\s*(M\d+)\s*-\s*(.+?)(?=\s+(?:Bill\s+Month(?:\s+Short)?|Anno\s+Corrente|Market\s+Catalog)\s*(?:è|=|:)|$)", all_text, re.I)
    if not match: raise ValueError("Codice cliente non trovato nel filtro 'Customer Full'.")
    code, name = match.group(1).strip().upper(), match.group(2).strip(" -")
    if code not in CLIENTS: raise ValueError(f"Cliente {code} non presente nell'anagrafica autorizzata.")
    if hasattr(file_obj, "seek"): file_obj.seek(0)
    data = pd.read_excel(file_obj, sheet_name="Export", header=header_idx, engine="openpyxl")
    data.columns = [normalize(c) for c in data.columns]
    data["Market Catalog"] = data["Market Catalog"].map(normalize)
    for col in NUMERIC_COLUMNS:
        if col not in data.columns: data[col] = 0.0
        data[col] = pd.to_numeric(data[col], errors="coerce").fillna(0.0)
    data = data[data["Market Catalog"].isin(CATEGORIES + ["Total"])].copy().rename(columns={"Market Catalog":"Voce"})
    if data.empty: raise ValueError("Nessuna categoria valida trovata.")
    return data, code, name

def euro(v): return f"€ {float(v):,.0f}".replace(",", ".")
def number(v): return f"{float(v):,.0f}".replace(",", ".")
def percent(v): return f"{float(v)*100:.1f}%".replace(".", ",")

def dashboard(data, code, name):
    totals=data[data["Voce"]=="Total"]
    detail=data[data["Voce"]!="Total"].copy()
    total=totals.iloc[0] if not totals.empty else detail.sum(numeric_only=True)
    ac,budget,qty=float(total.get("Fatturato AC",0)),float(total.get("Budget",0)),float(total.get("Quantità AC (No-Acc)",0))
    st.markdown(f'<div class="brandline"><b>MD Rappresentanze</b>&nbsp;&nbsp; Fatturato &nbsp;&nbsp; Cliente: {code} - {name}</div><div class="titleband">FATTURATO - Prodotti</div>',unsafe_allow_html=True)
    st.markdown('<div class="months">'+''.join(f'<div class="month">{m}</div>' for m in ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"])+"</div>",unsafe_allow_html=True)
    c1,c2,c3,c4=st.columns(4);c1.metric("Fatturato AC",euro(ac));c2.metric("Budget",euro(budget));c3.metric("Raggiungimento budget",percent(ac/budget if budget else 0));c4.metric("Quantità AC",number(qty))
    fig=go.Figure(go.Pie(labels=detail["Voce"],values=detail["Fatturato AC"],hole=.48,marker_colors=["#168af4","#163eae","#ec6b2d","#760080","#dd469d"],textinfo="label+percent"));fig.update_layout(title="Fatturato AC - Catalogo",height=400,paper_bgcolor="white");st.plotly_chart(fig,use_container_width=True)
    out=detail[["Voce","Fatturato AC","Budget","% AC / Budget","K Lordo AC","Quantità AC (No-Acc)"]].copy();out["Fatturato AC"]=out["Fatturato AC"].map(euro);out["Budget"]=out["Budget"].map(euro);out["% AC / Budget"]=out["% AC / Budget"].map(percent);out["K Lordo AC"]=out["K Lordo AC"].map(percent);out["Quantità AC (No-Acc)"]=out["Quantità AC (No-Acc)"].map(number);st.dataframe(out,use_container_width=True,hide_index=True)

def client_management():
    st.title("Gestione clienti")
    status=dataset_status()
    for code,info in CLIENTS.items():
        record=status.get(code); state="Dati caricati" if record else "Dati non caricati"; update=f" · aggiornato {record['updated_at']}" if record else ""
        st.markdown(f'<div class="client-card"><b>{info["name"]}</b><br>Codice: {code} · Utente: {info["username"]}<br><b>{state}</b>{update}</div>',unsafe_allow_html=True)

def upload_page():
    st.title("Aggiorna dati clienti")
    uploads=st.file_uploader("Carica i file Excel dei clienti",type=["xlsx"],accept_multiple_files=True)
    if st.button("Elabora e salva tutti i file",type="primary",disabled=not uploads):
        ok,errors=[],[]
        for upload in uploads:
            try:
                content=upload.getvalue();_,code,name=read_export(io.BytesIO(content));save_file(code,name,upload.name,content);ok.append(f"{upload.name}: {code} - {name}")
            except Exception as exc: errors.append(f"{upload.name}: {exc}")
        if ok:
            st.success(f"Salvati {len(ok)} file su {len(uploads)}.")
            for item in ok: st.write("• "+item)
        if errors:
            st.error(f"Non salvati {len(errors)} file su {len(uploads)}.")
            for item in errors: st.write("• "+item)

st.session_state.setdefault("user",None)
if not st.session_state.user:
    st.markdown('<div class="login-logo">',unsafe_allow_html=True);st.image("logo_md.png",width=300);st.markdown('</div>',unsafe_allow_html=True)
    with st.form("login"):
        username=st.text_input("Nome utente");password=st.text_input("Password",type="password")
        if st.form_submit_button("Accedi",type="primary"):
            user=authenticate(username,password)
            if user: st.session_state.user={"username":username,**user};st.rerun()
            else: st.error("Credenziali non valide.")
    st.stop()

user=st.session_state.user
with st.sidebar:
    st.image("logo_md.png",use_container_width=True)
    st.write(f"Utente: **{user['username']}**")
    if st.button("Esci"): st.session_state.user=None;st.rerun()
    pages=["Dashboard","Aggiorna dati","Gestione clienti"] if user.get("role")=="admin" else ["Dashboard"]
    page=st.radio("Sezione",pages)

if page=="Aggiorna dati": upload_page();st.stop()
if page=="Gestione clienti": client_management();st.stop()
if user.get("role")=="admin":
    st.info("Seleziona 'Aggiorna dati' per caricare gli Excel oppure 'Gestione clienti' per verificarne lo stato.");st.stop()
code=str(user.get("customer_code","")).strip().upper()
record=load_file(code)
if not record: st.warning("Dati non ancora pubblicati per questo cliente.");st.stop()
try:
    data,found_code,name=read_export(io.BytesIO(bytes(record["content"])))
    if found_code!=code: raise ValueError("Il dataset non corrisponde all'utente collegato.")
    dashboard(data,found_code,name);st.caption(f"Ultimo aggiornamento: {record['updated_at']}")
except Exception as exc: st.error(f"Errore nella lettura dei dati: {exc}")
