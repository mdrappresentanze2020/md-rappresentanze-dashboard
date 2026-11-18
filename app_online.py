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
from sqlalchemy.exc import OperationalError

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

def db_connection():
    return st.connection("postgresql", type="sql")

def run_db_with_retry(operation):
    connection = db_connection()
    try:
        return operation(connection)
    except OperationalError:
        # Neon può sospendere il compute o chiudere una connessione inattiva.
        # Reset elimina il pool non più valido e il secondo tentativo apre
        # una connessione nuova, evitando l'errore al primo accesso.
        connection.reset()
        return operation(db_connection())

def ensure_table():
    def operation(connection):
        with connection.session as session:
            session.execute(text("""CREATE TABLE IF NOT EXISTS customer_exports(customer_code TEXT PRIMARY KEY,customer_name TEXT NOT NULL,filename TEXT NOT NULL,content BYTEA NOT NULL,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""))
            session.commit()
    return run_db_with_retry(operation)
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
def load_google_order_pdf(customer_code, order_number):
    config = st.secrets.get("google_pdf", {})
    webapp_url = str(config.get("webapp_url", "")).strip()
    token = str(config.get("token", "")).strip()
    if not webapp_url or not token:
        return None, "Configurazione Google PDF non disponibile."
    params = urllib.parse.urlencode({
        "cliente": str(customer_code).strip().upper(),
        "ordine": str(order_number).strip().upper(),
        "token": token,
    })
    url = webapp_url + ("&" if "?" in webapp_url else "?") + params
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "MD-Rappresentanze-Dashboard/1.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("ok") and payload.get("trovato") and payload.get("pdf"):
            return {
                "filename": payload.get("filename") or f"{order_number}.pdf",
                "content": base64.b64decode(payload["pdf"]),
                "source": "Google Drive",
            }, None
        if payload.get("trovato") is False:
            return None, None
        return None, payload.get("errore") or "Risposta Google PDF non valida."
    except Exception as exc:
        return None, f"Collegamento Google PDF non disponibile: {exc}"

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
    def operation(connection):
        with connection.session as session:
            return session.execute(text("SELECT customer_name,filename,content,updated_at FROM customer_exports WHERE customer_code=:code"), {"code":code}).mappings().first()
    return run_db_with_retry(operation)
def dataset_status():
    ensure_table()
    def operation(connection):
        with connection.session as session:
            rows = session.execute(text("SELECT customer_code,customer_name,filename,updated_at FROM customer_exports")).mappings().all()
            return {r["customer_code"]:r for r in rows}
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

    hvac_catalogs = catalogs[catalogs["Catalogo"].isin(["Home","Split","VRF","Fancoil"])]
    consuntivo_annuale = float(hvac_catalogs["Totale Complessivo"].sum())
    consuntivo_q1 = float((hvac_catalogs["Spedito Q1"] + hvac_catalogs["Da Spedire Q1"]).sum())

    def raggiungimento(consuntivo, target):
        return None if target <= 0 else (consuntivo / target) * 100
    def pct_label(value):
        return "Target da definire" if value is None else f"{value:.1f}%".replace(".", ",")

    r_annuale = raggiungimento(consuntivo_annuale, target_annuale)
    r_q1 = raggiungimento(consuntivo_q1, target_q1)
    t1,t2,t3,t4 = st.columns(4)
    t1.metric("Super Pre Split", euro(target_split), delta="Target da definire" if target_split <= 0 else "Consuntivo da associare", delta_color="off")
    t2.metric("Super Pre Aqua", euro(target_aqua), delta="Target da definire" if target_aqua <= 0 else "Consuntivo da associare", delta_color="off")
    t3.metric("Target annuale Home + Split + VRF + Fancoil", euro(target_annuale), delta=pct_label(r_annuale), delta_color="off")
    t4.metric("Target Q1 Home + Split + VRF + Fancoil", euro(target_q1), delta=pct_label(r_q1), delta_color="off")

    p1,p2 = st.columns(2)
    with p1:
        st.caption(f"Consuntivo Home + Split + VRF + Fancoil: {euro(consuntivo_annuale)}")
        st.progress(min(max((r_annuale or 0)/100,0),1), text=f"Raggiungimento annuale: {pct_label(r_annuale)}")
    with p2:
        st.caption(f"Consuntivo Q1 Home + Split + VRF + Fancoil: {euro(consuntivo_q1)}")
        st.progress(min(max((r_q1 or 0)/100,0),1), text=f"Raggiungimento Q1: {pct_label(r_q1)}")

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
    order_options = catalog_orders["Numero Ordine"].dropna().astype(str).tolist()

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
                annuale=c3.number_input("Target annuale Home + Split + VRF + Fancoil (€)",min_value=0.0,value=float(t.get("target_annuale_hvac",0) or 0),step=1000.0,format="%.2f")
                q1=c4.number_input("Target Q1 Home + Split + VRF + Fancoil (€)",min_value=0.0,value=float(t.get("target_q1_hvac",0) or 0),step=1000.0,format="%.2f")
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
    pages=["Dashboard","Aggiorna dati","Gestione clienti","Target annuali","Gestione PDF conferme"] if user.get("role")=="admin" else ["Dashboard"]
    page=st.radio("Sezione",pages)

if page=="Aggiorna dati": upload_page();st.stop()
if page=="Gestione clienti": client_management();st.stop()
if page=="Target annuali": targets_page();st.stop()
if page=="Gestione PDF conferme": order_pdfs_page();st.stop()
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
