import streamlit as st
import libsql_experimental as libsql
import pandas as pd
import base64
from datetime import datetime, date, timedelta

# ── Config ──
st.set_page_config(page_title="Visa Vista CRM", page_icon="🌍", layout="wide")

TURSO_URL = st.secrets["TURSO_URL"]
TURSO_TOKEN = st.secrets["TURSO_TOKEN"]

VISA_TYPES = ["Tourist", "Work", "Student", "Business", "Family", "Transit"]
COUNTRIES = ["UK", "USA", "Canada", "Schengen", "Australia", "UAE", "Other"]
STATUSES = ["NEW", "DEPOSIT_RECEIVED", "APPOINTMENT_BOOKED", "DOCS_HANDED", "PAYMENT_COMPLETE", "SUBMITTED", "APPROVED", "REJECTED"]
STATUS_COLORS = {"NEW": "🔵", "DEPOSIT_RECEIVED": "🟡", "APPOINTMENT_BOOKED": "🟣", "DOCS_HANDED": "🤝", "PAYMENT_COMPLETE": "✅", "SUBMITTED": "🏛️", "APPROVED": "🎉", "REJECTED": "❌"}
PAYMENT_METHODS = ["Cash", "Bank Transfer", "Card", "Online"]
MIN_FEE = 150.0

# ── Database ──
def get_db():
    if 'db_conn' not in st.session_state:
        conn = libsql.connect("visa_crm.db", sync_url=TURSO_URL, auth_token=TURSO_TOKEN)
        conn.sync()
        st.session_state['db_conn'] = conn
    return st.session_state['db_conn']

def sync_db():
    """Call this after any write operation"""
    conn = get_db()
    conn.sync()

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS staff (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            role TEXT NOT NULL,
            active INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            phone TEXT NOT NULL,
            email TEXT NOT NULL,
            passport_number TEXT NOT NULL,
            nationality TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_ref TEXT UNIQUE,
            client_id INTEGER REFERENCES clients(id),
            salesperson_id INTEGER REFERENCES staff(id),
            visa_type TEXT,
            destination TEXT,
            status TEXT DEFAULT 'NEW',
            total_fee REAL DEFAULT 0,
            fee_override_reason TEXT,
            appointment_date TEXT,
            docs_handed INTEGER DEFAULT 0,
            docs_handed_at TEXT,
            docs_handed_by INTEGER REFERENCES staff(id),
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER REFERENCES cases(id),
            amount REAL NOT NULL,
            method TEXT,
            received_by INTEGER REFERENCES staff(id),
            payment_date TEXT,
            proof_image TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS activity_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            case_id INTEGER REFERENCES cases(id),
            action TEXT,
            details TEXT,
            performed_by TEXT,
            timestamp TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    sync_db()

init_db()

# ── Helpers ──
def run_query(query, params=(), fetch=True):
    conn = get_db()
    if fetch:
        c = conn.cursor()
        c.execute(query, params)
        rows = c.fetchall()
        cols = [d[0] for d in c.description] if c.description else []
        df = pd.DataFrame(rows, columns=cols) if cols else pd.DataFrame()
        return df
    else:
        conn.execute(query, params)
        conn.commit()
        sync_db()

def get_next_case_ref():
    yr = datetime.now().year
    df = run_query("SELECT COUNT(*) as cnt FROM cases WHERE case_ref LIKE ?", (f"VIS-{yr}-%",))
    return f"VIS-{yr}-{df['cnt'].iloc[0]+1:04d}"

def log_activity(case_id, action, details="", performed_by="System"):
    run_query("INSERT INTO activity_log (case_id, action, details, performed_by) VALUES (?,?,?,?)",
              (case_id, action, details, performed_by), fetch=False)

@st.cache_data(ttl=30)
def get_staff_list():
    df = run_query("SELECT id, name FROM staff WHERE active=1")
    return dict(zip(df['name'], df['id'])) if not df.empty else {}

def get_case_balance(case_id):
    fee = run_query("SELECT total_fee FROM cases WHERE id=?", (case_id,))['total_fee'].iloc[0]
    paid = run_query("SELECT COALESCE(SUM(amount),0) as paid FROM payments WHERE case_id=?", (case_id,))['paid'].iloc[0]
    return fee, paid, fee - paid

@st.cache_data(ttl=30)
def get_upcoming_appointments():
    today = date.today()
    return run_query("""
        SELECT c.id, c.case_ref, c.appointment_date, c.status, cl.full_name as client, cl.phone
        FROM cases c LEFT JOIN clients cl ON c.client_id = cl.id
        WHERE c.appointment_date IS NOT NULL AND c.appointment_date != ''
          AND c.status NOT IN ('APPROVED', 'REJECTED')
          AND DATE(c.appointment_date) BETWEEN DATE(?) AND DATE(?)
        ORDER BY c.appointment_date ASC
    """, (today.isoformat(), (today + timedelta(days=3)).isoformat()))

@st.cache_data(ttl=30)
def has_appointment_tomorrow():
    today = date.today()
    return run_query("""
        SELECT c.case_ref, cl.full_name, c.appointment_date
        FROM cases c LEFT JOIN clients cl ON c.client_id = cl.id
        WHERE DATE(c.appointment_date) IN (DATE(?), DATE(?))
          AND c.status NOT IN ('APPROVED', 'REJECTED')
    """, (today.isoformat(), (today + timedelta(days=1)).isoformat()))

@st.cache_data(ttl=30)
def get_idle_cases():
    return run_query("""
        SELECT c.case_ref, cl.full_name as client, c.status, c.updated_at,
               s.name as salesperson, cl.phone,
               CAST(julianday('now') - julianday(c.updated_at) AS INTEGER) as days_idle
        FROM cases c
        LEFT JOIN clients cl ON c.client_id = cl.id
        LEFT JOIN staff s ON c.salesperson_id = s.id
        WHERE c.status NOT IN ('PAYMENT_COMPLETE', 'SUBMITTED', 'APPROVED', 'REJECTED')
          AND julianday('now') - julianday(c.updated_at) >= 2
        ORDER BY days_idle DESC
    """)

# ── Sidebar ──
st.sidebar.title("🌍 Visa Vista CRM")
staff_names = get_staff_list()
if staff_names:
    current_user = st.sidebar.selectbox("Logged in as", list(staff_names.keys()))
else:
    current_user = "Admin"
    st.sidebar.info("Add staff in Settings first")

upcoming = get_upcoming_appointments()
if not upcoming.empty:
    st.sidebar.divider()
    st.sidebar.subheader("🚨 Appointments")
    today = date.today()
    for _, apt in upcoming.iterrows():
        days_left = (date.fromisoformat(apt['appointment_date']) - today).days
        lbl = {0: "🔴 TODAY", 1: "🔴 TOMORROW", 2: "🟠 2 days", 3: "🟡 3 days"}.get(days_left, f"{days_left}d")
        if days_left <= 1:
            st.sidebar.error(f"{lbl} — {apt['client']} ({apt['case_ref']})")
        else:
            st.sidebar.warning(f"{lbl} — {apt['client']} ({apt['case_ref']})")

idle = get_idle_cases()
if not idle.empty:
    st.sidebar.divider()
    st.sidebar.subheader(f"🔔 Updates Needed ({len(idle)})")
    for _, row in idle.iterrows():
        st.sidebar.warning(f"⏰ {row['client']} — {row['days_idle']}d idle")

st.sidebar.divider()
page = st.sidebar.radio("Navigate", [
    "➕ New Case",
    "📊 Dashboard",
    "📁 Cases",
    "👥 Clients",
    "💰 Payments",
    "⚙️ Settings"
])

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ➕ NEW CASE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
if page == "➕ New Case":
    st.title("➕ New Case")

    appt_block = has_appointment_tomorrow()
    if not appt_block.empty:
        st.error("🚫 **New case creation blocked — appointment(s) within 24 hours:**")
        for _, ab in appt_block.iterrows():
            st.error(f"→ **{ab['case_ref']}** — {ab['full_name']} — Appointment: {ab['appointment_date']}")
        st.info("Focus on preparing these clients. New cases can be created after.")
        st.stop()

    st.subheader("1. Check Existing Client")
    lookup_name = st.text_input("Enter client name to check for duplicates", key="lookup_name")

    existing_client_id = None
    use_existing = False

    if lookup_name and len(lookup_name.strip()) > 1:
        name_matches = run_query("SELECT id, full_name, phone, email, passport_number FROM clients WHERE LOWER(full_name) = LOWER(?)", (lookup_name.strip(),))
        if not name_matches.empty:
            st.warning(f"⚠️ **{len(name_matches)} existing client(s) found:**")
            for _, match in name_matches.iterrows():
                st.write(f"• **{match['full_name']}** — 📞 {match['phone']} — 🛂 {match['passport_number']}")

            if len(name_matches) == 1:
                existing_client_id = name_matches.iloc[0]['id']
            elif len(name_matches) > 1:
                st.info("Multiple clients with this name — select by passport number below.")
                passport_lookup = st.text_input("Enter passport number to identify", key="passport_lookup")
                if passport_lookup:
                    match_row = name_matches[name_matches['passport_number'].str.upper() == passport_lookup.strip().upper()]
                    if not match_row.empty:
                        existing_client_id = match_row.iloc[0]['id']
                        st.success(f"✅ Matched: {match_row.iloc[0]['full_name']} — {match_row.iloc[0]['passport_number']}")
                    else:
                        st.info("No passport match — a new client will be created.")

            if existing_client_id:
                link_choice = st.radio("What would you like to do?",
                    ["Create a new case for this existing client", "This is a different person — create new client"], key="link_choice")
                if link_choice == "Create a new case for this existing client":
                    use_existing = True
        else:
            st.success("✅ No existing client found — new client will be created.")

    st.subheader("2. Client & Visa Details")

    with st.form("new_case_form", clear_on_submit=True):
        col1, col2 = st.columns(2)
        name = col1.text_input("Full Name *", value=lookup_name.strip() if lookup_name else "")
        phone = col2.text_input("Phone *")
        email = col1.text_input("Email *")
        passport = col2.text_input("Passport Number *")
        nationality = col1.text_input("Nationality *")

        st.markdown("---")
        col3, col4 = st.columns(2)
        visa_type = col3.selectbox("Visa Type", VISA_TYPES)
        destination = col4.selectbox("Destination", COUNTRIES)
        total_fee = col3.number_input("Total Fee (£)", min_value=0.0, step=50.0)
        salesperson = col4.selectbox("Salesperson", list(staff_names.keys()) if staff_names else ["No staff - add in Settings"])
        notes = st.text_area("Notes")
        deposit = st.number_input("Initial Deposit (£, leave 0 if none)", min_value=0.0, step=50.0)
        deposit_proof = st.file_uploader("Upload deposit proof (screenshot)", type=["png", "jpg", "jpeg"], key="deposit_proof")

        fee_override_reason = ""
        if 0 < total_fee < MIN_FEE:
            st.warning(f"⚠️ Fee below minimum £{MIN_FEE:.0f} — override reason required.")
            fee_override_reason = st.text_input("Override Reason *", key="fee_override")

        submitted = st.form_submit_button("✅ Create Case", type="primary")

    if submitted:
        missing = []
        if not name or not name.strip(): missing.append("Full Name")
        if not phone or not phone.strip(): missing.append("Phone")
        if not email or not email.strip(): missing.append("Email")
        if not passport or not passport.strip(): missing.append("Passport Number")
        if not nationality or not nationality.strip(): missing.append("Nationality")

        if missing:
            st.error(f"❌ Required fields missing: **{', '.join(missing)}**")
        elif not staff_names:
            st.error("❌ Add staff members in Settings first.")
        elif total_fee <= 0:
            st.error("❌ Total fee must be greater than £0.")
        elif 0 < total_fee < MIN_FEE and not fee_override_reason.strip():
            st.error(f"❌ Fee below £{MIN_FEE:.0f} — override reason required.")
        else:
            create_ok = True
            passport_exists = run_query("SELECT id, full_name FROM clients WHERE UPPER(passport_number) = UPPER(?)", (passport.strip(),))

            if not use_existing and not passport_exists.empty:
                existing_name = passport_exists['full_name'].iloc[0]
                if existing_name.strip().lower() != name.strip().lower():
                    st.error(f"❌ Passport **{passport.strip()}** already registered to **{existing_name}**.")
                    create_ok = False
                else:
                    use_existing = True
                    existing_client_id = passport_exists['id'].iloc[0]

            if create_ok:
                conn = get_db()
                c = conn.cursor()

                if use_existing and existing_client_id:
                    client_id = existing_client_id
                else:
                    c.execute("INSERT INTO clients (full_name, phone, email, passport_number, nationality) VALUES (?,?,?,?,?)",
                              (name.strip(), phone.strip(), email.strip(), passport.strip(), nationality.strip()))
                    client_id = c.lastrowid

                case_ref = get_next_case_ref()
                sp_id = staff_names.get(salesperson)
                status = "DEPOSIT_RECEIVED" if deposit > 0 else "NEW"
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                override = fee_override_reason.strip() if fee_override_reason else None
                c.execute("INSERT INTO cases (case_ref, client_id, salesperson_id, visa_type, destination, status, total_fee, fee_override_reason, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (case_ref, client_id, sp_id, visa_type, destination, status, total_fee, override, notes, now, now))
                case_id = c.lastrowid

                if deposit > 0:
                    proof_b64 = None
                    if deposit_proof:
                        proof_b64 = base64.b64encode(deposit_proof.read()).decode('utf-8')
                    c.execute("INSERT INTO payments (case_id, amount, method, received_by, payment_date, proof_image, notes) VALUES (?,?,?,?,?,?,?)",
                              (case_id, deposit, "Cash", sp_id, now, proof_b64, "Initial deposit"))

                conn.commit()
                sync_db()
                override_note = f" [FEE OVERRIDE: {override}]" if override else ""
                log_activity(case_id, "CASE_CREATED", f"Created by {current_user}, fee: £{total_fee}{override_note}", current_user)
                st.success(f"✅ Case **{case_ref}** created for **{name}**!")
                st.balloons()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 📊 DASHBOARD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
elif page == "📊 Dashboard":
    st.title("📊 Dashboard")

    cases = run_query("SELECT * FROM cases")
    if cases.empty:
        st.info("No cases yet — hit ➕ New Case to get started!")
    else:
        total_fees = cases['total_fee'].sum()
        total_paid = run_query("SELECT COALESCE(SUM(amount),0) as s FROM payments")['s'].iloc[0]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Active Cases", len(cases[~cases['status'].isin(['APPROVED', 'REJECTED'])]))
        c2.metric("Total Revenue", f"£{total_fees:,.0f}")
        c3.metric("Collected", f"£{total_paid:,.0f}")
        c4.metric("Outstanding", f"£{total_fees - total_paid:,.0f}")

        st.subheader("📈 Sales Performance")
        sales_data = run_query("""
            SELECT s.name as salesperson,
                   COUNT(c.id) as cases_sold,
                   COALESCE(SUM(c.total_fee), 0) as total_revenue,
                   COALESCE(SUM(p.paid), 0) as amount_realised
            FROM cases c
            LEFT JOIN staff s ON c.salesperson_id = s.id
            LEFT JOIN (SELECT case_id, SUM(amount) as paid FROM payments GROUP BY case_id) p ON c.id = p.case_id
            WHERE s.name IS NOT NULL
            GROUP BY s.name ORDER BY total_revenue DESC
        """)

        if not sales_data.empty:
            col_chart, col_table = st.columns([1, 1])
            with col_chart:
                st.write("**Cases by Salesperson**")
                st.bar_chart(sales_data.set_index('salesperson')['cases_sold'], horizontal=True)
                st.write("**Revenue: Total vs Collected**")
                chart_df = sales_data[['salesperson', 'total_revenue', 'amount_realised']].set_index('salesperson')
                chart_df.columns = ['Total Fee', 'Collected']
                st.bar_chart(chart_df)
            with col_table:
                st.write("**Breakdown**")
                display_df = sales_data.copy()
                display_df['collection_rate'] = (display_df['amount_realised'] / display_df['total_revenue'].replace(0, 1) * 100).round(1).astype(str) + '%'
                display_df.columns = ['Salesperson', 'Cases', 'Revenue (£)', 'Collected (£)', 'Rate']
                st.dataframe(display_df, use_container_width=True, hide_index=True)

        st.subheader("Pipeline")
        status_counts = cases['status'].value_counts().reindex(STATUSES, fill_value=0)
        cols = st.columns(len(STATUSES))
        for i, s in enumerate(STATUSES):
            cols[i].metric(f"{STATUS_COLORS.get(s,'')} {s.replace('_',' ').title()}", int(status_counts.get(s, 0)))

        if not upcoming.empty:
            st.subheader("📅 Upcoming Appointments")
            today = date.today()
            for _, apt in upcoming.iterrows():
                days_left = (date.fromisoformat(apt['appointment_date']) - today).days
                fee, paid, bal = get_case_balance(apt['id'])
                bal_text = f" | **£{bal:.0f} outstanding!**" if bal > 0 else " | ✅ Paid"
                docs_ok = run_query("SELECT docs_handed FROM cases WHERE id=?", (apt['id'],))['docs_handed'].iloc[0]
                docs_text = " | ✅ Docs given" if docs_ok else " | ❌ **Docs pending!**"
                msg = f"{apt['client']} ({apt['case_ref']}) — 📞 {apt['phone']}{bal_text}{docs_text}"
                if days_left <= 1:
                    st.error(f"🔴 **{'TODAY' if days_left==0 else 'TOMORROW'}** — {msg}")
                elif days_left == 2:
                    st.warning(f"🟠 **In 2 days** — {msg}")
                else:
                    st.warning(f"🟡 **In 3 days** — {msg}")

        if not idle.empty:
            st.subheader("🔔 Idle Cases — Update Required")
            st.caption("Cases with no progress for 2+ days.")
            for _, row in idle.iterrows():
                urgency = "🔴" if row['days_idle'] >= 5 else "🟠" if row['days_idle'] >= 3 else "🟡"
                st.warning(f"{urgency} **{row['case_ref']}** — {row['client']} — **{row['status']}** for **{row['days_idle']} days** — {row['salesperson']} — 📞 {row['phone']}")

        st.subheader("⚠️ Other Alerts")
        alert_count = 0
        for _, case in cases.iterrows():
            if case['status'] not in ['APPROVED', 'REJECTED', 'NEW']:
                fee, paid, bal = get_case_balance(case['id'])
                if case['docs_handed'] and bal > 0:
                    st.warning(f"💰 **{case['case_ref']}** — Docs handed but **£{bal:.0f}** outstanding!")
                    alert_count += 1
        if alert_count == 0:
            st.success("All clear — no outstanding issues.")

        st.subheader("Recent Cases")
        recent = run_query("""
            SELECT c.case_ref, cl.full_name as client, c.visa_type, c.destination,
                   c.status, c.total_fee, c.appointment_date, c.docs_handed, s.name as salesperson
            FROM cases c LEFT JOIN clients cl ON c.client_id = cl.id
            LEFT JOIN staff s ON c.salesperson_id = s.id
            ORDER BY c.created_at DESC LIMIT 15
        """)
        if not recent.empty:
            st.dataframe(recent, use_container_width=True, hide_index=True)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 📁 CASES
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
elif page == "📁 Cases":
    st.title("📁 Case Manager")

    cases = run_query("""
        SELECT c.id, c.case_ref, cl.full_name as client, c.visa_type, c.destination,
               c.status, c.total_fee, c.fee_override_reason, s.name as salesperson, c.notes, c.updated_at,
               c.appointment_date, c.docs_handed, c.docs_handed_at, dh.name as docs_handed_by_name
        FROM cases c
        LEFT JOIN clients cl ON c.client_id = cl.id
        LEFT JOIN staff s ON c.salesperson_id = s.id
        LEFT JOIN staff dh ON c.docs_handed_by = dh.id
        ORDER BY c.created_at DESC
    """)

    if cases.empty:
        st.info("No cases yet.")
    else:
        col1, col2 = st.columns(2)
        filter_status = col1.multiselect("Filter by status", STATUSES, default=[s for s in STATUSES if s not in ['APPROVED', 'REJECTED']])
        filter_sales = col2.multiselect("Filter by salesperson", cases['salesperson'].dropna().unique().tolist())

        filtered = cases[cases['status'].isin(filter_status)] if filter_status else cases
        if filter_sales:
            filtered = filtered[filtered['salesperson'].isin(filter_sales)]

        case_options = {f"{r['case_ref']} — {r['client']} ({r['status']})": r['id'] for _, r in filtered.iterrows()}
        if case_options:
            selected_label = st.selectbox("Select a case to manage", list(case_options.keys()))
            case_id = case_options[selected_label]
            case = cases[cases['id'] == case_id].iloc[0]

            st.divider()

            col1, col2, col3 = st.columns(3)
            col1.write(f"**Client:** {case['client']}")
            col1.write(f"**Visa:** {case['visa_type']} → {case['destination']}")
            col1.write(f"**Salesperson:** {case['salesperson']}")
            col2.write(f"**Status:** {STATUS_COLORS.get(case['status'],'')} {case['status']}")
            col2.write(f"**Last Updated:** {case['updated_at']}")
            if case['fee_override_reason']:
                col2.warning(f"⚠️ Fee override: {case['fee_override_reason']}")

            fee, paid, balance = get_case_balance(case_id)
            col3.metric("Total Fee", f"£{fee:,.0f}")
            col3.metric("Paid", f"£{paid:,.0f}")
            if balance > 0:
                col3.metric("Balance Due", f"£{balance:,.0f}", delta=f"-£{balance:,.0f}", delta_color="inverse")
            else:
                col3.metric("Balance Due", "£0 ✅")

            if case['updated_at']:
                try:
                    last_update = datetime.strptime(case['updated_at'], "%Y-%m-%d %H:%M:%S")
                except:
                    last_update = datetime.strptime(case['updated_at'][:10], "%Y-%m-%d")
                days_since = (datetime.now() - last_update).days
                if days_since >= 2 and case['status'] not in ['PAYMENT_COMPLETE', 'SUBMITTED', 'APPROVED', 'REJECTED']:
                    st.error(f"🔴 **This case has been idle for {days_since} days!** Update the status, book an appointment, or add progress.")

            st.divider()

            st.subheader("📅 Appointment")
            current_apt = case['appointment_date'] if case['appointment_date'] else None

            if current_apt:
                apt_date = date.fromisoformat(current_apt)
                days_until = (apt_date - date.today()).days
                if days_until < 0:
                    st.write(f"Appointment was on **{current_apt}** ({abs(days_until)} days ago)")
                elif days_until == 0:
                    st.error(f"🔴 Appointment is **TODAY** — {current_apt}")
                elif days_until == 1:
                    st.error(f"🔴 Appointment is **TOMORROW** — {current_apt}")
                elif days_until == 2:
                    st.warning(f"🟠 Appointment in **2 days** — {current_apt}")
                elif days_until == 3:
                    st.warning(f"🟡 Appointment in **3 days** — {current_apt}")
                else:
                    st.info(f"Appointment: **{current_apt}** ({days_until} days away)")

            with st.form("appointment_form"):
                new_apt_date = st.date_input("Set / Update Appointment Date",
                                             value=date.fromisoformat(current_apt) if current_apt else date.today() + timedelta(days=7),
                                             min_value=date.today())
                if st.form_submit_button("Save Appointment"):
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    new_status = case['status']
                    if case['status'] in ('NEW', 'DEPOSIT_RECEIVED'):
                        new_status = 'APPOINTMENT_BOOKED'
                    run_query("UPDATE cases SET appointment_date=?, status=?, updated_at=? WHERE id=?",
                              (new_apt_date.isoformat(), new_status, now, case_id), fetch=False)
                    log_activity(case_id, "APPOINTMENT_SET", f"Set to {new_apt_date.isoformat()} by {current_user}", current_user)
                    st.success(f"✅ Appointment set for {new_apt_date}")
                    st.rerun()

            st.divider()

            st.subheader("📄 Document Handover")
            if case['docs_handed']:
                st.success(f"✅ **All documents handed** on {case['docs_handed_at']} by **{case['docs_handed_by_name']}**")
                if balance > 0:
                    st.warning(f"💰 **£{balance:.0f} still outstanding** — collect before embassy visit.")
                else:
                    st.success("✅ Fully paid — client is ready.")
            else:
                st.info("Documents have **not yet been handed** to the client.")
                if balance > 0:
                    st.warning(f"⚠️ Client still owes **£{balance:.0f}**.")
                with st.form("handover_form"):
                    handover_by = st.selectbox("Who is handing over?", list(staff_names.keys()))
                    if st.form_submit_button("✅ Confirm All Documents Handed", type="primary"):
                        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        hb_id = staff_names.get(handover_by)
                        new_status = "DOCS_HANDED" if balance > 0 else "PAYMENT_COMPLETE"
                        run_query("UPDATE cases SET docs_handed=1, docs_handed_at=?, docs_handed_by=?, status=?, updated_at=? WHERE id=?",
                                  (now, hb_id, new_status, now, case_id), fetch=False)
                        log_activity(case_id, "DOCS_HANDED", f"All documents handed by {handover_by}", current_user)
                        st.success(f"✅ Documents handed by {handover_by}")
                        st.rerun()

            st.divider()

            st.subheader("🔄 Update Status")
            current_idx = STATUSES.index(case['status']) if case['status'] in STATUSES else 0
            with st.form("status_form"):
                new_status = st.selectbox("Move to", STATUSES, index=current_idx)
                status_submitted = st.form_submit_button("Update Status", type="primary")

            if status_submitted:
                if new_status == "SUBMITTED" and balance > 0:
                    st.error(f"🚫 Cannot submit — £{balance:.0f} outstanding.")
                elif new_status != case['status']:
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    run_query("UPDATE cases SET status=?, updated_at=? WHERE id=?", (new_status, now, case_id), fetch=False)
                    log_activity(case_id, "STATUS_CHANGED", f"{case['status']} → {new_status}", current_user)
                    st.success(f"Status updated to {new_status}")
                    st.rerun()

            st.divider()

            st.subheader("🗑️ Delete Case")
            with st.expander("Delete this case permanently"):
                st.warning("This action cannot be undone.")
                with st.form("delete_form"):
                    delete_reason = st.text_area("Reason for deletion *", placeholder="e.g. Duplicate, client withdrew...")
                    confirm_text = st.text_input("Type the case reference to confirm", placeholder=case['case_ref'])
                    if st.form_submit_button("🗑️ Permanently Delete Case", type="primary"):
                        if not delete_reason or not delete_reason.strip():
                            st.error("❌ You must provide a reason.")
                        elif confirm_text.strip() != case['case_ref']:
                            st.error(f"❌ Type **{case['case_ref']}** exactly to confirm.")
                        else:
                            log_activity(case_id, "CASE_DELETED", f"{case['case_ref']} deleted by {current_user}. Reason: {delete_reason.strip()}", current_user)
                            run_query("DELETE FROM payments WHERE case_id=?", (case_id,), fetch=False)
                            run_query("DELETE FROM cases WHERE id=?", (case_id,), fetch=False)
                            st.success(f"✅ Case {case['case_ref']} deleted.")
                            st.rerun()

            st.divider()

            st.subheader("📋 Activity Log")
            logs = run_query("SELECT timestamp, action, details, performed_by FROM activity_log WHERE case_id=? ORDER BY timestamp DESC LIMIT 20", (case_id,))
            if not logs.empty:
                st.dataframe(logs, use_container_width=True, hide_index=True)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 👥 CLIENTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
elif page == "👥 Clients":
    st.title("👥 Clients")
    clients = run_query("""
        SELECT cl.*, COUNT(c.id) as total_cases, SUM(c.total_fee) as total_fees
        FROM clients cl LEFT JOIN cases c ON cl.id = c.client_id
        GROUP BY cl.id ORDER BY cl.created_at DESC
    """)
    if clients.empty:
        st.info("No clients yet.")
    else:
        search = st.text_input("🔍 Search clients", "")
        if search:
            clients = clients[clients['full_name'].str.contains(search, case=False, na=False) |
                              clients['phone'].str.contains(search, case=False, na=False) |
                              clients['passport_number'].str.contains(search, case=False, na=False)]
        st.dataframe(clients[['full_name', 'phone', 'email', 'passport_number', 'nationality', 'total_cases', 'total_fees']],
                     use_container_width=True, hide_index=True)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 💰 PAYMENTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
elif page == "💰 Payments":
    st.title("💰 Payments")
    tab1, tab2 = st.tabs(["Record Payment", "Payment History"])

    with tab1:
        p_cases = run_query("SELECT c.id, c.case_ref, cl.full_name FROM cases c LEFT JOIN clients cl ON c.client_id=cl.id WHERE c.status NOT IN ('APPROVED','REJECTED') ORDER BY c.created_at DESC")
        if p_cases.empty:
            st.info("No active cases.")
        else:
            case_opts = {f"{r['case_ref']} — {r['full_name']}": r['id'] for _, r in p_cases.iterrows()}
            selected = st.selectbox("Case", list(case_opts.keys()))
            case_id = case_opts[selected]
            fee, paid, balance = get_case_balance(case_id)
            st.info(f"Fee: £{fee:,.0f} | Paid: £{paid:,.0f} | **Balance: £{balance:,.0f}**")

            with st.form("payment_form"):
                col1, col2 = st.columns(2)
                amount = col1.number_input("Amount (£)", min_value=0.01, step=50.0, value=balance if balance > 0 else 50.0)
                method = col2.selectbox("Method", PAYMENT_METHODS)
                pay_date = col1.date_input("Date", value=date.today())
                pay_notes = st.text_input("Notes")
                pay_proof = st.file_uploader("Upload payment proof (screenshot)", type=["png", "jpg", "jpeg"], key="pay_proof")
                if st.form_submit_button("Record Payment", type="primary"):
                    proof_b64 = None
                    if pay_proof:
                        proof_b64 = base64.b64encode(pay_proof.read()).decode('utf-8')
                    run_query("INSERT INTO payments (case_id, amount, method, received_by, payment_date, proof_image, notes) VALUES (?,?,?,?,?,?,?)",
                              (case_id, amount, method, staff_names.get(current_user), pay_date.isoformat(), proof_b64, pay_notes), fetch=False)
                    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    new_bal = balance - amount
                    if new_bal <= 0:
                        run_query("UPDATE cases SET status='PAYMENT_COMPLETE', updated_at=? WHERE id=? AND status NOT IN ('SUBMITTED','APPROVED','REJECTED')",
                                  (now, case_id), fetch=False)
                    elif paid == 0:
                        run_query("UPDATE cases SET status='DEPOSIT_RECEIVED', updated_at=? WHERE id=? AND status='NEW'",
                                  (now, case_id), fetch=False)
                    else:
                        run_query("UPDATE cases SET updated_at=? WHERE id=?", (now, case_id), fetch=False)
                    log_activity(case_id, "PAYMENT_RECEIVED", f"£{amount} via {method} by {current_user}", current_user)
                    st.success(f"✅ £{amount:,.0f} recorded!")
                    st.rerun()

    with tab2:
        try:
            payments = run_query("""
                SELECT p.id, p.payment_date, c.case_ref, cl.full_name as client,
                       p.amount, p.method, s.name as received_by, p.notes, p.proof_image
                FROM payments p
                LEFT JOIN cases c ON p.case_id = c.id
                LEFT JOIN clients cl ON c.client_id = cl.id
                LEFT JOIN staff s ON p.received_by = s.id
                ORDER BY p.created_at DESC
            """)
            has_proof_col = True
        except:
            payments = run_query("""
                SELECT p.id, p.payment_date, c.case_ref, cl.full_name as client,
                       p.amount, p.method, s.name as received_by, p.notes
                FROM payments p
                LEFT JOIN cases c ON p.case_id = c.id
                LEFT JOIN clients cl ON c.client_id = cl.id
                LEFT JOIN staff s ON p.received_by = s.id
                ORDER BY p.created_at DESC
            """)
            has_proof_col = False

        if not payments.empty:
            st.metric("Total Collected", f"£{payments['amount'].sum():,.0f}")
            st.dataframe(payments[['payment_date', 'case_ref', 'client', 'amount', 'method', 'received_by', 'notes']],
                         use_container_width=True, hide_index=True)

            if has_proof_col and 'proof_image' in payments.columns:
                proofs = payments[payments['proof_image'].notna() & (payments['proof_image'] != '')]
                if not proofs.empty:
                    st.subheader("📎 Payment Proofs")
                    for _, p in proofs.iterrows():
                        with st.expander(f"{p['case_ref']} — {p['client']} — £{p['amount']:.0f} on {p['payment_date']}"):
                            try:
                                img_bytes = base64.b64decode(p['proof_image'])
                                st.image(img_bytes, caption=f"Proof for £{p['amount']:.0f}", use_container_width=True)
                            except:
                                st.warning("Could not display proof image.")
        else:
            st.info("No payments recorded yet.")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ⚙️ SETTINGS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
elif page == "⚙️ Settings":
    st.title("⚙️ Settings")
    st.subheader("Staff Management")
    staff = run_query("SELECT * FROM staff ORDER BY name")

    if not staff.empty:
        for _, person in staff.iterrows():
            col1, col2, col3, col4 = st.columns([3, 2, 1, 1])
            col1.write(f"**{person['name']}**")
            col2.write(person['role'])
            col3.write("✅" if person['active'] else "❌")
            if col4.button("🗑️", key=f"del_{person['id']}"):
                linked = run_query("SELECT COUNT(*) as cnt FROM cases WHERE salesperson_id=?", (person['id'],))['cnt'].iloc[0]
                if linked > 0:
                    run_query("UPDATE staff SET active=0 WHERE id=?", (person['id'],), fetch=False)
                    st.warning(f"{person['name']} deactivated ({linked} linked cases).")
                else:
                    run_query("DELETE FROM staff WHERE id=?", (person['id'],), fetch=False)
                    st.success(f"Removed {person['name']}")
                st.rerun()
    else:
        st.info("No staff added yet.")

    with st.form("add_staff"):
        st.write("**Add Staff Member**")
        col1, col2 = st.columns(2)
        s_name = col1.text_input("Name")
        s_role = col2.selectbox("Role", ["Salesperson", "Case Manager", "Finance", "Admin"])
        if st.form_submit_button("Add Staff"):
            if s_name:
                run_query("INSERT INTO staff (name, role) VALUES (?,?)", (s_name, s_role), fetch=False)
                st.success(f"Added {s_name}")
                st.rerun()

    st.divider()
    st.subheader("Export Data")
    col1, col2 = st.columns(2)
    if col1.button("📥 Export Cases"):
        df = run_query("SELECT c.*, cl.full_name as client, s.name as salesperson FROM cases c LEFT JOIN clients cl ON c.client_id=cl.id LEFT JOIN staff s ON c.salesperson_id=s.id")
        st.download_button("Download", df.to_csv(index=False), "cases_export.csv", "text/csv")
    if col2.button("📥 Export Payments"):
        df = run_query("SELECT p.*, c.case_ref FROM payments p LEFT JOIN cases c ON p.case_id=c.id")
        st.download_button("Download", df.to_csv(index=False), "payments_export.csv", "text/csv")
