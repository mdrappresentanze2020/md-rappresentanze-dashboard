import hashlib
import hmac
import io
import os
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


def secure_password(password, salt=None):
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, 250_000
    )
    return salt.hex(), digest.hex()


def verify_secure_password(password, salt_hex, digest_hex):
    try:
        salt = bytes.fromhex(salt_hex)
        _, calculated = secure_password(password, salt)
        return hmac.compare_digest(calculated, digest_hex)
    except (TypeError, ValueError):
        return False


def configured_users():
    raw_users = st.secrets.get("users", {})
    return {username: dict(values) for username, values in raw_users.items()}


def get_database():
    return st.connection("postgresql", type="sql")


def ensure_users_table():
    query = text("""
        CREATE TABLE IF NOT EXISTS app_users (
            username TEXT PRIMARY KEY,
            password_salt TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'cliente',
            customer_code TEXT NOT NULL DEFAULT '',
            customer_name TEXT NOT NULL DEFAULT '',
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    with get_database().session as session:
        session.execute(query)
        session.commit()


def authenticate(username, password):
    username = username.strip().lower()
    ensure_users_table()
    query = text("""
        SELECT username, password_salt, password_hash, role,
               customer_code, customer_name, active
        FROM app_users
        WHERE lower(username) = :username
    """)
    with get_database().session as session:
        row = session.execute(query, {"username": username}).mappings().first()

    if row:
        if not row["active"]:
            return None
        if verify_secure_password(password, row["password_salt"], row["password_hash"]):
            return {
                "role": row["role"],
                "customer_code": row["customer_code"],
                "customer_name": row["customer_name"],
            }
        return None

    # Compatibilità iniziale con gli utenti già salvati nei Secrets.
    secret_user = configured_users().get(username)
    if not secret_user:
        return None
    stored_hash = str(secret_user.get("password_sha256", ""))
    if not hmac.compare_digest(password_hash(password), stored_hash):
        return None
    return secret_user


def create_or_update_user(username, password, role, customer_code, customer_name):
    ensure_users_table()
    username = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{3,40}", username):
        raise ValueError("Lo username deve avere 3-40 caratteri: lettere, numeri, punto, trattino o underscore.")
    if len(password) < 10:
        raise ValueError("La password deve contenere almeno 10 caratteri.")
    if role == "cliente" and not customer_code.strip():
        raise ValueError("Per un cliente il codice cliente è obbligatorio.")
    salt, digest = secure_password(password)
    query = text("""
        INSERT INTO app_users (
            username, password_salt, password_hash, role,
            customer_code, customer_name, active, updated_at
        ) VALUES (
            :username, :salt, :digest, :role,
            :customer_code, :customer_name, TRUE, CURRENT_TIMESTAMP
        )
        ON CONFLICT (username) DO UPDATE SET
            password_salt = EXCLUDED.password_salt,
            password_hash = EXCLUDED.password_hash,
            role = EXCLUDED.role,
            customer_code = EXCLUDED.customer_code,
            customer_name = EXCLUDED.customer_name,
            active = TRUE,
            updated_at = CURRENT_TIMESTAMP
    """)
    params = {
        "username": username,
        "salt": salt,
        "digest": digest,
        "role": role,
        "customer_code": customer_code.strip(),
        "customer_name": customer_name.strip(),
    }
    with get_database().session as session:
        session.execute(query, params)
        session.commit()


def set_user_active(username, active):
    ensure_users_table()
    query = text("""
        UPDATE app_users SET active = :active, updated_at = CURRENT_TIMESTAMP
        WHERE username = :username
    """)
    with get_database().session as session:
        session.execute(query, {"active": active, "username": username})
        session.commit()


def get_users_dataframe():
    ensure_users_table()
    query = text("""
        SELECT username, role, customer_code, customer_name, active, updated_at
        FROM app_users ORDER BY username
    """)
    with get_database().session as session:
        rows = session.execute(query).mappings().all()
    return pd.DataFrame(rows)


def show_user_management():
    st.subheader("Gestione clienti e utenti")
    st.caption("Crea un nuovo accesso oppure sovrascrivi un utente esistente usando lo stesso username.")

    with st.form("user_management_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        username = col1.text_input("Username")
        password = col2.text_input("Password", type="password")
        col3, col4 = st.columns(2)
        role = col3.selectbox("Ruolo", ["cliente", "admin"])
        customer_code = col4.text_input("Codice cliente", help="Esempio: M7003635")
        customer_name = st.text_input("Ragione sociale", help="Esempio: ERRECLIMA SRL")
        submitted = st.form_submit_button("Salva utente", type="primary")

        if submitted:
            try:
                create_or_update_user(
                    username, password, role, customer_code, customer_name
                )
                st.success(f"Utente {username.strip().lower()} salvato correttamente.")
            except Exception as error:
                st.error(str(error))

    users_df = get_users_dataframe()
    if users_df.empty:
        st.info("Non ci sono ancora utenti creati nel database.")
    else:
        st.dataframe(users_df, use_container_width=True, hide_index=True)
        usernames = users_df["username"].tolist()
        selected = st.selectbox("Utente da attivare/disattivare", usernames)
        col1, col2 = st.columns(2)
        if col1.button("Attiva utente"):
            set_user_active(selected, True)
            st.success(f"Utente {selected} attivato.")
            st.rerun()
        if col2.button("Disattiva utente"):
            if selected == st.session_state.user.get("username"):
                st.error("Non puoi disattivare l'utente con cui sei connesso.")
            else:
                set_user_active(selected, False)
                st.success(f"Utente {selected} disattivato.")
                st.rerun()


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
        page = st.radio("Sezione", ["Dashboard", "Aggiorna dati", "Gestione utenti"])
    else:
        page = "Dashboard"

if page == "Gestione utenti":
    show_user_management()
    st.stop()

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
