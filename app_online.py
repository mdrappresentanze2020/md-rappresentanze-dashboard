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
.stApp{background:#f7f7f7;color:#202124}.block-container{padding-top:1rem;max-width:1500px}
[data-testid="stHeader"]{background:#087dac}[data-testid="stSidebar"]{background:#f3f4f6!important;border-right:1px solid #d6dbe1}
[data-testid="stSidebar"] *{color:#1f2937!important}.brandline{background:#087dac;color:#fff;padding:12px 18px;border-radius:4px}
.titleband{background:#dff27d;text-align:center;font-size:28px;font-weight:900;font-style:italic;padding:5px;margin:8px 0 16px}
.months{display:grid;grid-template-columns:repeat(12,1fr);gap:2px}.month{background:#3d3d3f;color:#fff;text-align:center;padding:7px 2px;font-size:12px}
.stMetric{background:#fff!important;border:1px solid #d7dde5;border-radius:14px;padding:14px}
[data-testid="stMetricLabel"] p{color:#3f4854!important;font-weight:700}[data-testid="stMetricValue"]{color:#4F8FC5!important}
</style>""", unsafe_allow_html=True)

CATEGORIES = ["Applied", "Fancoil", "Home", "Split", "VRF"]
NUMERIC_COLUMNS = [
    "Fatturato APP", "% AC / APP", "Fatturato AP", "% AC / AP",
    "Fatturato AC", "Budget", "% AC / Budget", "K Lordo AP",
    "K Lordo AC", "Quantità AP (No-Acc)", "Quantità AC (No-Acc)"
]

def password_hash(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def configured_users():
    return {name: dict(values) for name, values in st.secrets.get("users", {}).items()}

def authenticate(username, password):
    user = configured_users().get(username.strip())
    if not user:
        return None
    expected = str(user.get("password_sha256", ""))
    return user if hmac.compare_digest(password_hash(password), expected) else None

def db_connection():
    return st.connection("postgresql", type="sql")

def ensure_table():
    with db_connection().session as session:
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS customer_exports(
                customer_code TEXT PRIMARY KEY,
                customer_name TEXT NOT NULL,
                filename TEXT NOT NULL,
                content BYTEA NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """))
        session.commit()

def save_file(customer_code, customer_name, filename, content):
    ensure_table()
    with db_connection().session as session:
        session.execute(text("""
            INSERT INTO customer_exports(customer_code, customer_name, filename, content, updated_at)
            VALUES(:code, :name, :filename, :content, CURRENT_TIMESTAMP)
            ON CONFLICT(customer_code) DO UPDATE SET
                customer_name=:name, filename=:filename, content=:content,
                updated_at=CURRENT_TIMESTAMP
        """), {"code": customer_code, "name": customer_name,
                "filename": filename, "content": content})
        session.commit()

def load_file(customer_code):
    ensure_table()
    with db_connection().session as session:
        row = session.execute(text("""
            SELECT customer_name, filename, content, updated_at
            FROM customer_exports WHERE customer_code=:code
        """), {"code": customer_code}).mappings().first()
    return row

def normalize(value):
    if value is None or pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\n", " ")).strip()

def read_export(file_obj):
    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    raw = pd.read_excel(file_obj, sheet_name="Export", header=None, engine="openpyxl")
    if raw.empty:
        raise ValueError("Il foglio Export è vuoto.")

    # Cerca l'intestazione reale: alcuni export sono per catalogo, altri sono riepiloghi per cliente.
    header_idx = None
    first_column = None
    for idx, row in raw.iterrows():
        values = [normalize(v) for v in row.tolist()]
        if "Market Catalog" in values:
            header_idx, first_column = idx, "Market Catalog"
            break
        if "Cliente" in values:
            header_idx, first_column = idx, "Cliente"
            break
    if header_idx is None:
        raise ValueError("Intestazione 'Market Catalog' o 'Cliente' non trovata nel foglio Export.")

    # Cerca Customer Full in qualsiasi cella e accetta testo prima/dopo il filtro.
    all_text = " ".join(normalize(v) for v in raw.fillna("").to_numpy().flatten())
    match = re.search(
        r"Customer\s+Full\s*(?:è|=|:)\s*(M\d+)\s*-\s*(.+?)(?=\s+(?:Bill\s+Month(?:\s+Short)?|Anno\s+Corrente|Market\s+Catalog)\s*(?:è|=|:)|$)",
        all_text, flags=re.IGNORECASE
    )
    if not match:
        raise ValueError("Codice cliente non trovato nel filtro 'Customer Full'.")
    customer_code = match.group(1).strip().upper()
    customer_name = match.group(2).strip(" -")

    if hasattr(file_obj, "seek"):
        file_obj.seek(0)
    data = pd.read_excel(file_obj, sheet_name="Export", header=header_idx, engine="openpyxl")
    data.columns = [normalize(c) for c in data.columns]
    if first_column not in data.columns:
        raise ValueError(f"Colonna '{first_column}' non trovata dopo la lettura dell'intestazione.")

    for col in NUMERIC_COLUMNS:
        if col not in data.columns:
            data[col] = 0.0
        data[col] = pd.to_numeric(data[col], errors="coerce").fillna(0.0)

    data[first_column] = data[first_column].map(normalize)
    if first_column == "Market Catalog":
        data = data[data[first_column].isin(CATEGORIES + ["Total"])].copy()
        data = data.rename(columns={first_column: "Voce"})
        mode = "categorie"
    else:
        # Gli export Addessi/De Gregoris sono riepiloghi cliente senza ripartizione per catalogo.
        data = data[(data[first_column] != "") & (~data[first_column].str.startswith("Filtri applicati", na=False))].copy()
        data = data.rename(columns={first_column: "Voce"})
        mode = "riepilogo"

    if data.empty:
        raise ValueError("Nessuna riga dati valida trovata nel foglio Export.")
    return data, customer_code, customer_name, mode

def euro(value):
    return f"€ {float(value):,.0f}".replace(",", ".")

def number(value):
    return f"{float(value):,.0f}".replace(",", ".")

def percent(value):
    return f"{float(value) * 100:.1f}%".replace(".", ",")

def dashboard(data, code, name, mode):
    total_rows = data[data["Voce"] == "Total"]
    detail = data[data["Voce"] != "Total"].copy()
    total = total_rows.iloc[0] if not total_rows.empty else detail.sum(numeric_only=True)
    ac = float(total.get("Fatturato AC", 0))
    budget = float(total.get("Budget", 0))
    quantity = float(total.get("Quantità AC (No-Acc)", 0))

    st.markdown(
        f'<div class="brandline"><b>MD Rappresentanze</b>&nbsp;&nbsp; Fatturato '
        f'&nbsp;&nbsp; Cliente: {code} - {name}</div><div class="titleband">FATTURATO - Prodotti</div>',
        unsafe_allow_html=True
    )
    months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    st.markdown('<div class="months">' + ''.join(f'<div class="month">{m}</div>' for m in months) + '</div>', unsafe_allow_html=True)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Fatturato AC", euro(ac))
    c2.metric("Budget", euro(budget) if budget else "Non disponibile")
    c3.metric("Raggiungimento budget", percent(ac / budget) if budget else "Non disponibile")
    c4.metric("Quantità AC", number(quantity))

    if mode == "categorie" and not detail.empty:
        fig = go.Figure(go.Pie(
            labels=detail["Voce"], values=detail["Fatturato AC"], hole=.48,
            marker_colors=["#168af4", "#163eae", "#ec6b2d", "#760080", "#dd469d"],
            textinfo="label+percent"
        ))
        fig.update_layout(title="Fatturato AC - Catalogo", height=400, paper_bgcolor="white")
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("Questo export contiene il riepilogo complessivo del cliente, senza dettaglio per Market Catalog.")

    display = detail[["Voce", "Fatturato AC", "Budget", "% AC / Budget", "K Lordo AC", "Quantità AC (No-Acc)"]].copy()
    display["Fatturato AC"] = display["Fatturato AC"].map(euro)
    display["Budget"] = display["Budget"].map(lambda v: euro(v) if v else "-")
    display["% AC / Budget"] = display["% AC / Budget"].map(lambda v: percent(v) if v else "-")
    display["K Lordo AC"] = display["K Lordo AC"].map(percent)
    display["Quantità AC (No-Acc)"] = display["Quantità AC (No-Acc)"].map(number)
    st.dataframe(display, use_container_width=True, hide_index=True)

st.image("logo_md.png", width=300)
st.session_state.setdefault("user", None)

if not st.session_state.user:
    with st.form("login"):
        username = st.text_input("Nome utente")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Accedi", type="primary"):
            user = authenticate(username, password)
            if user:
                st.session_state.user = {"username": username, **user}
                st.rerun()
            else:
                st.error("Credenziali non valide.")
    st.stop()

user = st.session_state.user
with st.sidebar:
    st.image("logo_md.png", use_container_width=True)
    st.write(f"Utente: **{user['username']}**")
    if st.button("Esci"):
        st.session_state.user = None
        st.rerun()
    page = st.radio("Sezione", ["Dashboard", "Aggiorna dati"] if user.get("role") == "admin" else ["Dashboard"])

if page == "Aggiorna dati":
    st.title("Aggiorna dati clienti")
    uploads = st.file_uploader(
        "Carica uno o più file Excel cliente", type=["xlsx"], accept_multiple_files=True
    )
    if st.button("Elabora e salva tutti i file", type="primary", disabled=not uploads):
        successes, failures = [], []
        for upload in uploads:
            try:
                content = upload.getvalue()
                _, code, name, _ = read_export(io.BytesIO(content))
                save_file(code, name, upload.name, content)
                successes.append(f"{upload.name}: {code} - {name}")
            except Exception as exc:
                failures.append(f"{upload.name}: {exc}")
        if successes:
            st.success(f"Elaborati e salvati {len(successes)} file su {len(uploads)}.")
            for item in successes:
                st.write("• " + item)
        if failures:
            st.error(f"Non elaborati {len(failures)} file su {len(uploads)}.")
            for item in failures:
                st.write("• " + item)
    st.stop()

customer_code = str(user.get("customer_code", "")).strip().upper()
if not customer_code:
    st.error("Nessun cliente associato a questo utente.")
    st.stop()
record = load_file(customer_code)
if not record:
    st.warning("Dati non ancora pubblicati per questo cliente.")
    st.stop()
try:
    data, code, name, mode = read_export(io.BytesIO(bytes(record["content"])))
    dashboard(data, code, name, mode)
    st.caption(f"Ultimo aggiornamento dati: {record['updated_at']}")
except Exception as exc:
    st.error(f"Errore nella lettura dei dati: {exc}")
