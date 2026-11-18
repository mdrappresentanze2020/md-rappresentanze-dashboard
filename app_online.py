import hashlib
import hmac
import io
import re
import json
import base64
import urllib.parse
import urllib.request

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import text

st.set_page_config(page_title="MD Rappresentanze | Clienti", page_icon="📊", layout="wide")
st.markdown("""<style>
.stApp{background:#f7f7f7;color:#202124}.block-container{padding-top:3.6rem;max-width:1500px}
[data-testid="stHeader"]{background:#087dac}
[data-testid="stSidebar"]{background:#ffffff!important;border-right:1px solid #e5e7eb}
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
/* Testi dashboard sempre visibili su sfondo chiaro */
[data-testid="stMain"] h1,[data-testid="stMain"] h2,[data-testid="stMain"] h3,[data-testid="stMain"] p,[data-testid="stMain"] strong{color:#202124!important}
[data-testid="stExpander"] summary,[data-testid="stExpander"] summary *{color:#202124!important;font-weight:700!important}
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
def ensure_targets_table():
    with db_connection().session as session:
        session.execute(text("""CREATE TABLE IF NOT EXISTS customer_targets(customer_code TEXT PRIMARY KEY,super_pre_split NUMERIC NOT NULL DEFAULT 0,super_pre_aqua NUMERIC NOT NULL DEFAULT 0,target_annuale_hvac NUMERIC NOT NULL DEFAULT 0,target_q1_hvac NUMERIC NOT NULL DEFAULT 0,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""))
        session.commit()
def load_targets(code=None):
    ensure_targets_table()
    with db_connection().session as session:
        if code:
            row=session.execute(text("SELECT customer_code,super_pre_split,super_pre_aqua,target_annuale_hvac,target_q1_hvac,updated_at FROM customer_targets WHERE customer_code=:code"), {"code":code}).mappings().first()
            return row or {}
        rows=session.execute(text("SELECT customer_code,super_pre_split,super_pre_aqua,target_annuale_hvac,target_q1_hvac,updated_at FROM customer_targets")).mappings().all()
    return {r["customer_code"]:r for r in rows}
def save_targets(code, super_split, super_aqua, annuale, q1):
    ensure_targets_table()
    with db_connection().session as session:
        session.execute(text("""INSERT INTO customer_targets(customer_code,super_pre_split,super_pre_aqua,target_annuale_hvac,target_q1_hvac,updated_at) VALUES(:code,:split,:aqua,:annuale,:q1,CURRENT_TIMESTAMP) ON CONFLICT(customer_code) DO UPDATE SET super_pre_split=:split,super_pre_aqua=:aqua,target_annuale_hvac=:annuale,target_q1_hvac=:q1,updated_at=CURRENT_TIMESTAMP"""), {"code":code,"split":float(super_split),"aqua":float(super_aqua),"annuale":float(annuale),"q1":float(q1)})
        session.commit()
def load_google_document(customer_code, order_number, document_type="conferma"):
    config = st.secrets.get("google_pdf", {})
    webapp_url = str(config.get("webapp_url", "")).strip()
    token = str(config.get("token", "")).strip()
    if not webapp_url or not token:
        return [], "Configurazione Google PDF non disponibile."
    params = urllib.parse.urlencode({
        "cliente": str(customer_code).strip().upper(),
        "ordine": str(order_number).strip().upper(),
        "tipo": str(document_type).strip().lower(),
        "token": token,
    })
    url = webapp_url + ("&" if "?" in webapp_url else "?") + params
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "MD-Rappresentanze-Dashboard/1.0"})
        with urllib.request.urlopen(request, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("ok") and payload.get("trovato"):
            raw_files = payload.get("files") or []
            if not raw_files and payload.get("pdf"):
                raw_files = [{"filename": payload.get("filename") or f"{order_number}.pdf", "pdf": payload.get("pdf")}]
            documents = []
            for item in raw_files:
                if item.get("pdf"):
                    documents.append({
                        "filename": item.get("filename") or f"{order_number}.pdf",
                        "content": base64.b64decode(item["pdf"]),
                        "source": "Google Drive",
                    })
            return documents, None
        if payload.get("trovato") is False:
            return [], None
        return [], payload.get("errore") or "Risposta Google PDF non valida."
    except Exception as exc:
        return [], f"Collegamento Google PDF non disponibile: {exc}"

def load_google_order_pdf(customer_code, order_number):
    documents, error = load_google_document(customer_code, order_number, "conferma")
    return (documents[0] if documents else None), error

def load_google_ddt(customer_code, order_number):
    return load_google_document(customer_code, order_number, "ddt")

def ensure_order_pdfs_table():
    with db_connection().session as session:
        session.execute(text("""CREATE TABLE IF NOT EXISTS order_pdfs(customer_code TEXT NOT NULL,order_number TEXT NOT NULL,filename TEXT NOT NULL,content BYTEA NOT NULL,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(customer_code,order_number))"""))
        session.commit()
def save_order_pdf(customer_code, order_number, filename, content):
    ensure_order_pdfs_table()
    with db_connection().session as session:
        session.execute(text("""INSERT INTO order_pdfs(customer_code,order_number,filename,content,updated_at) VALUES(:code,:order_number,:filename,:content,CURRENT_TIMESTAMP) ON CONFLICT(customer_code,order_number) DO UPDATE SET filename=:filename,content=:content,updated_at=CURRENT_TIMESTAMP"""), {"code":customer_code,"order_number":str(order_number),"filename":filename,"content":content})
        session.commit()
def load_order_pdf(customer_code, order_number):
    ensure_order_pdfs_table()
    with db_connection().session as session:
        return session.execute(text("SELECT filename,content,updated_at FROM order_pdfs WHERE customer_code=:code AND order_number=:order_number"), {"code":customer_code,"order_number":str(order_number)}).mappings().first()
def pdf_status():
    ensure_order_pdfs_table()
    with db_connection().session as session:
        return session.execute(text("SELECT customer_code,order_number,filename,updated_at FROM order_pdfs ORDER BY customer_code,order_number")).mappings().all()
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

def run_db_with_retry(operation):
    connection = db_connection()
    try:
        return operation(connection)
    except Exception:
        try:
            connection.reset()
        except Exception:
            pass
        return operation(db_connection())

def ensure_addendum_table():
    def operation(connection):
        with connection.session as session:
            session.execute(text("""CREATE TABLE IF NOT EXISTS addendum_orders(customer_code TEXT NOT NULL,product_code TEXT NOT NULL,ordered_qty INTEGER NOT NULL DEFAULT 0,list_price NUMERIC NOT NULL DEFAULT 0,promo_price NUMERIC NOT NULL DEFAULT 0,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(customer_code,product_code))"""))
            session.execute(text("ALTER TABLE addendum_orders ADD COLUMN IF NOT EXISTS list_price NUMERIC NOT NULL DEFAULT 0"))
            session.execute(text("ALTER TABLE addendum_orders ADD COLUMN IF NOT EXISTS promo_price NUMERIC NOT NULL DEFAULT 0"))
            session.execute(text("ALTER TABLE addendum_orders ADD COLUMN IF NOT EXISTS gift_value NUMERIC NOT NULL DEFAULT 0"))
            session.execute(text("ALTER TABLE addendum_orders ADD COLUMN IF NOT EXISTS quantity_discount NUMERIC NOT NULL DEFAULT 0"))
            session.commit()
    return run_db_with_retry(operation)

def load_addendum_orders(customer_code):
    ensure_addendum_table()
    def operation(connection):
        with connection.session as session:
            rows = session.execute(text("SELECT product_code,ordered_qty,list_price,promo_price,gift_value,quantity_discount FROM addendum_orders WHERE customer_code=:code"), {"code":customer_code}).mappings().all()
            return {r["product_code"]: {"qty":int(r["ordered_qty"] or 0),"list_price":float(r["list_price"] or 0),"promo_price":float(r["promo_price"] or 0),"gift_value":float(r["gift_value"] or 0),"quantity_discount":float(r["quantity_discount"] or 0)} for r in rows}
    return run_db_with_retry(operation)

def save_addendum_orders(customer_code, values):
    ensure_addendum_table()
    def operation(connection):
        with connection.session as session:
            for product_code, item in values.items():
                session.execute(text("""INSERT INTO addendum_orders(customer_code,product_code,ordered_qty,list_price,promo_price,gift_value,quantity_discount,updated_at) VALUES(:code,:product,:qty,:list_price,:promo_price,:gift_value,:quantity_discount,CURRENT_TIMESTAMP) ON CONFLICT(customer_code,product_code) DO UPDATE SET ordered_qty=:qty,list_price=:list_price,promo_price=:promo_price,gift_value=:gift_value,quantity_discount=:quantity_discount,updated_at=CURRENT_TIMESTAMP"""), {"code":customer_code,"product":product_code,"qty":int(item["qty"]),"list_price":float(item["list_price"]),"promo_price":float(item["promo_price"]),"gift_value":float(item.get("gift_value",0)),"quantity_discount":float(item.get("quantity_discount",0))})
            session.commit()
    return run_db_with_retry(operation)

def normalize(value):
    if value is None or pd.isna(value): return ""
    return re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()

def extract_customer(raw):
    all_text = " ".join(normalize(v) for v in raw.fillna("").to_numpy().flatten())
    match = re.search(
        r"Customer\s+Full\s*(?:è|=|:)\s*(M\d+)\s*-\s*(.+?)(?=\s+(?:Filter\s+Bill|Bill-Deliv|Bill\s+Month|Anno\s+Corrente|Market\s+Catalog)|$)",
        all_text, re.I
    )
    if not match:
        raise ValueError("Codice cliente non trovato nel filtro 'Customer Full'.")
    code = match.group(1).strip().upper()
    name = match.group(2).strip(" -")
    if code not in CLIENTS:
        raise ValueError(f"Cliente {code} non presente nell'anagrafica autorizzata.")
    return code, name

def read_export(file_obj):
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    raw = pd.read_excel(file_obj, sheet_name="Export", header=None, engine="openpyxl")
    if raw.empty:
        raise ValueError("Il foglio Export è vuoto.")
    code, name = extract_customer(raw)

    header_idx = next((idx for idx, row in raw.iterrows()
                       if "Cliente" in [normalize(v) for v in row.tolist()]
                       and "Catalogo" in [normalize(v) for v in row.tolist()]
                       and "Numero Ordine" in [normalize(v) for v in row.tolist()]
                       and "Famiglia Prodotto" in [normalize(v) for v in row.tolist()]), None)
    if header_idx is None:
        raise ValueError("Intestazioni Cliente, Catalogo, Numero Ordine e Famiglia Prodotto non trovate.")

    header_values = [normalize(v) for v in raw.iloc[header_idx].tolist()]
    has_range = "Range" in header_values
    value_start = 5 if has_range else 4

    records = []
    current_catalog = ""
    current_order = ""
    current_family = ""

    for r in range(header_idx + 1, len(raw)):
        row = raw.iloc[r].tolist()
        row += [None] * max(0, value_start + 11 - len(row))
        client = normalize(row[0])
        catalog = normalize(row[1])
        order_number = normalize(row[2])
        family = normalize(row[3])
        range_value = normalize(row[4]) if has_range else ""

        if client.startswith("Filtri applicati"):
            break
        if catalog and catalog != "Total":
            current_catalog = catalog
        if order_number and order_number != "Total":
            current_order = order_number
            current_family = ""
        if family and family != "Total":
            current_family = family
        if not client and not catalog and not order_number and not family and not range_value:
            continue

        # Gerarchia del nuovo export:
        # Cliente > Catalogo > Numero Ordine > Famiglia Prodotto > Range.
        if client == "Total" and not catalog and not order_number:
            level, label = "grand_total", "Total"
        elif catalog == "Total" and not order_number:
            level, label = "grand_total", "Total"
        elif order_number == "Total" and not family:
            level, label = "catalog", current_catalog
        elif order_number and order_number != "Total" and family == "Total":
            level, label = "order", order_number
        elif family and family != "Total" and range_value == "Total":
            level, label = "family", family
        elif has_range and range_value and range_value != "Total":
            level, label = "range", range_value
        elif (not has_range) and family and family != "Total":
            level, label = "family", family
        else:
            continue

        nums = [pd.to_numeric(row[i], errors="coerce") for i in range(value_start, value_start + 11)]
        nums = [0.0 if pd.isna(x) else float(x) for x in nums]
        records.append({
            "Livello": level,
            "Catalogo": "Total" if level == "grand_total" else current_catalog,
            "Numero Ordine": current_order if level in ["family", "range"] else (label if level == "order" else ""),
            "Famiglia Prodotto": current_family if level == "range" else (label if level == "family" else ""),
            "Range": label if level == "range" else "",
            "Spedito Q1": nums[0], "Spedito Q2": nums[1], "Spedito Q3": nums[2], "Spedito Q4": nums[3], "Spedito Totale": nums[4],
            "Da Spedire Q1": nums[5], "Da Spedire Q2": nums[6], "Da Spedire Q3": nums[7], "Da Spedire Q4": nums[8], "Da Spedire Totale": nums[9],
            "Totale Complessivo": nums[10]
        })

    data = pd.DataFrame(records)
    if data.empty:
        raise ValueError("Nessuna riga dettagliata valida trovata.")
    return data, code, name

def euro(v):
    return f"€ {float(v):,.0f}".replace(",", ".")

def dashboard(data, code, name):
    grand = data[data["Livello"] == "grand_total"]
    if grand.empty:
        raise ValueError("Riga totale complessivo non trovata.")
    total = grand.iloc[-1]
    catalogs = data[data["Livello"] == "catalog"].copy()
    families = data[data["Livello"] == "family"].copy()

    spedito = float(total["Spedito Totale"])
    da_spedire = float(total["Da Spedire Totale"])
    complessivo = float(total["Totale Complessivo"])
    incidenza = da_spedire / complessivo if complessivo else 0

    st.markdown(
        f'<div class="brandline"><b>MD Rappresentanze</b>&nbsp;&nbsp; Cliente: {code} - {name}</div>'
        '<div class="titleband">FATTURATO - Spedito e Da Spedire</div>', unsafe_allow_html=True)

    c1,c2,c3,c4 = st.columns(4)
    c1.metric("Spedito", euro(spedito))
    c2.metric("Da Spedire", euro(da_spedire))
    c3.metric("Totale complessivo", euro(complessivo))
    c4.metric("Incidenza Da Spedire", f"{incidenza*100:.1f}%".replace(".", ","))
    targets = load_targets(code)
    st.subheader("Target")
    target_split=float(targets.get("super_pre_split",0) or 0)
    target_aqua=float(targets.get("super_pre_aqua",0) or 0)
    target_annuale=float(targets.get("target_annuale_hvac",0) or 0)
    target_q1=float(targets.get("target_q1_hvac",0) or 0)

    hvac_catalogs = catalogs[catalogs["Catalogo"].isin(["Home","Split","Fancoil"])]
    consuntivo_annuale = float(hvac_catalogs["Totale Complessivo"].sum())
    consuntivo_q1 = float((hvac_catalogs["Spedito Q1"] + hvac_catalogs["Da Spedire Q1"]).sum())

    split_catalogs = catalogs[catalogs["Catalogo"].astype(str).str.strip().str.casefold() == "split"]
    consuntivo_split_q1 = float((split_catalogs["Spedito Q1"] + split_catalogs["Da Spedire Q1"]).sum())
    consuntivo_split_q2 = float((split_catalogs["Spedito Q2"] + split_catalogs["Da Spedire Q2"]).sum())
    consuntivo_split = consuntivo_split_q1 + consuntivo_split_q2

    # Super Pre Aqua: Q1 + Q2 delle famiglie prodotto che contengono "aqua".
    aqua_families = families[families["Famiglia Prodotto"].astype(str).str.contains("aqua", case=False, na=False)]
    consuntivo_aqua_q1 = float((aqua_families["Spedito Q1"] + aqua_families["Da Spedire Q1"]).sum())
    consuntivo_aqua_q2 = float((aqua_families["Spedito Q2"] + aqua_families["Da Spedire Q2"]).sum())
    consuntivo_aqua = consuntivo_aqua_q1 + consuntivo_aqua_q2

    def raggiungimento(consuntivo, target):
        return None if target <= 0 else (consuntivo / target) * 100
    def pct_label(value):
        return "Target da definire" if value is None else f"{value:.1f}%".replace(".", ",")

    r_split = raggiungimento(consuntivo_split, target_split)
    r_aqua = raggiungimento(consuntivo_aqua, target_aqua)
    r_annuale = raggiungimento(consuntivo_annuale, target_annuale)
    r_q1 = raggiungimento(consuntivo_q1, target_q1)

    t1,t2,t3,t4 = st.columns(4)
    t1.metric("Super Pre Split", euro(target_split), delta=pct_label(r_split), delta_color="off")
    t2.metric("Super Pre Aqua", euro(target_aqua), delta=pct_label(r_aqua), delta_color="off")
    t3.metric("Target Q1 Home + Split + Fancoil", euro(target_q1), delta=pct_label(r_q1), delta_color="off")
    t4.metric("Target annuale Home + Split + Fancoil", euro(target_annuale), delta=pct_label(r_annuale), delta_color="off")

    p_split, p_aqua, p_q1, p_annuale = st.columns(4)
    with p_split:
        st.markdown(f"<div style='text-align:center;font-weight:700;color:#202124'>Consuntivo Split Q1 + Q2<br>{euro(consuntivo_split)}<br><span style='font-size:12px'>Q1 {euro(consuntivo_split_q1)} · Q2 {euro(consuntivo_split_q2)}</span></div>", unsafe_allow_html=True)
        st.progress(min(max((r_split or 0)/100,0),1), text=f"Raggiungimento Split: {pct_label(r_split)}")
    with p_aqua:
        st.markdown(f"<div style='text-align:center;font-weight:700;color:#202124'>Consuntivo Aqua Q1 + Q2<br>{euro(consuntivo_aqua)}<br><span style='font-size:12px'>Q1 {euro(consuntivo_aqua_q1)} · Q2 {euro(consuntivo_aqua_q2)}</span></div>", unsafe_allow_html=True)
        st.progress(min(max((r_aqua or 0)/100,0),1), text=f"Raggiungimento Aqua: {pct_label(r_aqua)}")
    with p_q1:
        st.markdown(f"<div style='text-align:center;font-weight:700;color:#202124'>Consuntivo Q1 Home + Split + Fancoil<br>{euro(consuntivo_q1)}</div>", unsafe_allow_html=True)
        st.progress(min(max((r_q1 or 0)/100,0),1), text=f"Raggiungimento Q1: {pct_label(r_q1)}")
    with p_annuale:
        st.markdown(f"<div style='text-align:center;font-weight:700;color:#202124'>Consuntivo Home + Split + Fancoil<br>{euro(consuntivo_annuale)}</div>", unsafe_allow_html=True)
        st.progress(min(max((r_annuale or 0)/100,0),1), text=f"Raggiungimento annuale: {pct_label(r_annuale)}")

    st.subheader("Andamento trimestrale")
    qdata = pd.DataFrame({
        "Trimestre": ["Q1","Q2","Q3","Q4"],
        "Spedito": [total[f"Spedito Q{i}"] for i in range(1,5)],
        "Da Spedire": [total[f"Da Spedire Q{i}"] for i in range(1,5)]
    })
    fig_q = go.Figure()
    fig_q.add_bar(name="Spedito", x=qdata["Trimestre"], y=qdata["Spedito"], marker_color="#4F8FC5", text=[euro(v) for v in qdata["Spedito"]], textposition="inside", textfont=dict(color="white",size=12))
    fig_q.add_bar(name="Da Spedire", x=qdata["Trimestre"], y=qdata["Da Spedire"], marker_color="#D6E86B", text=[euro(v) if v else "" for v in qdata["Da Spedire"]], textposition="inside", textfont=dict(color="#202124",size=11))
    fig_q.update_layout(barmode="stack", height=320, paper_bgcolor="white", plot_bgcolor="white", yaxis_title="Euro", legend_orientation="h", legend=dict(font=dict(color="#202124",size=12),bgcolor="rgba(255,255,255,0.9)"), margin=dict(l=20,r=15,t=20,b=20), font=dict(color="#202124"), xaxis=dict(tickfont=dict(color="#202124"),showgrid=False), yaxis=dict(tickfont=dict(color="#4b5563"),gridcolor="#e5e7eb"))
    st.plotly_chart(fig_q, use_container_width=True)

    left,right = st.columns(2)
    with left:
        st.subheader("Totale per catalogo")
        fig_cat = go.Figure(go.Pie(labels=catalogs["Catalogo"], values=catalogs["Totale Complessivo"], hole=.48,
                                   marker_colors=["#168af4","#163eae","#ec6b2d","#760080","#dd469d"], textinfo="label+percent"))
        fig_cat.update_layout(height=330, paper_bgcolor="white", plot_bgcolor="white", font=dict(color="#202124"), showlegend=True, legend=dict(orientation="v",x=1.02,y=0.5,font=dict(color="#202124",size=12),bgcolor="rgba(255,255,255,0.95)"), margin=dict(l=10,r=145,t=20,b=10))
        st.plotly_chart(fig_cat, use_container_width=True)
    with right:
        st.subheader("Spedito vs Da Spedire per catalogo")
        fig_cat2 = go.Figure()
        fig_cat2.add_bar(name="Spedito", x=catalogs["Catalogo"], y=catalogs["Spedito Totale"], marker_color="#4F8FC5", text=[euro(v) for v in catalogs["Spedito Totale"]], textposition="inside", textfont=dict(color="white",size=11))
        fig_cat2.add_bar(name="Da Spedire", x=catalogs["Catalogo"], y=catalogs["Da Spedire Totale"], marker_color="#D6E86B", text=[euro(v) if v else "" for v in catalogs["Da Spedire Totale"]], textposition="inside", textfont=dict(color="#202124",size=10))
        fig_cat2.update_layout(barmode="stack",height=330,paper_bgcolor="white",plot_bgcolor="white",legend_orientation="h",legend=dict(font=dict(color="#202124",size=12),bgcolor="rgba(255,255,255,0.9)"),yaxis_title="Euro",font=dict(color="#202124"),margin=dict(l=20,r=10,t=20,b=20),xaxis=dict(tickfont=dict(color="#202124"),showgrid=False),yaxis=dict(tickfont=dict(color="#4b5563"),gridcolor="#e5e7eb"))
        st.plotly_chart(fig_cat2,use_container_width=True)

    st.subheader("Riepilogo cataloghi")
    summary = catalogs[["Catalogo","Spedito Q1","Spedito Q2","Spedito Q3","Spedito Q4","Spedito Totale","Da Spedire Q1","Da Spedire Q2","Da Spedire Q3","Da Spedire Q4","Da Spedire Totale","Totale Complessivo"]].copy()
    for col in summary.columns[1:]: summary[col] = summary[col].map(euro)
    st.dataframe(summary, use_container_width=True, hide_index=True)

    st.subheader("Prodotti e conferme ordine")
    orders = data[data["Livello"] == "order"].copy()
    ranges = data[data["Livello"] == "range"].copy()

    catalog_options = catalogs["Catalogo"].dropna().astype(str).tolist()
    if not catalog_options:
        st.info("Nessun catalogo disponibile.")
        return

    filter_col1, filter_col2 = st.columns(2)
    with filter_col1:
        selected_catalog = st.selectbox(
            "Filtra per catalogo",
            catalog_options,
            key=f"catalog_summary_{code}"
        )

    catalog_orders = orders[orders["Catalogo"] == selected_catalog].copy()
    order_options = sorted(catalog_orders["Numero Ordine"].dropna().astype(str).unique().tolist(), key=lambda x: (int(re.sub(r"\D", "", x) or 0), x), reverse=True)

    with filter_col2:
        if order_options:
            selected_order = st.selectbox(
                "Conferma ordine",
                order_options,
                key=f"order_selector_{code}_{selected_catalog}"
            )
        else:
            selected_order = None
            st.selectbox(
                "Conferma ordine",
                ["Nessuna conferma disponibile"],
                disabled=True,
                key=f"order_selector_empty_{code}_{selected_catalog}"
            )

    # Tabella prodotti del catalogo: righe già visibili.
    family_rows = ranges[ranges["Catalogo"] == selected_catalog].copy()
    if family_rows.empty:
        family_summary = families[families["Catalogo"] == selected_catalog][[
            "Famiglia Prodotto","Spedito Totale","Da Spedire Totale","Totale Complessivo"
        ]].copy()
    else:
        family_summary = family_rows.groupby("Famiglia Prodotto", as_index=False).agg({
            "Spedito Totale":"sum",
            "Da Spedire Totale":"sum",
            "Totale Complessivo":"sum"
        })

    if not family_summary.empty:
        family_summary = family_summary.sort_values("Totale Complessivo", ascending=False)
        family_display = family_summary.copy()
        family_display = family_display.rename(columns={
            "Spedito Totale":"Spedito",
            "Da Spedire Totale":"Da Spedire",
            "Totale Complessivo":"Totale"
        })
        for col in ["Spedito","Da Spedire","Totale"]:
            family_display[col] = family_display[col].map(euro)
        st.dataframe(family_display, use_container_width=True, hide_index=True)

    if selected_order:
        selected_row = catalog_orders[catalog_orders["Numero Ordine"] == selected_order].iloc[0]
        st.markdown("**Dettaglio conferma ordine selezionata**")
        o1,o2,o3 = st.columns(3)
        o1.metric("Numero ordine", selected_order)
        o2.metric("Catalogo", selected_catalog)
        o3.metric("Totale ordine", euro(selected_row["Totale Complessivo"]))
        pdf_record, pdf_error = load_google_order_pdf(code, selected_order)
        if not pdf_record:
            neon_pdf = load_order_pdf(code, selected_order)
            if neon_pdf:
                pdf_record = {
                    "filename": neon_pdf["filename"],
                    "content": bytes(neon_pdf["content"]),
                    "source": "Archivio manuale",
                }
        if pdf_record:
            st.download_button(
                "Apri / Scarica conferma PDF",
                data=pdf_record["content"],
                file_name=pdf_record["filename"],
                mime="application/pdf",
                key=f"download_pdf_{code}_{selected_order}",
                type="primary"
            )
            st.caption(f"Documento disponibile da: {pdf_record['source']}")
        elif pdf_error:
            st.warning(pdf_error)
        else:
            st.info("PDF conferma non ancora disponibile.")

        ddt_documents, ddt_error = load_google_ddt(code, selected_order)
        if ddt_documents:
            for index, document in enumerate(ddt_documents):
                label = "Apri / Scarica DDT" if len(ddt_documents) == 1 else f"Apri / Scarica DDT {index + 1}"
                st.download_button(
                    label,
                    data=document["content"],
                    file_name=document["filename"],
                    mime="application/pdf",
                    key=f"download_ddt_{code}_{selected_order}_{index}",
                    type="primary"
                )
                st.caption("Documento disponibile da: Google Drive")
        elif ddt_error:
            st.warning(ddt_error)
        else:
            st.caption("Nessun DDT disponibile per questa conferma.")

        selected_ranges = ranges[ranges["Numero Ordine"] == selected_order].copy()
        if not selected_ranges.empty:
            range_summary = selected_ranges.groupby(["Famiglia Prodotto","Range"], as_index=False).agg({
                "Spedito Totale":"sum",
                "Da Spedire Totale":"sum",
                "Totale Complessivo":"sum"
            })
            range_summary = range_summary.rename(columns={
                "Spedito Totale":"Spedito",
                "Da Spedire Totale":"Da Spedire",
                "Totale Complessivo":"Totale"
            })
            range_display = range_summary.copy()
            for col in ["Spedito","Da Spedire","Totale"]:
                range_display[col] = range_display[col].map(euro)
            st.dataframe(range_display, use_container_width=True, hide_index=True)

        with st.expander("Dettaglio trimestrale della conferma"):
            quarter_table = pd.DataFrame({
                "Voce":["Spedito","Da Spedire","Totale"],
                "Q1":[selected_row["Spedito Q1"],selected_row["Da Spedire Q1"],selected_row["Spedito Q1"]+selected_row["Da Spedire Q1"]],
                "Q2":[selected_row["Spedito Q2"],selected_row["Da Spedire Q2"],selected_row["Spedito Q2"]+selected_row["Da Spedire Q2"]],
                "Q3":[selected_row["Spedito Q3"],selected_row["Da Spedire Q3"],selected_row["Spedito Q3"]+selected_row["Da Spedire Q3"]],
                "Q4":[selected_row["Spedito Q4"],selected_row["Da Spedire Q4"],selected_row["Spedito Q4"]+selected_row["Da Spedire Q4"]],
                "Totale":[selected_row["Spedito Totale"],selected_row["Da Spedire Totale"],selected_row["Totale Complessivo"]]
            })
            for col in ["Q1","Q2","Q3","Q4","Totale"]:
                quarter_table[col] = quarter_table[col].map(euro)
            st.dataframe(quarter_table, use_container_width=True, hide_index=True)

def admin_dashboard():
    st.title("Cruscotto generale MD Rappresentanze")
    status = dataset_status()
    rows = []
    catalog_rows = []
    for code, info in CLIENTS.items():
        record = load_file(code)
        if not record:
            rows.append({"Cliente": info["name"], "Codice": code, "Spedito": 0.0, "Da Spedire": 0.0, "Totale": 0.0, "Stato": "Dati non caricati"})
            continue
        try:
            data, found_code, found_name = read_export(io.BytesIO(bytes(record["content"])))
            grand = data[data["Livello"] == "grand_total"]
            if grand.empty:
                raise ValueError("Totale mancante")
            total = grand.iloc[-1]
            rows.append({
                "Cliente": info["name"], "Codice": code,
                "Spedito": float(total["Spedito Totale"]),
                "Da Spedire": float(total["Da Spedire Totale"]),
                "Totale": float(total["Totale Complessivo"]), "Stato": "Dati caricati"
            })
            for _, r in data[data["Livello"] == "catalog"].iterrows():
                catalog_rows.append({"Cliente": info["name"], "Catalogo": r["Catalogo"], "Totale": float(r["Totale Complessivo"])})
        except Exception as exc:
            rows.append({"Cliente": info["name"], "Codice": code, "Spedito": 0.0, "Da Spedire": 0.0, "Totale": 0.0, "Stato": f"Da aggiornare"})

    overview = pd.DataFrame(rows)
    loaded = overview[overview["Stato"] == "Dati caricati"]
    total_spedito = loaded["Spedito"].sum() if not loaded.empty else 0
    total_da_spedire = loaded["Da Spedire"].sum() if not loaded.empty else 0
    total_complessivo = loaded["Totale"].sum() if not loaded.empty else 0

    a,b,c,d = st.columns(4)
    a.metric("Spedito complessivo", euro(total_spedito))
    b.metric("Da Spedire complessivo", euro(total_da_spedire))
    c.metric("Totale portafoglio", euro(total_complessivo))
    d.metric("Clienti aggiornati", f"{len(loaded)} / {len(CLIENTS)}")

    if not loaded.empty:
        st.subheader("Confronto clienti")
        fig = go.Figure()
        fig.add_bar(name="Spedito", x=loaded["Cliente"], y=loaded["Spedito"], marker_color="#4F8FC5", text=[euro(v) for v in loaded["Spedito"]], textposition="inside", textfont=dict(color="white",size=12))
        fig.add_bar(name="Da Spedire", x=loaded["Cliente"], y=loaded["Da Spedire"], marker_color="#D6E86B", text=[euro(v) if v else "" for v in loaded["Da Spedire"]], textposition="inside", textfont=dict(color="#202124",size=11))
        fig.update_layout(barmode="stack", height=320, paper_bgcolor="white", plot_bgcolor="white", legend_orientation="h", legend=dict(font=dict(color="#202124",size=12),bgcolor="rgba(255,255,255,0.9)"), yaxis_title="Euro", font=dict(color="#202124"), margin=dict(l=20,r=15,t=20,b=55), xaxis=dict(tickfont=dict(color="#202124",size=11),showgrid=False), yaxis=dict(tickfont=dict(color="#4b5563"),gridcolor="#e5e7eb"))
        st.plotly_chart(fig, use_container_width=True)

        if catalog_rows:
            cat = pd.DataFrame(catalog_rows).groupby("Catalogo", as_index=False)["Totale"].sum()
            st.subheader("Ripartizione complessiva per catalogo")
            fig_cat = go.Figure(go.Pie(labels=cat["Catalogo"], values=cat["Totale"], hole=.48,
                                       marker_colors=["#168af4","#163eae","#ec6b2d","#760080","#dd469d"], textinfo="label+percent"))
            fig_cat.update_layout(height=320, paper_bgcolor="white", plot_bgcolor="white", font=dict(color="#202124"), showlegend=True, legend=dict(orientation="v",x=1.02,y=0.5,font=dict(color="#202124",size=12),bgcolor="rgba(255,255,255,0.95)"), margin=dict(l=10,r=145,t=20,b=10))
            st.plotly_chart(fig_cat, use_container_width=True)

    st.subheader("Situazione clienti")
    table = overview.copy()
    for col in ["Spedito", "Da Spedire", "Totale"]:
        table[col] = table[col].map(euro)
    st.dataframe(table, use_container_width=True, hide_index=True)

    available = [r for r in rows if r["Stato"] == "Dati caricati"]
    if available:
        st.subheader("Apri dashboard cliente")
        choices = {f'{r["Cliente"]} · {r["Codice"]}': r["Codice"] for r in available}
        label = st.selectbox("Cliente", list(choices.keys()), key="admin_client_selector")
        if st.button("Apri dettaglio cliente", type="primary"):
            chosen = choices[label]
            record = load_file(chosen)
            data, found_code, found_name = read_export(io.BytesIO(bytes(record["content"])))
            st.session_state["admin_preview"] = (data, found_code, found_name, record["updated_at"])
        if "admin_preview" in st.session_state:
            data, found_code, found_name, updated_at = st.session_state["admin_preview"]
            st.divider()
            dashboard(data, found_code, found_name)
            st.caption(f"Ultimo aggiornamento: {updated_at}")

def order_pdfs_page():
    st.title("Gestione PDF conferme Clivet")
    st.caption("Associa ogni PDF Clivet al cliente e al numero ordine presente nella dashboard.")
    client_labels={f"{info['name']} · {code}":code for code,info in CLIENTS.items()}
    selected_client_label=st.selectbox("Cliente",list(client_labels.keys()),key="pdf_client")
    selected_code=client_labels[selected_client_label]
    record=load_file(selected_code)
    if not record:
        st.warning("Per questo cliente non sono ancora stati caricati i dati Excel.")
        return
    try:
        data,_,_=read_export(io.BytesIO(bytes(record["content"])))
        orders=data[data["Livello"]=="order"]["Numero Ordine"].dropna().astype(str).unique().tolist()
    except Exception as exc:
        st.error(f"Impossibile leggere gli ordini del cliente: {exc}")
        return
    if not orders:
        st.warning("Nessun numero ordine trovato nel file del cliente.")
        return
    selected_order=st.selectbox("Numero ordine",orders,key=f"pdf_order_{selected_code}")
    existing=load_order_pdf(selected_code,selected_order)
    if existing:
        st.success(f"PDF già associato: {existing['filename']}")
        st.download_button("Scarica PDF attuale",data=bytes(existing["content"]),file_name=existing["filename"],mime="application/pdf",key=f"admin_download_{selected_code}_{selected_order}")
    uploaded_pdf=st.file_uploader("Carica conferma Clivet in PDF",type=["pdf"],key=f"pdf_upload_{selected_code}_{selected_order}")
    if uploaded_pdf is not None and st.button("Salva PDF conferma",type="primary",key=f"save_pdf_{selected_code}_{selected_order}"):
        save_order_pdf(selected_code,selected_order,uploaded_pdf.name,uploaded_pdf.getvalue())
        st.success(f"PDF associato all'ordine {selected_order}.")
        st.rerun()
    st.subheader("PDF già caricati")
    rows=[dict(r) for r in pdf_status()]
    if rows:
        status_df=pd.DataFrame(rows)
        status_df["Cliente"]=status_df["customer_code"].map(lambda c: CLIENTS.get(c,{}).get("name",c))
        status_df=status_df[["Cliente","order_number","filename","updated_at"]].rename(columns={"order_number":"Numero Ordine","filename":"File","updated_at":"Aggiornato"})
        st.dataframe(status_df,use_container_width=True,hide_index=True)
    else:
        st.info("Nessun PDF caricato.")
def ensure_commercial_agreement_table():
    def operation(connection):
        with connection.session as session:
            session.execute(text("""CREATE TABLE IF NOT EXISTS commercial_agreement_2027(customer_code TEXT PRIMARY KEY,baseline_2026 NUMERIC NOT NULL DEFAULT 0,baseline_q1_2026 NUMERIC NOT NULL DEFAULT 0,quantity_discount NUMERIC NOT NULL DEFAULT 0,snapshot_locked BOOLEAN NOT NULL DEFAULT FALSE,snapshot_date TIMESTAMP,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""))
            session.commit()
    return run_db_with_retry(operation)

def load_commercial_agreement(code):
    ensure_commercial_agreement_table()
    def operation(connection):
        with connection.session as session:
            return session.execute(text("SELECT * FROM commercial_agreement_2027 WHERE customer_code=:code"),{"code":code}).mappings().first() or {}
    return run_db_with_retry(operation)

def save_commercial_agreement(code, baseline, baseline_q1, quantity_discount, lock_snapshot=False):
    ensure_commercial_agreement_table()
    def operation(connection):
        with connection.session as session:
            session.execute(text("""INSERT INTO commercial_agreement_2027(customer_code,baseline_2026,baseline_q1_2026,quantity_discount,snapshot_locked,snapshot_date,updated_at) VALUES(:code,:baseline,:q1,:discount,:locked,CASE WHEN :locked THEN CURRENT_TIMESTAMP ELSE NULL END,CURRENT_TIMESTAMP) ON CONFLICT(customer_code) DO UPDATE SET baseline_2026=CASE WHEN commercial_agreement_2027.snapshot_locked THEN commercial_agreement_2027.baseline_2026 ELSE :baseline END,baseline_q1_2026=CASE WHEN commercial_agreement_2027.snapshot_locked THEN commercial_agreement_2027.baseline_q1_2026 ELSE :q1 END,quantity_discount=:discount,snapshot_locked=commercial_agreement_2027.snapshot_locked OR :locked,snapshot_date=CASE WHEN commercial_agreement_2027.snapshot_locked THEN commercial_agreement_2027.snapshot_date WHEN :locked THEN CURRENT_TIMESTAMP ELSE commercial_agreement_2027.snapshot_date END,updated_at=CURRENT_TIMESTAMP"""),{"code":code,"baseline":float(baseline),"q1":float(baseline_q1),"discount":float(quantity_discount),"locked":bool(lock_snapshot)})
            session.commit()
    return run_db_with_retry(operation)

def commercial_agreement_page(user):
    st.title("Accordi Addendum")
    st.header("Accordo Commerciale 2027 · Rivenditore")
    is_admin=user.get("role")=="admin"
    if is_admin:
        labels={f"{info['name']} · {code}":code for code,info in CLIENTS.items()}
        selected=st.selectbox("Cliente",list(labels.keys()),key="commercial_client")
        code=labels[selected]
    else:
        code=user.get("customer_code") or user.get("code")
        if not code or code not in CLIENTS:
            st.error("Cliente non associato all'account.");return
        st.caption(CLIENTS[code]["name"])
    record=load_file(code)
    if not record:
        st.info("Dati cliente non disponibili.");return
    parsed=read_export(io.BytesIO(bytes(record["content"])))
    data=parsed[0] if isinstance(parsed,tuple) else parsed
    catalogs=data[data["Livello"]=="catalog"].copy()
    hs=catalogs[catalogs["Catalogo"].astype(str).str.strip().str.casefold().isin(["home","split"])]
    current_q1=float((hs["Spedito Q1"]+hs["Da Spedire Q1"]).sum())
    current_year=float(hs["Totale Complessivo"].sum())
    agreement=load_commercial_agreement(code)
    locked=bool(agreement.get("snapshot_locked",False))
    baseline=float(agreement.get("baseline_2026",0) or 0)
    baseline_q1=float(agreement.get("baseline_q1_2026",0) or 0)
    discount=float(agreement.get("quantity_discount",0) or 0)

    if is_admin:
        with st.form(f"agreement_admin_{code}"):
            st.subheader("Parametri amministratore")
            q_discount=st.number_input("Sconto quantità HOME (%)",min_value=0.0,max_value=100.0,value=discount,step=0.5,format="%.2f",help="Valore riservato all'Admin. Lo sconto quantità è cumulabile con gli eventuali sconti pre-stagionali previsti dall'accordo.")
            if locked:
                st.info(f"Consuntivo 2026 fissato: {euro(baseline)} · Q1 2026: {euro(baseline_q1)}")
            else:
                st.warning("Snapshot 2026 non ancora fissato. Il pulsante sottostante registra i dati correnti e li mantiene invariati per i confronti 2027.")
            c1,c2=st.columns(2)
            save=c1.form_submit_button("Salva sconto quantità",use_container_width=True)
            lock=c2.form_submit_button("Fissa consuntivo 2026",type="primary",use_container_width=True,disabled=locked)
        if save or lock:
            save_commercial_agreement(code,current_year,current_q1,q_discount,lock_snapshot=lock)
            st.success("Accordo commerciale aggiornato.");st.rerun()

    if not locked:
        baseline=current_year
        baseline_q1=current_q1
        st.caption("Anteprima: finché lo snapshot 2026 non viene fissato dall'Admin, il confronto usa i dati correnti.")

    target_q1=baseline_q1*1.15
    annual_targets=[(0.80,0.01),(0.90,0.02),(1.00,0.04),(1.10,0.05),(1.20,0.06)]
    ratio=(current_year/baseline) if baseline>0 else 0
    achieved_rate=0.0
    achieved_target=0.0
    for threshold,rate in annual_targets:
        if ratio+1e-9>=threshold:
            achieved_rate=rate; achieved_target=threshold
    min_turnover_ok=current_year>=50000
    annual_prize=current_year*achieved_rate if min_turnover_ok else 0.0
    q1_reached=target_q1>0 and current_q1>=target_q1
    q1_prize=current_q1*0.03 if q1_reached and min_turnover_ok else 0.0

    st.subheader("Confronto 2026 → 2027")
    c1,c2,c3,c4=st.columns(4)
    c1.metric("Consuntivo HOME+SPLIT 2026",euro(baseline))
    c2.metric("HOME+SPLIT 2027 attuale",euro(current_year))
    c3.metric("Avanzamento vs 2026",f"{ratio*100:.1f}%".replace(".",","))
    c4.metric("Sconto quantità HOME",f"{discount:.2f}%".replace(".",","))

    st.subheader("Target 1° trimestre")
    q1a,q1b,q1c,q1d=st.columns(4)
    q1a.metric("Q1 2026 fisso",euro(baseline_q1))
    q1b.metric("Target Q1 · 115%",euro(target_q1))
    q1c.metric("Q1 2027 attuale",euro(current_q1))
    q1d.metric("Premio Q1 · 3%",euro(q1_prize),delta="Raggiunto" if q1_reached else "Non raggiunto")

    st.subheader("Target annuale e premio")
    rows=[]
    for threshold,rate in annual_targets:
        target_value=baseline*threshold
        reached=baseline>0 and current_year>=target_value
        rows.append({"Obiettivo vs 2026":f"{threshold*100:.0f}%","Target":euro(target_value),"Premio":f"{rate*100:.0f}%","Stato":"Raggiunto" if reached else "Da raggiungere","Valore premio sul fatturato attuale":euro(current_year*rate if reached and min_turnover_ok else 0)})
    st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)
    p1,p2,p3=st.columns(3)
    p1.metric("Premio annuale maturato",euro(annual_prize),delta=f"{achieved_rate*100:.0f}%" if achieved_rate else "0%")
    p2.metric("Premio Q1 maturato",euro(q1_prize))
    p3.metric("Fatturato minimo €50.000","OK" if min_turnover_ok else "NON RAGGIUNTO")

    st.subheader("Condizioni commerciali principali")
    st.markdown("**Sconti catalogo:** HOME base 55%; SPLIT base 60%; prestagionale SPLIT fino a 99 pz 64%, oltre 100 pz 65%. VRF e APPLIED su richiesta di offerta.")
    st.markdown("**Trasporto:** HOME + SPLIT Italia 3,5% sul netto. Importo minimo addebitabile per un trasporto €50.")
    st.markdown("**Pre-stagionali:** soglie minime definite per SPLIT, EDGE, HYDRO-SPLIT, AQUA, FANCOILS e SINERGY, calcolate sul 60% dell'anno precedente con minimi specifici.")
    st.caption("I premi sono una stima di dashboard. Restano soggetti alle ulteriori condizioni di riconoscimento previste dall'accordo commerciale.")

def ensure_bilico_table():
    def operation(connection):
        with connection.session as session:
            session.execute(text("""CREATE TABLE IF NOT EXISTS addendum_bilico(customer_code TEXT NOT NULL,line_code TEXT NOT NULL,size_code TEXT NOT NULL,ordered_qty INTEGER NOT NULL DEFAULT 0,extra_discount NUMERIC NOT NULL DEFAULT 0,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(customer_code,line_code,size_code))"""))
            session.commit()
    return run_db_with_retry(operation)

def load_bilico(customer_code):
    ensure_bilico_table()
    def operation(connection):
        with connection.session as session:
            rows=session.execute(text("SELECT line_code,size_code,ordered_qty,extra_discount FROM addendum_bilico WHERE customer_code=:code"),{"code":customer_code}).mappings().all()
            result={}
            for r in rows:
                result[(r["line_code"],r["size_code"])]=int(r["ordered_qty"] or 0)
                result[(r["line_code"],"EXTRA_DISCOUNT")]=float(r["extra_discount"] or 0)
            return result
    return run_db_with_retry(operation)

def save_bilico(customer_code, quantities, discounts):
    ensure_bilico_table()
    def operation(connection):
        with connection.session as session:
            for (line,size),qty in quantities.items():
                discount=float(discounts.get(line,0))
                session.execute(text("""INSERT INTO addendum_bilico(customer_code,line_code,size_code,ordered_qty,extra_discount,updated_at) VALUES(:code,:line,:size,:qty,:discount,CURRENT_TIMESTAMP) ON CONFLICT(customer_code,line_code,size_code) DO UPDATE SET ordered_qty=:qty,extra_discount=:discount,updated_at=CURRENT_TIMESTAMP"""),{"code":customer_code,"line":line,"size":size,"qty":int(qty),"discount":discount})
            session.commit()
    return run_db_with_retry(operation)

def bilico_page(user):
    st.title("Accordi Addendum")
    st.header("Sconto Bilico 2027")
    is_admin=user.get("role")=="admin"
    if is_admin:
        labels={f"{info['name']} · {code}":code for code,info in CLIENTS.items()}
        selected=st.selectbox("Cliente",list(labels.keys()),key="bilico_client")
        code=labels[selected]
    else:
        code=user.get("customer_code") or user.get("code")
        if not code or code not in CLIENTS:
            st.error("Cliente non associato all'account.");return
        st.caption(CLIENTS[code]["name"])

    lines={
      "EZCOOL": {"title":"EZCool (MLA1-Y + ILA1-Y)","sizes":[("27M",234.0),("35M",251.0),("53M",450.0),("70M",549.0)]},
      "CRISTALLO2": {"title":"CRISTALLO 2 (MMA1-Y + IMA1-Y)","sizes":[("27M",377.0),("35M",397.0),("53M",632.0),("70M",765.0)]},
    }
    saved=load_bilico(code)
    quantities={};discounts={}
    with st.form(f"bilico_{code}"):
        for line,info in lines.items():
            st.subheader(info["title"])
            cols=st.columns(4)
            for idx,(size,net) in enumerate(info["sizes"]):
                with cols[idx]:
                    st.markdown(f"**{size}**")
                    st.caption(f"Netto Bilico {euro(net)}")
                    quantities[(line,size)]=st.number_input("Quantità",min_value=0,step=1,value=int(saved.get((line,size),0)),key=f"bilico_{code}_{line}_{size}")
            discounts[line]=st.number_input(f"{info['title']} · Extra sconto (%)",min_value=0.0,max_value=100.0,step=0.5,value=float(saved.get((line,"EXTRA_DISCOUNT"),0)),format="%.2f",key=f"bilico_discount_{code}_{line}") if is_admin else float(saved.get((line,"EXTRA_DISCOUNT"),0))
        save=st.form_submit_button("Salva quantità Bilico",type="primary",use_container_width=True)
    if save:
        if is_admin:
            save_bilico(code,quantities,discounts)
        else:
            # Il cliente può aggiornare le quantità, mantenendo invariato l'extra sconto impostato dall'Admin.
            save_bilico(code,quantities,{line:float(saved.get((line,"EXTRA_DISCOUNT"),0)) for line in lines})
        st.success("Addendum Bilico aggiornato.");st.rerun()

    grand_total=0.0
    for line,info in lines.items():
        rows=[];subtotal=0.0
        extra=float(saved.get((line,"EXTRA_DISCOUNT"),discounts.get(line,0)))
        # durante l'editing Admin mostriamo anche il valore corrente del form
        if is_admin: extra=discounts.get(line,extra)
        for size,net in info["sizes"]:
            qty=quantities.get((line,size),int(saved.get((line,size),0)))
            row_total=qty*net;subtotal+=row_total
            rows.append({"Taglia":size,"Quantità":qty,"Prezzo netto Bilico":euro(net),"Totale":euro(row_total)})
        extra_value=subtotal*(extra/100)
        final=max(subtotal-extra_value,0);grand_total+=final
        st.subheader(info["title"])
        st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)
        c1,c2,c3=st.columns(3)
        c1.metric("Subtotale",euro(subtotal))
        c2.metric("Extra sconto",euro(extra_value),delta=f"{extra:.2f}%".replace(".",","))
        c3.metric("Totale dopo extra sconto",euro(final))
    st.metric("Totale complessivo Sconto Bilico",euro(grand_total))

def ensure_superpre_table():
    def operation(connection):
        with connection.session as session:
            session.execute(text("""CREATE TABLE IF NOT EXISTS addendum_superpre(customer_code TEXT NOT NULL,family_code TEXT NOT NULL,target_h1 NUMERIC NOT NULL DEFAULT 0,program_q1 NUMERIC NOT NULL DEFAULT 0,program_q2 NUMERIC NOT NULL DEFAULT 0,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(customer_code,family_code))"""))
            session.execute(text("ALTER TABLE addendum_superpre ADD COLUMN IF NOT EXISTS program_q1 NUMERIC NOT NULL DEFAULT 0"))
            session.execute(text("ALTER TABLE addendum_superpre ADD COLUMN IF NOT EXISTS program_q2 NUMERIC NOT NULL DEFAULT 0"))
            session.commit()
    return run_db_with_retry(operation)

def load_superpre_values(customer_code):
    ensure_superpre_table()
    def operation(connection):
        with connection.session as session:
            rows=session.execute(text("SELECT family_code,program_q1,program_q2 FROM addendum_superpre WHERE customer_code=:code"),{"code":customer_code}).mappings().all()
            return {r["family_code"]:{"q1":float(r["program_q1"] or 0),"q2":float(r["program_q2"] or 0)} for r in rows}
    return run_db_with_retry(operation)

def save_superpre_program(customer_code, values):
    ensure_superpre_table()
    def operation(connection):
        with connection.session as session:
            for family,item in values.items():
                session.execute(text("""INSERT INTO addendum_superpre(customer_code,family_code,program_q1,program_q2,updated_at) VALUES(:code,:family,:q1,:q2,CURRENT_TIMESTAMP) ON CONFLICT(customer_code,family_code) DO UPDATE SET program_q1=:q1,program_q2=:q2,updated_at=CURRENT_TIMESTAMP"""),{"code":customer_code,"family":family,"q1":float(item["q1"]),"q2":float(item["q2"])})
            session.commit()
    return run_db_with_retry(operation)

def superpre_page(user):
    st.title("Accordi Addendum")
    st.header("Extra Premio Super Pre 2027 · Split e Aqua F")
    is_admin=user.get("role")=="admin"
    if is_admin:
        labels={f"{info['name']} · {code}":code for code,info in CLIENTS.items()}
        selected=st.selectbox("Cliente",list(labels.keys()),key="superpre_client")
        code=labels[selected]
    else:
        code=user.get("customer_code") or user.get("code")
        if not code or code not in CLIENTS:
            st.error("Cliente non associato all'account.")
            return
        st.caption(CLIENTS[code]["name"])

    record=load_file(code)
    if not record:
        st.info("Dati cliente non disponibili.")
        return
    parsed=read_export(io.BytesIO(bytes(record["content"])))
    data=parsed[0] if isinstance(parsed, tuple) else parsed
    catalogs=data[data["Livello"]=="catalog"].copy()
    families=data[data["Livello"]=="family"].copy()
    split=catalogs[catalogs["Catalogo"].astype(str).str.strip().str.casefold()=="split"]
    aqua=families[families["Famiglia Prodotto"].astype(str).str.contains("aqua",case=False,na=False)]

    # Target H1 fisso: valore Q1 + Q2 già presente nel file cliente.
    split_actual_q1=float((split["Spedito Q1"]+split["Da Spedire Q1"]).sum())
    split_actual_q2=float((split["Spedito Q2"]+split["Da Spedire Q2"]).sum())
    aqua_actual_q1=float((aqua["Spedito Q1"]+aqua["Da Spedire Q1"]).sum())
    aqua_actual_q2=float((aqua["Spedito Q2"]+aqua["Da Spedire Q2"]).sum())
    target_split=split_actual_q1+split_actual_q2
    target_aqua=aqua_actual_q1+aqua_actual_q2
    targets={"SPLIT":target_split,"AQUA_F":target_aqua}
    actuals={"SPLIT":{"q1":split_actual_q1,"q2":split_actual_q2},"AQUA_F":{"q1":aqua_actual_q1,"q2":aqua_actual_q2}}
    programs=load_superpre_values(code)

    # La programmazione è libera e separata per trimestre.
    with st.form(f"superpre_program_{code}"):
        st.subheader("Programmazione libera")
        s1,s2=st.columns(2)
        split_q1=s1.number_input("SPLIT · Programmazione Q1 (€)",min_value=0.0,value=float(programs.get("SPLIT",{}).get("q1",0)),step=1000.0,format="%.2f")
        split_q2=s2.number_input("SPLIT · Programmazione Q2 (€)",min_value=0.0,value=float(programs.get("SPLIT",{}).get("q2",0)),step=1000.0,format="%.2f")
        a1,a2=st.columns(2)
        aqua_q1=a1.number_input("AQUA F · Programmazione Q1 (€)",min_value=0.0,value=float(programs.get("AQUA_F",{}).get("q1",0)),step=1000.0,format="%.2f")
        aqua_q2=a2.number_input("AQUA F · Programmazione Q2 (€)",min_value=0.0,value=float(programs.get("AQUA_F",{}).get("q2",0)),step=1000.0,format="%.2f")
        save=st.form_submit_button("Salva programmazione",type="primary",use_container_width=True)
    if save:
        save_superpre_program(code,{"SPLIT":{"q1":split_q1,"q2":split_q2},"AQUA_F":{"q1":aqua_q1,"q2":aqua_q2}})
        st.success("Programmazione Super Pre salvata.");st.rerun()

    program_values={"SPLIT":{"q1":split_q1,"q2":split_q2},"AQUA_F":{"q1":aqua_q1,"q2":aqua_q2}}
    rows=[]; total_premio=0.0; results=[]
    for family,label in [("SPLIT","SPLIT"),("AQUA_F","AQUA F")]:
        target=targets[family]
        q1=program_values[family]["q1"]; q2=program_values[family]["q2"]
        programmed=q1+q2
        # Tolleranza di un centesimo per evitare che valori visualmente uguali
        # risultino non raggiunti a causa dei decimali interni.
        reached=target>0 and round(programmed)>=round(target)
        premio_q1=q1*0.10 if reached else 0.0
        premio_q2=q2*0.05 if reached else 0.0
        premio=premio_q1+premio_q2; total_premio+=premio
        rows.append({"Famiglia":label,"Target H1 fisso":euro(target),"Fatto Q1 attuale":euro(actuals[family]["q1"]),"Fatto Q2 fino ad oggi":euro(actuals[family]["q2"]),"Programmazione Q1":euro(q1),"Programmazione Q2":euro(q2),"Programmazione totale":euro(programmed),"Target raggiunto":"Sì" if reached else "No","Premio Q1 10%":euro(premio_q1),"Premio Q2 5%":euro(premio_q2),"Premio totale":euro(premio)})
    st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)
    st.metric("Premio Super Pre complessivo stimato",euro(total_premio))
    st.caption("I target SPLIT e AQUA F sono distinti. Il target può essere raggiunto interamente nel Q1, interamente nel Q2 oppure sommando i due trimestri. Il premio è pari al 10% della quota programmata nel Q1 e al 5% della quota programmata nel Q2, purché il relativo target sia raggiunto.")

def ensure_addendum_bonus_table():
    def operation(connection):
        with connection.session as session:
            session.execute(text("""CREATE TABLE IF NOT EXISTS addendum_bonus(customer_code TEXT NOT NULL,family_code TEXT NOT NULL,bonus_qty INTEGER NOT NULL DEFAULT 0,bonus_net_value NUMERIC NOT NULL DEFAULT 0,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(customer_code,family_code))"""))
            session.commit()
    return run_db_with_retry(operation)

def load_addendum_bonus(customer_code):
    ensure_addendum_bonus_table()
    def operation(connection):
        with connection.session as session:
            rows=session.execute(text("SELECT family_code,bonus_qty,bonus_net_value FROM addendum_bonus WHERE customer_code=:code"),{"code":customer_code}).mappings().all()
            return {r["family_code"]:{"qty":int(r["bonus_qty"] or 0),"net":float(r["bonus_net_value"] or 0)} for r in rows}
    return run_db_with_retry(operation)

def save_addendum_bonus(customer_code, values):
    ensure_addendum_bonus_table()
    def operation(connection):
        with connection.session as session:
            for family,item in values.items():
                session.execute(text("""INSERT INTO addendum_bonus(customer_code,family_code,bonus_qty,bonus_net_value,updated_at) VALUES(:code,:family,:qty,:net,CURRENT_TIMESTAMP) ON CONFLICT(customer_code,family_code) DO UPDATE SET bonus_qty=:qty,bonus_net_value=:net,updated_at=CURRENT_TIMESTAMP"""),{"code":customer_code,"family":family,"qty":int(item["qty"]),"net":float(item["net"])})
            session.commit()
    return run_db_with_retry(operation)

def addendum_products():
    return [
        ("EDGE_F_2.1", "EDGE F (R290) · WiSAN-PME 1 S 2.1", "Lotto promo EDGE F: minimo 5 pezzi complessivi · 4 fatturati + 1 omaggio WiSAN-PME 1S 4.1", 0.0),
        ("EDGE_F_3.1", "EDGE F (R290) · WiSAN-PME 1 S 3.1", "Lotto promo EDGE F: minimo 5 pezzi complessivi · 4 fatturati + 1 omaggio WiSAN-PME 1S 4.1", 0.0),
        ("EDGE_F_4.1", "EDGE F (R290) · WiSAN-PME 1 S 4.1", "Lotto promo EDGE F: minimo 5 pezzi complessivi · 4 fatturati + 1 omaggio WiSAN-PME 1S 4.1", 0.0),
        ("EDGE_F_5.1", "EDGE F (R290) · WiSAN-PME 1 S 5.1", "Lotto promo EDGE F: minimo 5 pezzi complessivi · 4 fatturati + 1 omaggio WiSAN-PME 1S 4.1", 0.0),
        ("EDGE_F_6.1", "EDGE F (R290) · WiSAN-PME 1 S 6.1", "Lotto promo EDGE F: minimo 5 pezzi complessivi · 4 fatturati + 1 omaggio WiSAN-PME 1S 4.1", 0.0),
        ("EDGE_F_7.1", "EDGE F (R290) · WiSAN-PME 1 S 7.1", "Lotto promo EDGE F: minimo 5 pezzi complessivi · 4 fatturati + 1 omaggio WiSAN-PME 1S 4.1", 0.0),
        ("EDGE_F_8.1", "EDGE F (R290) · WiSAN-PME 1 S 8.1", "Lotto promo EDGE F: minimo 5 pezzi complessivi · 4 fatturati + 1 omaggio WiSAN-PME 1S 4.1", 0.0),
        ("AQUA_F_100", "AQUA F (R290) · DSDH-P AQ 1 S 100", "Lotto AQUA F: minimo 11 pezzi complessivi · 10 fatturati + 1 omaggio taglia inferiore", 0.0),
        ("AQUA_F_190", "AQUA F (R290) · DSDH-P AQ 1 S 190", "Lotto AQUA F: minimo 11 pezzi complessivi · 10 fatturati + 1 omaggio taglia inferiore", 0.0),
        ("AQUA_F_190S", "AQUA F (R290) · DSDH-P AQ 1 S 190S", "Lotto AQUA F: minimo 11 pezzi complessivi · 10 fatturati + 1 omaggio taglia inferiore", 0.0),
        ("AQUA_F_300", "AQUA F (R290) · DSDH-P AQ 1 S 300", "Lotto AQUA F: minimo 11 pezzi complessivi · 10 fatturati + 1 omaggio taglia inferiore", 0.0),
        ("AQUA_F_300S", "AQUA F (R290) · DSDH-P AQ 1 S 300S", "Lotto AQUA F: minimo 11 pezzi complessivi · 10 fatturati + 1 omaggio taglia inferiore", 0.0),
        ("WISAN_YME_2.1", "EDGE EVO 2.0 · WiSAN-YME 1S 2.1", "Prezzo netto Addendum", 1306.0),
        ("WISAN_YME_3.1", "EDGE EVO 2.0 · WiSAN-YME 1S 3.1", "Prezzo netto Addendum", 1574.0),
        ("WISAN_YME_4.1", "EDGE EVO 2.0 · WiSAN-YME 1S 4.1", "Prezzo netto Addendum", 1752.0),
        ("WISAN_YME_5.1", "EDGE EVO 2.0 · WiSAN-YME 1S 5.1", "Prezzo netto Addendum", 2061.0),
        ("WISAN_YME_6.1", "EDGE EVO 2.0 · WiSAN-YME 1S 6.1", "Prezzo netto Addendum", 2404.0),
        ("WISAN_YME_6.1T", "EDGE EVO 2.0 · WiSAN-YME 1S 6.1T", "Prezzo netto Addendum", 2496.0),
        ("WISAN_YME_7.1", "EDGE EVO 2.0 · WiSAN-YME 1S 7.1", "Prezzo netto Addendum", 2694.0),
        ("WISAN_YME_7.1T", "EDGE EVO 2.0 · WiSAN-YME 1S 7.1T", "Prezzo netto Addendum", 2793.0),
        ("WISAN_YME_8.1", "EDGE EVO 2.0 · WiSAN-YME 1S 8.1", "Prezzo netto Addendum", 2833.0),
        ("WISAN_YME_8.1T", "EDGE EVO 2.0 · WiSAN-YME 1S 8.1T", "Prezzo netto Addendum", 2942.0),
    ]

def save_addendum_quantities(customer_code, quantities):
    ensure_addendum_table()
    def operation(connection):
        with connection.session as session:
            for product_code, qty in quantities.items():
                session.execute(text("""UPDATE addendum_orders SET ordered_qty=:qty,updated_at=CURRENT_TIMESTAMP WHERE customer_code=:code AND product_code=:product"""), {"code":customer_code,"product":product_code,"qty":int(qty)})
                if session.execute(text("SELECT 1 FROM addendum_orders WHERE customer_code=:code AND product_code=:product"), {"code":customer_code,"product":product_code}).first() is None:
                    session.execute(text("""INSERT INTO addendum_orders(customer_code,product_code,ordered_qty,updated_at) VALUES(:code,:product,:qty,CURRENT_TIMESTAMP)"""), {"code":customer_code,"product":product_code,"qty":int(qty)})
            session.commit()
    return run_db_with_retry(operation)

def addendum_page(user, section):
    st.title("Accordi Addendum")
    is_admin=user.get("role")=="admin"
    if is_admin:
        client_labels={f"{info['name']} · {code}":code for code,info in CLIENTS.items()}
        selected_label=st.selectbox("Cliente",list(client_labels.keys()),key=f"addendum_client_{section}")
        selected_code=client_labels[selected_label]
    else:
        selected_code=user.get("customer_code") or user.get("code")
        if not selected_code or selected_code not in CLIENTS:
            st.error("Cliente non associato all'account.")
            return
        st.caption(CLIENTS[selected_code]["name"])

    saved=load_addendum_orders(selected_code)
    bonus_saved=load_addendum_bonus(selected_code)
    products=addendum_products()
    edge_products=[p for p in products if p[0].startswith("EDGE_F_")]
    aqua_products=[p for p in products if p[0].startswith("AQUA_F_")]
    evo_products=[p for p in products if p[0].startswith("WISAN_YME_")]

    def family_summary(title, family_products, threshold, bonus_key):
        rows=[]; order_total=0.0; total_qty=0
        bonus=bonus_saved.get(bonus_key,{})
        gift_net=float(bonus.get("net",0))
        for code,label,note,default_net in family_products:
            item=saved.get(code,{})
            qty=int(item.get("qty",0)); net=float(item.get("promo_price",0)) or float(default_net)
            total_qty+=qty; order_total+=qty*net
            rows.append((code,label,qty,net))
        gifts=total_qty//threshold
        gift_value=gifts*gift_net
        quota=(gift_value/total_qty) if total_qty else 0.0
        display=[]
        for code,label,qty,net in rows:
            effective=max(net-quota,0)
            display.append({"Modello":label.split("·",1)[-1].strip(),"Quantità":qty,"Prezzo netto":euro(effective),"Totale":euro(qty*effective)})
        discounted=max(order_total-gift_value,0)
        st.subheader(title)
        m1,m2,m3,m4=st.columns(4)
        m1.metric("Pezzi ordinati",total_qty)
        m2.metric("Totale ordine",euro(order_total))
        m3.metric("Valore omaggio automatico",euro(gift_value),delta=f"{gifts} omaggi")
        m4.metric("Totale scontato",euro(discounted))
        st.dataframe(pd.DataFrame(display),use_container_width=True,hide_index=True)
        return discounted

    if section=="promo":
        st.header("Addendum Promo EDGE e AQUA")
        st.caption("EDGE F: 1 omaggio ogni 5 pezzi misti. AQUA F: 1 omaggio ogni 11 pezzi misti.")
        active_products=edge_products+aqua_products
        if is_admin:
            values={}
            with st.form(f"promo_admin_{selected_code}"):
                for family_name,family_products in [("EDGE F",edge_products),("AQUA F",aqua_products)]:
                    st.subheader(family_name)
                    for code,label,note,default_net in family_products:
                        current=saved.get(code,{})
                        c1,c2=st.columns([2,1])
                        qty=c1.number_input(f"{label} · pezzi ordinati",min_value=0,step=1,value=int(current.get("qty",0)),key=f"aq_{section}_{selected_code}_{code}")
                        net=c2.number_input(f"{label} · prezzo netto (€)",min_value=0.0,step=10.0,value=float(current.get("promo_price",0)) or float(default_net),format="%.2f",key=f"an_{section}_{selected_code}_{code}")
                        values[code]={"qty":qty,"list_price":0.0,"promo_price":net,"gift_value":0.0,"quantity_discount":0.0}
                st.subheader("Valori omaggio")
                b1,b2=st.columns(2)
                edge_gift_net=b1.number_input("Valore netto unitario omaggio EDGE F 4.1 (€)",min_value=0.0,step=10.0,value=float(bonus_saved.get("EDGE_F",{}).get("net",0)),format="%.2f")
                aqua_gift_net=b2.number_input("Valore netto unitario omaggio AQUA F (€)",min_value=0.0,step=10.0,value=float(bonus_saved.get("AQUA_F",{}).get("net",0)),format="%.2f")
                submitted=st.form_submit_button("Salva Addendum Promo",type="primary",use_container_width=True)
            if submitted:
                save_addendum_orders(selected_code,values)
                edge_qty=sum(values[p[0]]["qty"] for p in edge_products); aqua_qty=sum(values[p[0]]["qty"] for p in aqua_products)
                save_addendum_bonus(selected_code,{"EDGE_F":{"qty":edge_qty//5,"net":edge_gift_net},"AQUA_F":{"qty":aqua_qty//11,"net":aqua_gift_net}})
                st.success("Addendum Promo salvato.");st.rerun()
        else:
            quantities={}
            with st.form(f"promo_client_{selected_code}"):
                st.subheader("Modifica quantità ordinate")
                for code,label,note,default_net in active_products:
                    quantities[code]=st.number_input(label,min_value=0,step=1,value=int(saved.get(code,{}).get("qty",0)),key=f"cq_{section}_{selected_code}_{code}")
                submitted=st.form_submit_button("Salva quantità",type="primary",use_container_width=True)
            if submitted:
                save_addendum_quantities(selected_code,quantities);st.success("Quantità aggiornate.");st.rerun()
        edge_total=family_summary("EDGE F",edge_products,5,"EDGE_F")
        aqua_total=family_summary("AQUA F",aqua_products,11,"AQUA_F")
        st.metric("Totale Addendum Promo EDGE e AQUA",euro(edge_total+aqua_total))

    if section=="evo":
        st.header("Addendum EDGE EVO 2.0")
        if is_admin:
            quantities={}; values={}; previous=[]; subtotal=0.0
            with st.form(f"evo_admin_{selected_code}"):
                for code,label,note,default_net in evo_products:
                    current=saved.get(code,{})
                    c1,c2,c3=st.columns(3)
                    qty=c1.number_input(f"{label} · quantità",min_value=0,step=1,value=int(current.get("qty",0)),key=f"eq_{selected_code}_{code}")
                    net=float(current.get("promo_price",0)) or float(default_net)
                    c2.metric("Prezzo netto base",euro(net))
                    c3.metric("Subtotale",euro(qty*net))
                    subtotal+=qty*net; previous.append(float(current.get("quantity_discount",0)))
                    values[code]={"qty":qty,"list_price":0.0,"promo_price":net,"gift_value":0.0,"quantity_discount":0.0}
                discount=st.number_input("Sconto quantità cumulativo (%)",min_value=0.0,max_value=100.0,step=0.5,value=max(previous or [0.0]),format="%.2f")
                submitted=st.form_submit_button("Salva Addendum EDGE EVO 2.0",type="primary",use_container_width=True)
            if submitted:
                for code in values: values[code]["quantity_discount"]=discount
                save_addendum_orders(selected_code,values);st.success("Addendum EDGE EVO 2.0 salvato.");st.rerun()
        else:
            quantities={}
            with st.form(f"evo_client_{selected_code}"):
                st.subheader("Modifica quantità ordinate")
                for code,label,note,default_net in evo_products:
                    quantities[code]=st.number_input(label,min_value=0,step=1,value=int(saved.get(code,{}).get("qty",0)),key=f"cq_evo_{selected_code}_{code}")
                submitted=st.form_submit_button("Salva quantità",type="primary",use_container_width=True)
            if submitted:
                save_addendum_quantities(selected_code,quantities);st.success("Quantità aggiornate.");st.rerun()
        rows=[]; subtotal=0.0; discounts=[]
        for code,label,note,default_net in evo_products:
            item=saved.get(code,{})
            qty=int(item.get("qty",0)); base=float(item.get("promo_price",0)) or float(default_net); discounts.append(float(item.get("quantity_discount",0)))
            subtotal+=qty*base; rows.append((label,qty,base))
        discount=max(discounts or [0.0]); display=[]
        for label,qty,base in rows:
            effective=base*(1-discount/100)
            display.append({"Modello":label.replace("EDGE EVO 2.0 · ",""),"Quantità":qty,"Prezzo netto":euro(effective),"Totale":euro(qty*effective)})
        discount_value=subtotal*discount/100; total=subtotal-discount_value
        st.dataframe(pd.DataFrame(display),use_container_width=True,hide_index=True)
        m1,m2,m3=st.columns(3)
        m1.metric("Subtotale",euro(subtotal));m2.metric("Sconto quantità",euro(discount_value),delta=f"{discount:.2f}%".replace(".",","));m3.metric("Totale EDGE EVO 2.0",euro(total))

def targets_page():
    st.title("Target annuali")
    st.caption("I valori vengono salvati su Neon e restano invariati finché non vengono modificati dall'amministratore.")
    current=load_targets()
    for code,info in CLIENTS.items():
        t=current.get(code,{})
        with st.expander(f"{info['name']} · {code}", expanded=False):
            with st.form(f"targets_{code}"):
                c1,c2=st.columns(2)
                super_split=c1.number_input("Super Pre Split (€)",min_value=0.0,value=float(t.get("super_pre_split",0) or 0),step=1000.0,format="%.2f")
                super_aqua=c2.number_input("Super Pre Aqua (€)",min_value=0.0,value=float(t.get("super_pre_aqua",0) or 0),step=1000.0,format="%.2f")
                c3,c4=st.columns(2)
                annuale=c3.number_input("Target annuale Home + Split + Fancoil (€)",min_value=0.0,value=float(t.get("target_annuale_hvac",0) or 0),step=1000.0,format="%.2f")
                q1=c4.number_input("Target Q1 Home + Split + Fancoil (€)",min_value=0.0,value=float(t.get("target_q1_hvac",0) or 0),step=1000.0,format="%.2f")
                if st.form_submit_button("Salva Target",type="primary"):
                    save_targets(code,super_split,super_aqua,annuale,q1)
                    st.success(f"Target salvati per {info['name']}.")
                    st.rerun()
            if t.get("updated_at"):
                st.caption(f"Ultima modifica: {t['updated_at']}")
def client_management():
    st.title("Gestione clienti")
    status=dataset_status()
    for code,info in CLIENTS.items():
        record=status.get(code); state="Dati caricati" if record else "Dati non caricati"; update=f" · aggiornato {record['updated_at']}" if record else ""
        st.markdown(f'<div class="client-card"><b>{info["name"]}</b><br>Codice: {code} · Utente: {info["username"]}<br><b>{state}</b>{update}</div>',unsafe_allow_html=True)

def upload_page():
    st.title("Aggiorna dati clienti - tracciato dettagliato")
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
    pages=["Dashboard","Aggiorna dati","Gestione clienti","Target annuali","Gestione PDF conferme","Accordi Addendum · Promo EDGE e AQUA","Accordi Addendum · EDGE EVO 2.0","Accordi Addendum · Super Pre Split e Aqua F","Accordi Addendum · Sconto Bilico 2027","Accordi Addendum · Accordo Commerciale 2027"] if user.get("role")=="admin" else ["Dashboard","Accordi Addendum · Promo EDGE e AQUA","Accordi Addendum · EDGE EVO 2.0","Accordi Addendum · Super Pre Split e Aqua F","Accordi Addendum · Sconto Bilico 2027","Accordi Addendum · Accordo Commerciale 2027"]
    page=st.radio("Sezione",pages)

if page=="Aggiorna dati": upload_page();st.stop()
if page=="Gestione clienti": client_management();st.stop()
if page=="Target annuali": targets_page();st.stop()
if page=="Gestione PDF conferme": order_pdfs_page();st.stop()
if page=="Accordi Addendum · Promo EDGE e AQUA": addendum_page(user,"promo");st.stop()
if page=="Accordi Addendum · EDGE EVO 2.0": addendum_page(user,"evo");st.stop()
if page=="Accordi Addendum · Super Pre Split e Aqua F": superpre_page(user);st.stop()
if page=="Accordi Addendum · Sconto Bilico 2027": bilico_page(user);st.stop()
if page=="Accordi Addendum · Accordo Commerciale 2027": commercial_agreement_page(user);st.stop()
if user.get("role")=="admin":
    admin_dashboard();st.stop()
code=str(user.get("customer_code","")).strip().upper()
record=load_file(code)
if not record: st.warning("Dati non ancora pubblicati per questo cliente.");st.stop()
try:
    data,found_code,name=read_export(io.BytesIO(bytes(record["content"])))
    if found_code!=code: raise ValueError("Il dataset non corrisponde all'utente collegato.")
    dashboard(data,found_code,name);st.caption(f"Ultimo aggiornamento: {record['updated_at']}")
except Exception as exc: st.error(f"Errore nella lettura dei dati: {exc}")
