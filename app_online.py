import hashlib
import hmac
import io
import re

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import text

st.set_page_config(
    page_title="MD Rappresentanze | Clienti",
    page_icon="📊",
    layout="wide",
)

st.markdown(
    """
    <style>
    .stApp {background:#f7f7f7; color:#202124;}
    .block-container {padding-top:1rem; max-width:1500px;}
    [data-testid="stHeader"] {background:#087dac;}
    [data-testid="stSidebar"] {background:#f3f4f6!important; border-right:1px solid #d6dbe1;}
    [data-testid="stSidebar"] * {color:#1f2937!important;}
    .brandline {background:#087dac; color:#fff; padding:12px 18px; border-radius:4px;}
    .titleband {background:#dff27d; text-align:center; font-size:28px; font-weight:900; font-style:italic; padding:5px; margin:8px 0 16px;}
    .months {display:grid; grid-template-columns:repeat(12,1fr); gap:2px;}
    .month {background:#3d3d3f; color:#fff; text-align:center; padding:7px 2px; font-size:12px;}
    .stMetric {background:#fff!important; border:1px solid #d7dde5; border-radius:14px; padding:14px;}
    [data-testid="stMetricLabel"] p {color:#3f4854!important; font-weight:700;}
    [data-testid="stMetricValue"] {color:#4F8FC5!important;}
    [data-testid="stSidebar"] .stButton button {background:#087dac!important; border-color:#087dac!important;}
    [data-testid="stSidebar"] .stButton button p,
    [data-testid="stSidebar"] .stButton button span {color:#ffffff!important; font-weight:700!important;}
    </style>
    """,
    unsafe_allow_html=True,
)

CATEGORIES = ["Applied", "Fancoil", "Home", "Split", "VRF"]
NUMERIC_COLUMNS = [
    "Fatturato APP",
    "% AC / APP",
    "Fatturato AP",
    "% AC / AP",
    "Fatturato AC",
    "Budget",
    "% AC / Budget",
    "K Lordo AP",
    "K Lordo AC",
    "Quantità AP (No-Acc)",
    "Quantità AC (No-Acc)",
]


def password_hash(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def configured_users():
    raw_users = st.secrets.get("users", {})
    return {username: dict(values) for username, values in raw_users.items()}


def authenticate(username, password):
    user = configured_users().get(username.strip())
    if not user:
        return None

    stored_hash = str(user.get("password_sha256", ""))
    calculated_hash = password_hash(password)

    if not hmac.compare_digest(calculated_hash, stored_hash):
        return None

    return user


def get_database():
    return st.connection("postgresql", type="sql")


def ensure_table():
    create_table_query = text(
        """
        CREATE TABLE IF NOT EXISTS customer_exports (
            customer_code TEXT PRIMARY KEY,
            customer_name TEXT NOT NULL,
            filename TEXT NOT NULL,
            content BYTEA NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    with get_database().session as session:
        session.execute(create_table_query)
        session.commit()


def save_customer_file(customer_code, customer_name, filename, content):
    ensure_table()

    save_query = text(
        """
        INSERT INTO customer_exports (
            customer_code,
            customer_name,
            filename,
            content,
            updated_at
        )
        VALUES (
            :customer_code,
            :customer_name,
            :filename,
            :content,
            CURRENT_TIMESTAMP
        )
        ON CONFLICT (customer_code)
        DO UPDATE SET
            customer_name = EXCLUDED.customer_name,
            filename = EXCLUDED.filename,
            content = EXCLUDED.content,
            updated_at = CURRENT_TIMESTAMP
        """
    )

    parameters = {
        "customer_code": customer_code,
        "customer_name": customer_name,
        "filename": filename,
        "content": content,
    }

    with get_database().session as session:
        session.execute(save_query, parameters)
        session.commit()


def load_customer_file(customer_code):
    ensure_table()

    query = text(
        """
        SELECT customer_name, filename, content, updated_at
        FROM customer_exports
        WHERE customer_code = :customer_code
        """
    )

    with get_database().session as session:
        result = session.execute(
            query,
            {"customer_code": customer_code},
        ).mappings().first()

    return result


def read_export(file_object):
    raw = pd.read_excel(
        file_object,
        sheet_name="Export",
        header=None,
        engine="openpyxl",
    )

    header_row = next(
        (
            index
            for index, row in raw.iterrows()
            if row.astype(str).str.strip().eq("Market Catalog").any()
        ),
        None,
    )

    if header_row is None:
        raise ValueError("Intestazione 'Market Catalog' non trovata nel foglio Export.")

    all_text = " ".join(raw.fillna("").astype(str).values.flatten())
    customer_match = re.search(
        r"Customer Full\s+è\s+(.+?)\s+Anno Corrente",
        all_text,
        re.IGNORECASE,
    )

    customer_code = ""
    customer_name = ""

    if customer_match:
        parts = customer_match.group(1).strip().split(" - ", 1)
        customer_code = parts[0].strip()
        customer_name = parts[-1].strip()

    if hasattr(file_object, "seek"):
        file_object.seek(0)

    data = pd.read_excel(
        file_object,
        sheet_name="Export",
        header=header_row,
        engine="openpyxl",
    )

    data.columns = [str(column).strip() for column in data.columns]

    required_columns = ["Market Catalog", "Fatturato AC", "Budget"]
    missing_columns = [column for column in required_columns if column not in data.columns]

    if missing_columns:
        raise ValueError("Colonne mancanti: " + ", ".join(missing_columns))

    data["Market Catalog"] = data["Market Catalog"].astype(str).str.strip()
    data = data[
        data["Market Catalog"].isin(CATEGORIES + ["Total"])
    ].copy()

    for column in NUMERIC_COLUMNS:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce").fillna(0)

    if data.empty:
        raise ValueError("Nel foglio Export non sono presenti categorie valide.")

    if not customer_code:
        raise ValueError("Codice cliente non trovato nella riga 'Filtri applicati'.")

    return data, customer_code, customer_name


def euro(value):
    return f"€ {value:,.0f}".replace(",", ".")


def number(value):
    return f"{value:,.0f}".replace(",", ".")


def percentage(value):
    return f"{value * 100:.1f}%".replace(".", ",")


def show_dashboard(data, customer_code, customer_name):
    categories = data[data["Market Catalog"].isin(CATEGORIES)].copy()
    total_rows = data[data["Market Catalog"] == "Total"]

    if not total_rows.empty:
        total = total_rows.iloc[0]
        fatturato_ac = float(total.get("Fatturato AC", 0))
        budget = float(total.get("Budget", 0))
        quantita_ac = float(total.get("Quantità AC (No-Acc)", 0))
    else:
        fatturato_ac = float(categories["Fatturato AC"].sum())
        budget = float(categories["Budget"].sum())
        quantita_ac = float(categories.get("Quantità AC (No-Acc)", pd.Series(dtype=float)).sum())

    raggiungimento = fatturato_ac / budget if budget else 0

    st.markdown(
        f"""
        <div class="brandline">
            <b>MD Rappresentanze</b>&nbsp;&nbsp;
            Fatturato&nbsp;&nbsp;
            Cliente: {customer_code} - {customer_name}
        </div>
        <div class="titleband">FATTURATO - Prodotti</div>
        """,
        unsafe_allow_html=True,
    )

    mesi = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    st.markdown(
        '<div class="months">'
        + "".join(f'<div class="month">{mese}</div>' for mese in mesi)
        + "</div>",
        unsafe_allow_html=True,
    )

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Fatturato AC", euro(fatturato_ac))
    col2.metric("Budget", euro(budget))
    col3.metric("Raggiungimento budget", percentage(raggiungimento))
    col4.metric("Quantità AC", number(quantita_ac))

    chart = go.Figure(
        go.Pie(
            labels=categories["Market Catalog"],
            values=categories["Fatturato AC"],
            hole=0.48,
            marker_colors=["#168af4", "#163eae", "#ec6b2d", "#760080", "#dd469d"],
            textinfo="label+percent",
        )
    )
    chart.update_layout(
        title="Fatturato AC - Catalogo",
        height=400,
        paper_bgcolor="white",
        margin=dict(l=10, r=10, t=50, b=10),
    )
    st.plotly_chart(chart, use_container_width=True)

    detail_columns = [
        "Market Catalog",
        "Fatturato AC",
        "Budget",
        "% AC / Budget",
        "K Lordo AC",
        "Quantità AC (No-Acc)",
    ]
    available_columns = [column for column in detail_columns if column in categories.columns]
    detail = categories[available_columns].copy()

    if "Fatturato AC" in detail:
        detail["Fatturato AC"] = detail["Fatturato AC"].map(euro)
    if "Budget" in detail:
        detail["Budget"] = detail["Budget"].map(euro)
    if "% AC / Budget" in detail:
        detail["% AC / Budget"] = detail["% AC / Budget"].map(percentage)
    if "K Lordo AC" in detail:
        detail["K Lordo AC"] = detail["K Lordo AC"].map(percentage)
    if "Quantità AC (No-Acc)" in detail:
        detail["Quantità AC (No-Acc)"] = detail["Quantità AC (No-Acc)"].map(number)

    st.dataframe(detail, use_container_width=True, hide_index=True)


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

current_user = st.session_state.user

with st.sidebar:
    st.image("logo_md.png", width=185)
    st.write(f"Utente: **{current_user['username']}**")

    if st.button("Esci"):
        st.session_state.user = None
        st.rerun()

    if current_user.get("role") == "admin":
        page = st.radio("Sezione", ["Dashboard", "Aggiorna dati"])
    else:
        page = "Dashboard"

if page == "Aggiorna dati":
    uploaded_file = st.file_uploader(
        "Carica Excel cliente",
        type=["xlsx"],
    )

    if uploaded_file is not None:
        file_content = uploaded_file.getvalue()

        try:
            _, customer_code, customer_name = read_export(io.BytesIO(file_content))
            save_customer_file(
                customer_code,
                customer_name,
                uploaded_file.name,
                file_content,
            )
            st.success(
                f"Dati aggiornati correttamente per {customer_code} - {customer_name}."
            )
        except Exception as error:
            st.error(f"Impossibile aggiornare i dati: {error}")

    st.stop()

if current_user.get("role") == "admin":
    st.info(
        "Modalità amministratore: usa 'Aggiorna dati' per caricare gli Excel dei clienti. "
        "Per verificare una pagina cliente, esci e accedi con il relativo utente."
    )
    st.stop()

customer_code = str(current_user.get("customer_code", "")).strip()

if not customer_code:
    st.error("Nessun cliente associato a questo utente.")
    st.stop()

stored_file = load_customer_file(customer_code)

if stored_file is None:
    st.warning("Dati non ancora pubblicati per questo cliente.")
    st.stop()

try:
    file_bytes = bytes(stored_file["content"])
    data, detected_code, detected_name = read_export(io.BytesIO(file_bytes))

    if detected_code.lower() != customer_code.lower():
        st.error("Il dataset pubblicato non corrisponde al cliente associato all'utenza.")
        st.stop()

    show_dashboard(data, detected_code, detected_name)
    st.caption(f"Ultimo aggiornamento dati: {stored_file['updated_at']}")
except Exception as error:
    st.error(f"Errore nella lettura dei dati: {error}")
