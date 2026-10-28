from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import statistics
import sqlite3
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet, InvalidToken
from flask import Flask, jsonify, make_response, redirect, render_template, request, url_for

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("FINANCE_DATA_DIR", BASE_DIR / "runtime")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "finance.db"
SESSION_KEY_PATH = DATA_DIR / ".session-key"
FERNET_KEY_PATH = DATA_DIR / ".encryption-key"
SESSION_COOKIE = "controle_session"
CSRF_COOKIE = "controle_csrf"
SESSION_DAYS = 14
CARD_PAYMENT_CATEGORY = "Pagamento de cartão"
CARD_REFUND_CATEGORY = "Estorno no cartão"
CATEGORIES = [
    "Alimentação", "Moradia", "Transporte", "Saúde", "Educação", "Assinaturas",
    "Compras", "Lazer", "Taxas e juros", "Salário", "Outras entradas",
    "Investimentos", CARD_PAYMENT_CATEGORY, "Transferência interna", "Outros", CARD_REFUND_CATEGORY,
]

app = Flask(__name__, template_folder="templates", static_folder="static")


class ExternalServiceError(Exception):
    pass


def _persistent_key(path: Path) -> bytes:
    env_name = "APP_SESSION_KEY" if path == SESSION_KEY_PATH else "APP_ENCRYPTION_KEY"
    supplied = os.environ.get(env_name)
    if supplied:
        return supplied.encode("utf-8")
    if path.exists():
        return path.read_bytes().strip()
    value = secrets.token_bytes(32) if path == SESSION_KEY_PATH else Fernet.generate_key()
    path.write_bytes(value)
    if os.name != "nt":
        path.chmod(0o600)
    return value


SESSION_KEY = _persistent_key(SESSION_KEY_PATH)
FERNET = Fernet(_persistent_key(FERNET_KEY_PATH))


def db_connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    return db


@contextmanager
def database():
    db = db_connect()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    with database() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS owner (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS secrets (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS items (
            item_id TEXT PRIMARY KEY,
            institution TEXT NOT NULL DEFAULT 'Instituição financeira',
            status TEXT NOT NULL DEFAULT 'CONNECTED',
            last_sync TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS accounts (
            account_id TEXT PRIMARY KEY,
            item_id TEXT NOT NULL,
            name TEXT NOT NULL,
            marketing_name TEXT NOT NULL DEFAULT '',
            number TEXT NOT NULL DEFAULT '',
            type TEXT NOT NULL DEFAULT 'BANK',
            subtype TEXT NOT NULL DEFAULT '',
            balance REAL NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'BRL',
            available_limit REAL,
            credit_limit REAL,
            due_date TEXT,
            institution TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            FOREIGN KEY(item_id) REFERENCES items(item_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS transactions (
            transaction_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            date TEXT NOT NULL,
            description TEXT NOT NULL,
            amount REAL NOT NULL,
            direction TEXT NOT NULL,
            flow TEXT NOT NULL,
            category TEXT NOT NULL,
            category_source TEXT NOT NULL DEFAULT 'auto',
            institution TEXT NOT NULL DEFAULT '',
            account_name TEXT NOT NULL DEFAULT '',
            account_type TEXT NOT NULL DEFAULT 'BANK',
            card_number TEXT NOT NULL DEFAULT '',
            bill_forecast_date TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions(date);
        CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id);
        CREATE TABLE IF NOT EXISTS bills (
            bill_id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL,
            due_date TEXT NOT NULL DEFAULT '',
            closing_date TEXT NOT NULL DEFAULT '',
            total_amount REAL NOT NULL DEFAULT 0,
            minimum_payment REAL NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'BRL',
            updated_at TEXT NOT NULL,
            FOREIGN KEY(account_id) REFERENCES accounts(account_id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS investments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            institution TEXT NOT NULL DEFAULT '',
            amount REAL NOT NULL,
            updated_on TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            period_key TEXT NOT NULL,
            created_at TEXT NOT NULL,
            message TEXT NOT NULL,
            analysis TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'sent',
            UNIQUE(kind, period_key)
        );
        CREATE TABLE IF NOT EXISTS forecast_overrides (
            item_key TEXT PRIMARY KEY,
            label_override TEXT NOT NULL DEFAULT '',
            amount_override REAL,
            type_override TEXT,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            is_custom INTEGER NOT NULL DEFAULT 0 CHECK(is_custom IN (0,1)),
            card_account_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """)
        account_columns = {row[1] for row in db.execute("PRAGMA table_info(accounts)").fetchall()}
        for column, declaration in (("credit_level", "TEXT NOT NULL DEFAULT ''"),
                                    ("credit_brand", "TEXT NOT NULL DEFAULT ''"),
                                    ("credit_status", "TEXT NOT NULL DEFAULT ''")):
            if column not in account_columns:
                db.execute(f"ALTER TABLE accounts ADD COLUMN {column} {declaration}")
        investment_columns = {row[1] for row in db.execute("PRAGMA table_info(investments)").fetchall()}
        for column, declaration in (("source", "TEXT NOT NULL DEFAULT 'manual'"),
                                    ("external_id", "TEXT"), ("item_id", "TEXT"),
                                    ("status", "TEXT NOT NULL DEFAULT ''"),
                                    ("details", "TEXT NOT NULL DEFAULT '{}'")):
            if column not in investment_columns:
                db.execute(f"ALTER TABLE investments ADD COLUMN {column} {declaration}")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_investments_external_id "
                   "ON investments(external_id) WHERE external_id IS NOT NULL")
        migration_key = "migration_card_cashflow_v1"
        if not db.execute("SELECT 1 FROM settings WHERE key=?", (migration_key,)).fetchone():
            transactions = db.execute(
                "SELECT transaction_id,description,category,direction,account_type,category_source,flow FROM transactions"
            ).fetchall()
            payment_key = normalize(CARD_PAYMENT_CATEGORY)
            refund_key = normalize(CARD_REFUND_CATEGORY)
            for tx in transactions:
                manual = tx["category_source"] == "manual"
                category_key = normalize(tx["category"])
                if manual and category_key not in (payment_key, refund_key):
                    continue
                if manual:
                    category = tx["category"]
                    flow = category_flow(category, tx["direction"], tx["account_type"])
                else:
                    category, flow = classify_transaction(
                        tx["description"], tx["category"], tx["direction"], tx["account_type"]
                    )
                    if flow not in ("card_payment", "card_refund") and not (
                            category == CARD_PAYMENT_CATEGORY and
                            _is_card_payment(tx["description"], tx["category"])):
                        continue
                if category != tx["category"] or flow != tx["flow"]:
                    db.execute("UPDATE transactions SET category=?,flow=? WHERE transaction_id=?",
                               (category, flow, tx["transaction_id"]))
            db.execute("INSERT INTO settings(key,value) VALUES(?,?)", (migration_key, iso_now()))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return now_utc().isoformat(timespec="seconds")


def local_now() -> datetime:
    zone_name = get_setting("timezone", "America/Sao_Paulo")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone(timedelta(hours=-3))
    return now_utc().astimezone(zone)


def get_setting(key: str, default: str = "") -> str:
    with database() as db:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: object) -> None:
    with database() as db:
        db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, str(value)))


def get_secret(key: str) -> str:
    with database() as db:
        row = db.execute("SELECT value FROM secrets WHERE key=?", (key,)).fetchone()
    if not row:
        return ""
    try:
        return FERNET.decrypt(row["value"].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError):
        return ""


def set_secret(key: str, value: str) -> None:
    if not value:
        return
    encrypted = FERNET.encrypt(value.encode("utf-8")).decode("ascii")
    with database() as db:
        db.execute("INSERT INTO secrets(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, encrypted))


def delete_secret(key: str) -> None:
    with database() as db:
        db.execute("DELETE FROM secrets WHERE key=?", (key,))


def owner_exists() -> bool:
    with database() as db:
        return db.execute("SELECT 1 FROM owner WHERE id=1").fetchone() is not None


def csrf_value() -> str:
    value = request.cookies.get(CSRF_COOKIE, "")
    if len(value) < 32:
        value = secrets.token_urlsafe(32)
    request._csrf_to_set = value
    return value


def csrf_valid() -> bool:
    supplied = request.headers.get("X-CSRF-Token", "") or request.form.get("csrf_token", "")
    cookie = request.cookies.get(CSRF_COOKIE, "")
    return bool(supplied and cookie and hmac.compare_digest(supplied, cookie))


def signed_session_token() -> str:
    raw = request.cookies.get(SESSION_COOKIE, "")
    try:
        payload, signature = raw.rsplit(".", 1)
        expected = hmac.new(SESSION_KEY, payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return ""
        issued, nonce = payload.split(":", 1)
        if time.time() - int(issued) > SESSION_DAYS * 86400:
            return ""
        return nonce
    except (ValueError, TypeError):
        return ""


def authenticated() -> bool:
    return bool(signed_session_token()) and owner_exists()


def make_session_cookie() -> str:
    payload = f"{int(time.time())}:{secrets.token_urlsafe(32)}"
    signature = hmac.new(SESSION_KEY, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not authenticated():
            if request.path.startswith("/api/"):
                return jsonify(error="Sua sessão expirou. Entre novamente."), 401
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapped


def json_body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def api_csrf_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not csrf_valid():
            return jsonify(error="Sessão de formulário inválida. Atualize a página e tente de novo."), 400
        return fn(*args, **kwargs)
    return wrapped


def normalize(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", text).strip().lower()


def category_flow(category: str, direction: str, account_type: str = "BANK") -> str:
    category_key = normalize(category)
    direction = str(direction or "").upper()
    account_type = str(account_type or "BANK").upper()
    if category_key == normalize(CARD_PAYMENT_CATEGORY):
        if account_type == "CREDIT":
            return "transfer"
        return "card_payment" if direction == "DEBIT" else "income"
    if category_key == normalize(CARD_REFUND_CATEGORY):
        if account_type == "CREDIT":
            return "card_refund"
        return "income" if direction == "CREDIT" else "expense"
    if category_key == normalize("Transferência interna"):
        return "transfer"
    if category_key == normalize("Investimentos"):
        return "investment" if direction == "DEBIT" else "income"
    if category_key in (normalize("Salário"), normalize("Outras entradas")):
        return "income" if direction == "CREDIT" else "expense"
    return "expense" if direction == "DEBIT" else "income"


def _is_card_payment(description: str, source_category: str, operation_type: str = "") -> bool:
    operation = str(operation_type or "").replace("_", " ").replace("-", " ")
    text = normalize(f"{description} {source_category} {operation}".replace("_", " ").replace("-", " "))
    if re.search(r"\b(?:pagamento|pagto|pgto|pgt|pagt)\b.{0,30}\b(?:fatura|fat|cartao)\b", text):
        return True
    phrases = (
        "pagamento de fatura", "pagamento da fatura", "pagamento fatura",
        "pagamento de cartao", "pagamento do cartao", "pagamento cartao",
        "pagto de fatura", "pagto da fatura", "pagto fatura", "pagto de cartao", "pagto cartao",
        "pgto de fatura", "pgto da fatura", "pgto fatura", "pgto de cartao", "pgto cartao",
        "pgt fatura", "pgt cartao", "pagt fatura", "pagt cartao",
        "fatura cartao", "fatura do cartao", "fatura de cartao",
        "credit card payment", "payment of card", "card payment", "card bill",
    )
    return any(phrase in text for phrase in phrases)


def _is_credit_account(tx: dict) -> bool:
    return str(tx.get("account_type") or "BANK").strip().upper() == "CREDIT"


def _confirmed_transactions(txs: list[dict]) -> list[dict]:
    return [tx for tx in txs if str(tx.get("status") or "").strip().upper() not in ("PENDING", "PENDENTE")]


def _eligible_transactions(txs: list[dict], through: date | None = None) -> list[dict]:
    confirmed = _confirmed_transactions(txs)
    if through:
        cutoff = through.isoformat()
        eligible = []
        for tx in confirmed:
            try:
                if date.fromisoformat(str(tx.get("date") or "")[:10]) <= through:
                    eligible.append(tx)
            except ValueError:
                continue
        confirmed = eligible
    return confirmed


def transaction_totals(txs: list[dict], through: date | None = None) -> dict:
    confirmed = _eligible_transactions(txs, through)
    income = sum(tx["amount"] for tx in confirmed if tx["flow"] == "income" and not _is_credit_account(tx))
    cash_expenses = sum(tx["amount"] for tx in confirmed
                        if tx["flow"] == "expense" and not _is_credit_account(tx))
    card_payments = sum(tx["amount"] for tx in confirmed
                        if tx["flow"] == "card_payment" and not _is_credit_account(tx))
    credit_purchases = sum(tx["amount"] for tx in confirmed
                           if tx["flow"] == "expense" and _is_credit_account(tx))
    card_refunds = sum(tx["amount"] for tx in confirmed if tx["flow"] == "card_refund")
    consumption_expenses = sum(tx["amount"] for tx in confirmed if tx["flow"] == "expense") - card_refunds
    investment_outflow = sum(tx["amount"] for tx in confirmed if tx["flow"] == "investment")
    cash_outflow = cash_expenses + card_payments
    return {"income": round(income, 2), "cash_expenses": round(cash_expenses, 2),
            "card_payments": round(card_payments, 2), "cash_outflow": round(cash_outflow, 2),
            "expenses": round(cash_outflow, 2), "consumption_expenses": round(consumption_expenses, 2),
            "credit_purchases": round(credit_purchases, 2), "card_refunds": round(card_refunds, 2),
            "invested_outflow": round(investment_outflow, 2),
            "net": round(income - cash_outflow - investment_outflow, 2)}


def category_spending_totals(txs: list[dict], through: date | None = None) -> dict[str, float]:
    totals: dict[str, float] = {}
    for tx in _eligible_transactions(txs, through):
        if tx["flow"] == "expense":
            totals[tx["category"]] = totals.get(tx["category"], 0.0) + tx["amount"]
        elif tx["flow"] == "card_refund":
            category = CARD_REFUND_CATEGORY
            totals[category] = totals.get(category, 0.0) - tx["amount"]
    return {name: round(amount, 2) for name, amount in totals.items() if amount}


def classify_transaction(description: str, source_category: str, direction: str,
                          account_type: str, operation_type: str = "") -> tuple[str, str]:
    direction = str(direction or "").upper()
    account_type = str(account_type or "BANK").upper()
    text = normalize(f"{description} {operation_type}".replace("_", " ").replace("-", " "))
    source = normalize(source_category)
    if _is_card_payment(description, source_category, operation_type):
        if account_type == "CREDIT":
            return CARD_PAYMENT_CATEGORY, "transfer"
        return ("Outras entradas", "income") if direction == "CREDIT" else (CARD_PAYMENT_CATEGORY, "card_payment")
    if any(x in text for x in ("transferencia entre contas", "transferencia propria", "transferencia interna", "entre minhas contas")):
        return "Transferência interna", "transfer"
    if account_type == "CREDIT" and direction == "CREDIT":
        return CARD_REFUND_CATEGORY, "card_refund"
    if direction == "CREDIT":
        if any(x in text for x in ("salario", "folha de pagamento", "payroll", "pro labore")):
            return "Salário", "income"
        return "Outras entradas", "income"
    if any(x in text or x in source for x in ("investimento", "tesouro direto", "cdb", "rdb", "aplicacao financeira", "fundo de investimento", "brokerage")):
        return "Investimentos", "investment"
    rules = [
        (("supermercado", "mercado", "hortifruti", "padaria", "restaurante", "ifood", "delivery", "acougue", "alimentacao", "food"), "Alimentação"),
        (("aluguel", "condominio", "energia", "luz", "agua", "gas", "internet", "moradia", "imobiliaria"), "Moradia"),
        (("uber", "99app", "combustivel", "posto", "estacionamento", "transporte", "metro", "onibus", "pedagio"), "Transporte"),
        (("farmacia", "hospital", "clinica", "medico", "saude", "laboratorio", "dentista"), "Saúde"),
        (("escola", "universidade", "curso", "educacao", "livraria"), "Educação"),
        (("netflix", "spotify", "prime video", "disney", "assinatura", "mensalidade", "icloud", "youtube premium"), "Assinaturas"),
        (("shopping", "loja", "amazon", "mercado livre", "magazine", "roupa", "vestuario", "varejo"), "Compras"),
        (("cinema", "show", "viagem", "hotel", "lazer", "parque", "entretenimento"), "Lazer"),
        (("tarifa", "juros", "iof", "multa", "anuidade"), "Taxas e juros"),
    ]
    for keys, category in rules:
        if any(key in text or key in source for key in keys):
            return category, "expense"
    return "Outros", "expense"


def _request_json(url: str, method: str = "GET", headers: dict | None = None,
                  payload: dict | None = None, timeout: int = 35) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    all_headers = {"Accept": "application/json", "Content-Type": "application/json"}
    all_headers.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        raise ExternalServiceError(f"Serviço externo respondeu com HTTP {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        if isinstance(exc, json.JSONDecodeError):
            raise ExternalServiceError("O serviço externo respondeu em formato inesperado.") from None
        raise ExternalServiceError("Não foi possível alcançar o serviço externo.") from None


_pluggy_cache = {"key": "", "expires": 0.0}
_pluggy_lock = threading.Lock()
_login_attempts: dict[str, list[float]] = {}
_login_attempts_lock = threading.Lock()
_report_jobs: dict[str, dict[str, str]] = {}
_report_jobs_lock = threading.Lock()
_active_weekly_job: str | None = None


def login_limited(address: str, failed: bool = False) -> bool:
    current = time.time()
    with _login_attempts_lock:
        attempts = [stamp for stamp in _login_attempts.get(address, []) if current - stamp < 900]
        _login_attempts[address] = attempts
        if failed:
            attempts.append(current)
        return len(attempts) >= 8


def pluggy_api_key() -> str:
    with _pluggy_lock:
        if _pluggy_cache["key"] and _pluggy_cache["expires"] > time.time() + 30:
            return _pluggy_cache["key"]
        client_id = get_secret("pluggy_client_id")
        client_secret = get_secret("pluggy_client_secret")
        if not client_id or not client_secret:
            raise ExternalServiceError("Preencha o Client ID e o Client Secret da Pluggy em Configurações.")
        auth = _request_json("https://api.pluggy.ai/auth", "POST", payload={
            "clientId": client_id, "clientSecret": client_secret,
        })
        key = auth.get("apiKey") or auth.get("api_key")
        if not key:
            raise ExternalServiceError("A Pluggy não retornou uma chave de API.")
        _pluggy_cache.update(key=key, expires=time.time() + 6900)
        return key


def pluggy_request(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    return _request_json("https://api.pluggy.ai" + path, method, {"X-API-KEY": pluggy_api_key()}, payload)


def _amount(value: object) -> float:
    try:
        return round(abs(float(value or 0)), 2)
    except (TypeError, ValueError):
        return 0.0


_BANK_CODE_NAMES = {
    "001": "Banco do Brasil", "033": "Santander", "077": "Inter",
    "104": "Caixa", "237": "Bradesco", "260": "Nubank", "341": "Itaú",
}


def _bank_name_from_account(account: dict) -> str:
    bank_data = account.get("bankData") or {}
    transfer_number = str(bank_data.get("transferNumber") or "")
    match = re.match(r"\s*(\d{3})[/-]", transfer_number)
    if match and match.group(1) in _BANK_CODE_NAMES:
        return _BANK_CODE_NAMES[match.group(1)]
    account_name = unicodedata.normalize("NFKD", str(account.get("marketingName") or account.get("name") or ""))
    account_name = "".join(ch for ch in account_name if not unicodedata.combining(ch)).lower()
    if "nu pagamentos" in account_name or "nubank" in account_name:
        return "Nubank"
    if "banco inter" in account_name or account_name == "inter":
        return "Inter"
    return ""


def _item_institution_name(accounts: list[dict], connector_name: str) -> str:
    generic = unicodedata.normalize("NFKD", connector_name).encode("ascii", "ignore").decode().lower()
    if generic not in {"meupluggy", "pluggy", "open finance", "instituicao financeira"}:
        return connector_name
    for account in accounts:
        bank_name = _bank_name_from_account(account)
        if bank_name:
            return bank_name
    return connector_name


def _card_product(account: dict, credit_data: dict) -> str:
    level = str(credit_data.get("level") or "").strip()
    if level:
        return level.replace("_", " ").title()
    name = str(account.get("marketingName") or account.get("name") or "").strip()
    if name and not re.fullmatch(r"[A-ZÀ-Ý][A-ZÀ-Ý .'-]{4,}", name):
        return name.title()
    return "Cartão de crédito"


def _card_account_label(institution: str, account: dict, credit_data: dict) -> str:
    number = re.sub(r"\D", "", str(account.get("number") or ""))
    last_four = number[-4:] if number else ""
    label = f"{institution} · {_card_product(account, credit_data)}"
    return label + (f" · final {last_four}" if last_four else "")


_INVESTMENT_TYPE_NAMES = {
    "FIXED_INCOME": "Renda fixa", "EQUITY": "Renda variável", "ETF": "ETF",
    "MUTUAL_FUND": "Fundos", "COE": "COE", "SECURITY": "Previdência", "OTHER": "Outros",
}
_INVESTMENT_SUBTYPE_NAMES = {
    "TREASURY": "Tesouro Direto", "CDB": "CDB", "LCI": "LCI", "LCA": "LCA", "LF": "Letra financeira",
    "CRI": "CRI", "CRA": "CRA", "DEBENTURES": "Debêntures", "STOCK": "Ações",
    "REAL_ESTATE_FUND": "FII", "BDR": "BDR", "MULTIMARKET_FUND": "Multimercado",
    "ETF_FUND": "Fundo de índice", "FIXED_INCOME_FUND": "Fundo de renda fixa",
    "STOCK_FUND": "Fundo de ações", "OFFSHORE_FUND": "Fundo offshore", "FIP_FUND": "FIP",
    "EXCHANGE_FUND": "Fundo cambial", "FI_INFRA": "FI-Infra", "FI_AGRO": "FI-Agro",
    "OPTION": "Opções", "DERIVATIVES": "Derivativos", "RETIREMENT": "Previdência",
}


def _investment_kind(investment: dict) -> str:
    investment_type = str(investment.get("type") or "OTHER").upper()
    subtype = str(investment.get("subtype") or "").upper()
    type_name = _INVESTMENT_TYPE_NAMES.get(investment_type, investment_type.replace("_", " ").title())
    subtype_name = _INVESTMENT_SUBTYPE_NAMES.get(subtype, subtype.replace("_", " ").title())
    return f"{type_name} · {subtype_name}" if subtype_name and subtype_name.lower() != type_name.lower() else type_name


def sync_item_investments(item_id: str, institution: str = "") -> int:
    rows: list[dict] = []
    page = 1
    while page <= 100:
        query = urllib.parse.urlencode({"itemId": item_id, "page": page, "pageSize": 100})
        response = pluggy_request("/investments?" + query)
        rows.extend(response.get("results", []))
        if page >= int(response.get("totalPages", 1) or 1):
            break
        page += 1

    current_ids = []
    updated = iso_now()
    with database() as db:
        item_row = db.execute("SELECT institution FROM items WHERE item_id=?", (item_id,)).fetchone()
        fallback_institution = institution or (item_row["institution"] if item_row else "")
        for investment in rows:
            external_id = str(investment.get("id") or "").strip()
            if not external_id or str(investment.get("status") or "").upper() == "TOTAL_WITHDRAWAL":
                continue
            current_ids.append(external_id)
            detail_fields = ("code", "currencyCode", "amountOriginal", "amountProfit", "amountWithdrawal",
                             "annualRate", "lastMonthRate", "lastTwelveMonthsRate", "dueDate", "issueDate",
                             "purchaseDate", "rate", "rateType", "fixedAnnualRate", "taxExempt", "issuerCNPJ")
            details = {key: investment.get(key) for key in detail_fields if investment.get(key) is not None}
            issuer = investment.get("issuer")
            if issuer is not None:
                details["issuer"] = issuer
            name = str(investment.get("name") or "Investimento Pluggy")[:120]
            holder = investment.get("institution") or {}
            if isinstance(holder, dict):
                provider = str(holder.get("name") or fallback_institution)
            else:
                provider = str(holder or fallback_institution)
            date_value = str(investment.get("updatedAt") or investment.get("date") or updated)[:10]
            try:
                date.fromisoformat(date_value)
            except ValueError:
                date_value = local_now().date().isoformat()
            amount_value = investment.get("balance")
            if amount_value is None:
                amount_value = investment.get("value") if investment.get("value") is not None else investment.get("amount")
            values = (name, _investment_kind(investment), provider[:120], _amount(amount_value), date_value,
                      "", "pluggy", external_id, item_id, str(investment.get("status") or ""),
                      json.dumps(details, ensure_ascii=False, separators=(",", ":")), updated)
            existing = db.execute("SELECT id FROM investments WHERE external_id=?", (external_id,)).fetchone()
            if existing:
                db.execute("""UPDATE investments SET name=?,kind=?,institution=?,amount=?,updated_on=?,notes=?,source=?,
                            external_id=?,item_id=?,status=?,details=?,created_at=? WHERE id=?""", (*values, existing["id"]))
            else:
                db.execute("""INSERT INTO investments(name,kind,institution,amount,updated_on,notes,source,external_id,
                            item_id,status,details,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", values)
        if current_ids:
            placeholders = ",".join("?" for _ in current_ids)
            db.execute(f"DELETE FROM investments WHERE source='pluggy' AND item_id=? AND external_id NOT IN ({placeholders})",
                       (item_id, *current_ids))
        else:
            db.execute("DELETE FROM investments WHERE source='pluggy' AND item_id=?", (item_id,))
    return len(current_ids)


def sync_item(item_id: str) -> int:
    item = pluggy_request(f"/items/{urllib.parse.quote(item_id, safe='')}")
    connector = item.get("connector") or {}
    connector_name = connector.get("name") or item.get("institution") or item.get("name") or "Institui\u00e7\u00e3o financeira"
    item_status = item.get("status") or item.get("executionStatus") or "CONNECTED"
    account_data = pluggy_request("/accounts?" + urllib.parse.urlencode({"itemId": item_id}))
    accounts = account_data.get("results", [])
    institution = _item_institution_name(accounts, str(connector_name))
    with database() as db:
        db.execute("INSERT INTO items(item_id,institution,status,created_at) VALUES(?,?,?,?) "
                   "ON CONFLICT(item_id) DO UPDATE SET institution=excluded.institution,status=excluded.status",
                   (item_id, institution, item_status, iso_now()))

    updated = iso_now()
    with database() as db:
        for account in accounts:
            credit_data = account.get("creditData") or {}
            account_institution = _bank_name_from_account(account) or institution
            account_name = _card_product(account, credit_data) if account.get("type") == "CREDIT" else (account.get("marketingName") or account.get("name") or "Conta")
            db.execute("""INSERT INTO accounts(account_id,item_id,name,marketing_name,number,type,subtype,balance,currency,
                        available_limit,credit_limit,due_date,institution,updated_at,credit_level,credit_brand,credit_status)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(account_id) DO UPDATE SET item_id=excluded.item_id,name=excluded.name,
                        marketing_name=excluded.marketing_name,number=excluded.number,type=excluded.type,
                        subtype=excluded.subtype,balance=excluded.balance,currency=excluded.currency,
                        available_limit=excluded.available_limit,credit_limit=excluded.credit_limit,
                        due_date=excluded.due_date,institution=excluded.institution,updated_at=excluded.updated_at,
                        credit_level=excluded.credit_level,credit_brand=excluded.credit_brand,credit_status=excluded.credit_status""",
                       (account.get("id"), item_id, account.get("name") or "Conta", account.get("marketingName") or "",
                        account.get("number") or "", account.get("type") or "BANK", account.get("subtype") or "",
                        _amount(account.get("balance")), account.get("currencyCode") or "BRL",
                        credit_data.get("availableCreditLimit"), credit_data.get("creditLimit"),
                        credit_data.get("balanceDueDate") or "", account_institution, updated,
                        credit_data.get("level") or "", credit_data.get("brand") or "", credit_data.get("status") or ""))

    count = 0
    for account in accounts:
        account_id = account.get("id")
        if not account_id:
            continue
        account_type = account.get("type") or "BANK"
        credit_data = account.get("creditData") or {}
        account_institution = _bank_name_from_account(account) or institution
        account_name = (_card_account_label(account_institution, account, credit_data) if account_type == "CREDIT"
                        else account.get("marketingName") or account.get("name") or "Conta")
        next_query = "?" + urllib.parse.urlencode({"accountId": account_id})
        page_count = 0
        while next_query and page_count < 100:
            data = pluggy_request("/v2/transactions" + next_query)
            transactions = data.get("results", [])
            with database() as db:
                for tx in transactions:
                    direction = (tx.get("type") or ("CREDIT" if float(tx.get("amount", 0) or 0) >= 0 else "DEBIT")).upper()
                    direction = "CREDIT" if direction == "CREDIT" else "DEBIT"
                    description = tx.get("description") or tx.get("descriptionRaw") or "Movimentação"
                    merchant = tx.get("merchant") or {}
                    if isinstance(merchant, dict) and merchant.get("name"):
                        description = merchant["name"] + " · " + description
                    metadata = tx.get("creditCardMetadata") or {}
                    operation_type = tx.get("operationType") or tx.get("operationTypeAdditionalInfo") or ""
                    category, flow = classify_transaction(description, tx.get("category") or "", direction,
                                                         account_type, operation_type)
                    tx_date = (tx.get("date") or tx.get("createdAt") or updated)[:32]
                    tx_id = tx.get("id")
                    if not tx_id:
                        continue
                    db.execute("""INSERT INTO transactions(transaction_id,account_id,date,description,amount,direction,flow,
                                category,institution,account_name,account_type,card_number,bill_forecast_date,status,updated_at)
                                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                                ON CONFLICT(transaction_id) DO UPDATE SET account_id=excluded.account_id,date=excluded.date,
                                description=excluded.description,amount=excluded.amount,direction=excluded.direction,
                                flow=CASE WHEN transactions.category_source='manual' THEN transactions.flow ELSE excluded.flow END,
                                category=CASE WHEN transactions.category_source='manual' THEN transactions.category ELSE excluded.category END,
                                institution=excluded.institution,account_name=excluded.account_name,
                                account_type=excluded.account_type,card_number=excluded.card_number,
                                bill_forecast_date=excluded.bill_forecast_date,status=excluded.status,updated_at=excluded.updated_at""",
                               (tx_id, account_id, tx_date, description, _amount(tx.get("amount")), direction, flow,
                                category, account_institution, account_name, account_type, metadata.get("cardNumber") or "",
                                tx.get("billForecastDate") or "", tx.get("status") or "", updated))
                    count += 1
            next_value = data.get("next")
            next_query = next_value if next_value and str(next_value).startswith("?") else ("?" + str(next_value) if next_value else "")
            page_count += 1

        if account_type == "CREDIT":
            page = 1
            while page <= 20:
                bills_data = pluggy_request("/bills?" + urllib.parse.urlencode({"accountId": account_id, "page": page, "pageSize": 100}))
                with database() as db:
                    for bill in bills_data.get("results", []):
                        db.execute("""INSERT INTO bills(bill_id,account_id,due_date,closing_date,total_amount,minimum_payment,currency,updated_at)
                                     VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(bill_id) DO UPDATE SET due_date=excluded.due_date,
                                     closing_date=excluded.closing_date,total_amount=excluded.total_amount,
                                     minimum_payment=excluded.minimum_payment,currency=excluded.currency,updated_at=excluded.updated_at""",
                                   (bill.get("id"), account_id, bill.get("dueDate") or "", bill.get("billClosingDate") or "",
                                    _amount(bill.get("totalAmount")), _amount(bill.get("minimumPaymentAmount")),
                                    bill.get("totalAmountCurrencyCode") or "BRL", updated))
                if page >= int(bills_data.get("totalPages", 1) or 1):
                    break
                page += 1

    try:
        sync_item_investments(item_id, institution)
    except ExternalServiceError:
        pass

    with database() as db:
        db.execute("UPDATE items SET last_sync=?,status=? WHERE item_id=?", (updated, item_status, item_id))
    return count


def sync_all_items() -> int:
    with database() as db:
        ids = [row["item_id"] for row in db.execute("SELECT item_id FROM items ORDER BY created_at")]
    total = 0
    errors = []
    for item_id in ids:
        try:
            total += sync_item(item_id)
        except ExternalServiceError as exc:
            errors.append(str(exc))
    set_setting("last_sync", iso_now())
    if errors and not total:
        raise ExternalServiceError(errors[0])
    return total


def records(query: str, params: tuple = ()) -> list[dict]:
    with database() as db:
        return [dict(row) for row in db.execute(query, params).fetchall()]


def get_report_metrics(start: date, end: date) -> dict:
    with database() as db:
        txs = [dict(row) for row in db.execute("SELECT * FROM transactions WHERE substr(date,1,10)>=? AND substr(date,1,10)<=? "
                                               "AND upper(coalesce(status,'')) NOT IN ('PENDING','PENDENTE') ORDER BY date",
                                               (start.isoformat(), end.isoformat())).fetchall()]
        investments = [dict(row) for row in db.execute("SELECT * FROM investments ORDER BY amount DESC").fetchall()]
    totals = transaction_totals(txs)
    categories = category_spending_totals(txs, end)
    return {"transactions": txs, "investments": investments, **totals,
            "expense": totals["consumption_expenses"], "investment_outflow": totals["invested_outflow"],
            "categories": dict(sorted(categories.items(), key=lambda item: item[1], reverse=True))}


def brl(value: float) -> str:
    rendered = f"{value:,.2f}"
    rendered = rendered.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {rendered}"


def weekly_analysis(metrics: dict) -> str:
    with database() as db:
        all_txs = [dict(row) for row in db.execute(
            "SELECT date,description,amount,direction,flow,category,institution,account_name,account_type,status "
            "FROM transactions WHERE upper(coalesce(status,'')) NOT IN ('PENDING','PENDENTE') "
            "AND substr(date,1,10)<=? ORDER BY date DESC", (local_now().date().isoformat(),)
        ).fetchall()]
    portfolio = []
    for investment in metrics["investments"]:
        try:
            details = json.loads(investment.get("details") or "{}")
        except (TypeError, json.JSONDecodeError):
            details = {}
        portfolio.append({
            "nome": investment["name"], "tipo": investment["kind"], "instituicao": investment["institution"],
            "origem": investment.get("source", "manual"), "valor_atual_brl": investment["amount"],
            "atualizado_em": investment["updated_on"], "status": investment.get("status", ""),
            "emissor": details.get("issuer"), "valor_original_brl": details.get("amountOriginal"),
            "lucro_atual_brl": details.get("amountProfit"), "taxa_anual": details.get("annualRate"),
            "indexador": details.get("rateType"), "vencimento": details.get("dueDate"),
            "observacoes": investment["notes"],
        })
    data = {
        "periodo_analisado": {"inicio": (local_now().date()-timedelta(days=6)).isoformat(), "fim": local_now().date().isoformat()},
        "resumo_periodo": {"entradas_brl": round(metrics["income"], 2),
                           "gastos_de_consumo_brl": round(metrics["expense"], 2),
                           "saidas_de_caixa_brl": round(metrics["cash_outflow"], 2),
                           "pagamentos_de_fatura_brl": round(metrics["card_payments"], 2),
                           "compras_no_credito_brl": round(metrics["credit_purchases"], 2),
                           "estornos_no_credito_brl": round(metrics["card_refunds"], 2),
                           "aportes_importados_brl": round(metrics["investment_outflow"], 2),
                           "saldo_de_caixa_brl": round(metrics["net"], 2),
                           "despesas_por_categoria_brl": {k: round(v, 2) for k,v in metrics["categories"].items()}},
        "historico_completo_de_movimentacoes": all_txs,
        "carteira_de_investimentos": portfolio,
    }
    prompt = (
        "Analise o JSON enviado em <stdin> como um assistente de financas pessoais. Use somente os dados fornecidos; "
        "nao acesse arquivos, nao execute comandos e nao tente conectar a servicos. Diferencie gastos de consumo, "
        "compras no credito, saidas de caixa e pagamentos de fatura: uma compra no credito entra como gasto na data da compra, "
        "mas so reduz o caixa quando a fatura e paga. O pagamento da fatura nao e uma segunda despesa de consumo. Avalie "
        "entradas e gastos, identifique "
        "as categorias e comerciantes que mais pesam, mudancas que merecem atencao, assinaturas ou padroes recorrentes "
        "quando houver evidencia suficiente, e sugira de 3 a 5 acoes concretas para economizar. Considere a carteira "
        "manual e a carteira sincronizada pelo Pluggy como patrimonio, diferencie a origem e os tipos de investimento, "
        "sem confundi-las com renda disponivel. Aponte lacunas ou classificacoes incertas, nao invente dados e nao "
        "recomende compra ou venda de ativos. Responda em portugues do Brasil, com titulo curto, leitura do periodo, "
        "oportunidades praticas e uma observacao de incerteza quando necessario. Seja conciso para envio por WhatsApp."
    )
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    cmd_name = get_setting("codex_path", "codex") or "codex"
    executable = shutil.which(cmd_name) or (cmd_name if Path(cmd_name).exists() else "")
    if not executable:
        raise ExternalServiceError("Codex CLI não encontrado. Configure o caminho em Configurações e autentique o CLI neste servidor.")
    model = get_setting("codex_model", "gpt-6-luna") or "gpt-6-luna"
    effort = get_setting("codex_effort", "max") or "max"
    run_dir = DATA_DIR / "codex-workspace"
    run_dir.mkdir(exist_ok=True)
    command = [executable, "exec", "--model", model, "--sandbox", "read-only", "--skip-git-repo-check", "--cd", str(run_dir),
               "-c", f'model_reasoning_effort="{effort}"', prompt]
    env = os.environ.copy()
    for secret_name in ("APP_ENCRYPTION_KEY", "APP_SESSION_KEY", "WPP_SECRET_KEY", "WHATSAPP_SECRET",
                        "PLUGGY_CLIENT_ID", "PLUGGY_CLIENT_SECRET", "PLUGGY_API_KEY", "OPENAI_API_KEY",
                        "CODEX_ACCESS_TOKEN"):
        env.pop(secret_name, None)
    # Keep the caller's Codex profile on a normal local run. Container deployments set
    # CODEX_HOME explicitly to a persistent directory inside this project.
    if os.environ.get("CODEX_HOME"):
        env["CODEX_HOME"] = os.environ["CODEX_HOME"]
    try:
        result = subprocess.run(command, input=payload, text=True, encoding="utf-8", errors="replace",
                                capture_output=True, timeout=600,
                                cwd=str(run_dir), env=env, check=False)
    except subprocess.TimeoutExpired:
        raise ExternalServiceError("A análise do Codex ultrapassou 10 minutos.") from None
    except OSError:
        raise ExternalServiceError("Não foi possível iniciar o Codex CLI neste servidor.") from None
    if result.returncode != 0:
        raise ExternalServiceError("A análise pelo Codex falhou. Confira a autenticação e o modelo no servidor.")
    answer = result.stdout.strip()
    if not answer:
        raise ExternalServiceError("O Codex CLI terminou sem retornar uma análise.")
    return answer[:7000]


def report_text(kind: str, start: date, end: date, analysis: str = "") -> tuple[str, dict]:
    metrics = get_report_metrics(start, end)
    title = "Resumo diário" if kind == "daily" else "Resumo semanal"
    lines = [f"*{title} · {start.strftime('%d/%m')} a {end.strftime('%d/%m')}*",
             f"Entradas: {brl(metrics['income'])}",
             f"Gastos de consumo (inclui compras no crédito): {brl(metrics['expense'])}",
             f"Saídas de caixa: {brl(metrics['cash_outflow'])}"]
    if metrics["card_payments"]:
        lines.append(f"Pagamentos de fatura: {brl(metrics['card_payments'])}")
    if metrics["investment_outflow"]:
        lines.append(f"Aportes identificados no extrato: {brl(metrics['investment_outflow'])}")
    lines.append(f"Saldo de caixa do período: {brl(metrics['net'])}")
    if metrics["categories"]:
        top = [(name, amount) for name, amount in metrics["categories"].items() if amount > 0][:3]
        if top:
            lines.append("Maiores categorias: " + " · ".join(f"{name} {brl(amount)}" for name, amount in top))
    if analysis:
        lines += ["", "*Análise GPT-6 Luna Max*", analysis]
    lines.append("Carteira de investimentos (Pluggy + manual): " + brl(sum(i["amount"] for i in metrics["investments"])))
    return "\n".join(lines)[:9000], metrics


def whatsapp_request(path: str, method: str = "GET", payload: dict | None = None) -> dict:
    base = get_setting("whatsapp_base_url", os.environ.get("WPP_BASE_URL", "http://127.0.0.1:21465")).strip().rstrip("/")
    session = get_setting("whatsapp_session", "financas").strip()
    token = get_secret("whatsapp_token")
    if not base or not session:
        raise ExternalServiceError("Configure o endereço e o nome da sessão WPPConnect.")
    if not token:
        raise ExternalServiceError("Conecte o WhatsApp e leia o QR Code primeiro.")
    return _request_json(base + path, method, {"Authorization": "Bearer " + token}, payload)


def whatsapp_qrcode() -> str:
    base = get_setting("whatsapp_base_url", os.environ.get("WPP_BASE_URL", "http://127.0.0.1:21465")).strip().rstrip("/")
    session = get_setting("whatsapp_session", "financas").strip()
    token = get_secret("whatsapp_token")
    if not token:
        return ""
    url = base + f"/api/{urllib.parse.quote(session, safe='')}/qrcode-session"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept": "application/json,image/png"})
    try:
        with urllib.request.urlopen(req, timeout=35) as response:
            body = response.read()
            content_type = response.headers.get_content_type()
        if content_type.startswith("image/"):
            return f"data:{content_type};base64," + base64.b64encode(body).decode("ascii")
        try:
            data = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return body.decode("utf-8", errors="replace").strip()
        qr = data.get("qrcode") or data.get("qrCode") or ""
        if isinstance(qr, dict):
            qr = qr.get("base64") or qr.get("url") or qr.get("code") or ""
        return str(qr)
    except urllib.error.HTTPError as exc:
        raise ExternalServiceError(f"Serviço externo respondeu com HTTP {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError):
        raise ExternalServiceError("Não foi possível alcançar o serviço externo.") from None


def send_whatsapp(message: str) -> None:
    phone = re.sub(r"\D", "", get_setting("whatsapp_phone", ""))
    if len(phone) < 10:
        raise ExternalServiceError("Informe em Configurações o número WhatsApp com DDI e DDD.")
    session = get_setting("whatsapp_session", "financas").strip()
    whatsapp_request(f"/api/{urllib.parse.quote(session, safe='')}/send-message", "POST",
                     {"phone": phone, "message": message})


def deliver_report(kind: str, run_ai: bool = False, scheduled: bool = False) -> dict:
    if not get_secret("whatsapp_token") or len(re.sub(r"\D", "", get_setting("whatsapp_phone", ""))) < 10:
        raise ExternalServiceError("Configure e conecte o WhatsApp antes de gerar um relatório para envio.")
    today = local_now().date()
    if kind == "daily":
        start = end = today
        key = today.isoformat() if scheduled else "manual-" + iso_now()
    else:
        end = today - timedelta(days=1) if scheduled else today
        start = end - timedelta(days=6)
        key = f"{start.isoformat()}_{end.isoformat()}" if scheduled else "manual-" + iso_now()
    metrics = get_report_metrics(start, end)
    analysis = weekly_analysis(metrics) if run_ai else ""
    message, _ = report_text(kind, start, end, analysis)
    send_whatsapp(message)
    with database() as db:
        db.execute("INSERT OR REPLACE INTO reports(kind,period_key,created_at,message,analysis,status) VALUES(?,?,?,?,?,?)",
                   (kind, key, iso_now(), message, analysis, "sent"))
    if scheduled:
        set_setting(f"last_{kind}_report_key", key)
    return {"message": message, "analysis": analysis}


def start_weekly_report_job() -> str:
    global _active_weekly_job
    with _report_jobs_lock:
        if _active_weekly_job:
            active_job = _report_jobs.get(_active_weekly_job)
            if active_job and active_job["status"] == "running":
                return _active_weekly_job
        job_id = secrets.token_urlsafe(18)
        _report_jobs[job_id] = {"kind": "weekly", "status": "running", "created_at": iso_now(), "error": ""}
        _active_weekly_job = job_id

    def run_job() -> None:
        global _active_weekly_job
        try:
            deliver_report("weekly", run_ai=True)
            status, error = "sent", ""
        except ExternalServiceError as exc:
            status, error = "failed", str(exc)
        except Exception:
            app.logger.exception("Unexpected failure while generating the weekly WhatsApp report")
            status, error = "failed", "Ocorreu um erro interno durante a análise semanal. Consulte os logs do servidor."
        with _report_jobs_lock:
            job = _report_jobs.get(job_id)
            if job:
                job.update(status=status, error=error)
            if _active_weekly_job == job_id:
                _active_weekly_job = None
            finished = sorted(
                ((key, value) for key, value in _report_jobs.items() if value["status"] != "running"),
                key=lambda item: item[1]["created_at"], reverse=True)
            for old_id, _ in finished[25:]:
                _report_jobs.pop(old_id, None)

    try:
        threading.Thread(target=run_job, name="weekly-report-job", daemon=True).start()
    except RuntimeError:
        with _report_jobs_lock:
            _report_jobs[job_id].update(status="failed", error="Não foi possível iniciar a análise semanal no servidor.")
            if _active_weekly_job == job_id:
                _active_weekly_job = None
    return job_id


def scheduler_loop() -> None:
    while True:
        try:
            current = local_now()
            frequency = get_setting("report_frequency", "weekly")
            daily_time = get_setting("daily_time", "20:00")
            weekly_time = get_setting("weekly_time", "09:00")
            try:
                daily_hour, daily_minute = [int(x) for x in daily_time.split(":", 1)]
                weekly_hour, weekly_minute = [int(x) for x in weekly_time.split(":", 1)]
            except ValueError:
                daily_hour, daily_minute, weekly_hour, weekly_minute = 20, 0, 9, 0
            if frequency in ("daily", "daily_weekly") and (current.hour, current.minute) == (daily_hour, daily_minute):
                day_key = current.date().isoformat()
                if get_setting("last_daily_report_key") != day_key:
                    set_setting("last_daily_report_key", day_key)
                    try:
                        deliver_report("daily", scheduled=True)
                    except ExternalServiceError as exc:
                        set_setting("last_schedule_error", f"Resumo diário: {exc}")
            weekly_day = int(get_setting("weekly_day", "0") or "0")
            if frequency in ("weekly", "daily_weekly") and current.weekday() == weekly_day and (current.hour, current.minute) == (weekly_hour, weekly_minute):
                end = current.date() - timedelta(days=1)
                key = f"{(end-timedelta(days=6)).isoformat()}_{end.isoformat()}"
                if get_setting("last_weekly_report_key") != key:
                    set_setting("last_weekly_report_key", key)
                    try:
                        deliver_report("weekly", run_ai=True, scheduled=True)
                    except ExternalServiceError as exc:
                        set_setting("last_schedule_error", f"Resumo semanal: {exc}")
            interval = int(get_setting("sync_interval_hours", "8") or "8")
            last_sync = get_setting("last_sync", "")
            if interval > 0 and owner_exists() and records("SELECT item_id FROM items LIMIT 1"):
                due = True
                try:
                    due = now_utc() - datetime.fromisoformat(last_sync) >= timedelta(hours=interval)
                except ValueError:
                    pass
                if due:
                    try:
                        sync_all_items()
                        set_setting("last_sync_error", "")
                    except ExternalServiceError as exc:
                        set_setting("last_sync_error", str(exc))
        except Exception:
            pass
        time.sleep(45)


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    csrf = getattr(request, "_csrf_to_set", request.cookies.get(CSRF_COOKIE, ""))
    if csrf:
        response.set_cookie(CSRF_COOKIE, csrf, httponly=False,
                            secure=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
                            samesite="Strict", path="/")
    return response


@app.get("/")
def home():
    if not owner_exists():
        return redirect(url_for("setup"))
    if not authenticated():
        return redirect(url_for("login"))
    token = csrf_value()
    return make_response(render_template("index.html", csrf_token=token, categories=CATEGORIES))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if owner_exists():
        return redirect(url_for("login"))
    token = csrf_value()
    error = ""
    if request.method == "POST":
        if not csrf_valid():
            error = "Atualize a página e tente novamente."
        else:
            password = request.form.get("password", "")
            confirmation = request.form.get("confirmation", "")
            if len(password) < 12:
                error = "Use uma senha com pelo menos 12 caracteres."
            elif password != confirmation:
                error = "As senhas não coincidem."
            else:
                salt = secrets.token_bytes(16)
                digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000).hex()
                with database() as db:
                    db.execute("INSERT INTO owner(id,password_hash,password_salt,created_at) VALUES(1,?,?,?)",
                               (digest, base64.b64encode(salt).decode(), iso_now()))
                response = redirect(url_for("home"))
                response.set_cookie(SESSION_COOKIE, make_session_cookie(), httponly=True,
                                    secure=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
                                    samesite="Strict", max_age=SESSION_DAYS*86400, path="/")
                return response
    return render_template("auth.html", mode="setup", csrf_token=token, error=error)


@app.route("/login", methods=["GET", "POST"])
def login():
    if not owner_exists():
        return redirect(url_for("setup"))
    if authenticated():
        return redirect(url_for("home"))
    token = csrf_value()
    error = ""
    if request.method == "POST":
        if not csrf_valid():
            error = "Atualize a página e tente novamente."
        elif login_limited(request.remote_addr or "unknown"):
            error = "Muitas tentativas. Aguarde 15 minutos e tente novamente."
        else:
            password = request.form.get("password", "")
            with database() as db:
                owner = db.execute("SELECT password_hash,password_salt FROM owner WHERE id=1").fetchone()
            if owner:
                salt = base64.b64decode(owner["password_salt"])
                digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000).hex()
                if hmac.compare_digest(digest, owner["password_hash"]):
                    with _login_attempts_lock:
                        _login_attempts.pop(request.remote_addr or "unknown", None)
                    response = redirect(url_for("home"))
                    response.set_cookie(SESSION_COOKIE, make_session_cookie(), httponly=True,
                                        secure=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
                                        samesite="Strict", max_age=SESSION_DAYS*86400, path="/")
                    return response
            login_limited(request.remote_addr or "unknown", failed=True)
            error = "Senha incorreta."
    return render_template("auth.html", mode="login", csrf_token=token, error=error)


@app.post("/logout")
@login_required
def logout():
    if not csrf_valid():
        return redirect(url_for("home"))
    response = redirect(url_for("login"))
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/summary")
@login_required
def api_summary():
    today = local_now().date()
    month_start = today.replace(day=1)
    with database() as db:
        txs = [dict(r) for r in db.execute("SELECT * FROM transactions WHERE substr(date,1,10)>=? AND substr(date,1,10)<=? AND upper(coalesce(status,'')) NOT IN ('PENDING','PENDENTE') ORDER BY julianday(date) DESC, date DESC, updated_at DESC",
                                           (month_start.isoformat(), today.isoformat())).fetchall()]
        accounts = [dict(r) for r in db.execute("SELECT a.*, i.institution AS item_institution FROM accounts a LEFT JOIN items i ON i.item_id=a.item_id ORDER BY a.type DESC,a.institution,a.name").fetchall()]
        bills = [dict(r) for r in db.execute("SELECT b.*,a.name AS account_name,a.institution FROM bills b JOIN accounts a ON a.account_id=b.account_id ORDER BY b.due_date DESC LIMIT 12").fetchall()]
        investments = [dict(r) for r in db.execute("SELECT * FROM investments ORDER BY amount DESC").fetchall()]
        recent = [dict(r) for r in db.execute("SELECT * FROM transactions ORDER BY julianday(date) DESC, date DESC, updated_at DESC LIMIT 80").fetchall()]
        chart_txs = [dict(r) for r in db.execute("SELECT date,amount,flow,account_type FROM transactions WHERE substr(date,1,10)>=? AND substr(date,1,10)<=? AND upper(coalesce(status,'')) NOT IN ('PENDING','PENDENTE') ORDER BY date",
                                                  ((today-timedelta(days=29)).isoformat(), today.isoformat())).fetchall()]
        connected = [dict(r) for r in db.execute("SELECT * FROM items ORDER BY created_at DESC").fetchall()]
    totals = transaction_totals(txs)
    categories = [{"name": name, "amount": amount}
                  for name, amount in sorted(category_spending_totals(txs, today).items(), key=lambda item: item[1], reverse=True)]
    days = [(today-timedelta(days=n)) for n in range(29,-1,-1)]
    daily = {d.isoformat(): {"income":0.0,"expense":0.0,"investment":0.0} for d in days}
    for tx in chart_txs:
        tx_day = (tx["date"] or "")[:10]
        if tx_day not in daily:
            continue
        flow = tx["flow"]
        is_credit = str(tx.get("account_type") or "BANK").upper() == "CREDIT"
        if flow == "income" and not is_credit:
            daily[tx_day]["income"] += tx["amount"]
        elif not is_credit and flow in ("expense", "card_payment"):
            daily[tx_day]["expense"] += tx["amount"]
        elif flow == "investment":
            daily[tx_day]["investment"] += tx["amount"]
    flow_data=[]
    cumulative=0.0
    for d in days:
        row=daily[d.isoformat()]
        net=row["income"]-row["expense"]-row["investment"]
        cumulative += net
        flow_data.append({"date":d.isoformat(),"income":round(row["income"],2),"expense":round(row["expense"],2),
                          "investment":round(row["investment"],2),"net":round(net,2),"cumulative":round(cumulative,2)})
    card_rows = [a for a in accounts if a["type"] == "CREDIT"]
    for card in card_rows:
        card_txs = [tx for tx in txs if tx["account_id"] == card["account_id"]]
        card["month_spend"] = round(
            sum(tx["amount"] for tx in card_txs if tx["flow"] == "expense")
            - sum(tx["amount"] for tx in card_txs if tx["flow"] == "card_refund"), 2
        )
        card["latest_bill"] = next((b for b in bills if b["account_id"]==card["account_id"]),None)
    return jsonify({"today":today.isoformat(),"month_label":today.strftime("%B %Y"),"income":totals["income"],
                    "expenses":totals["cash_outflow"], "cash_outflow":totals["cash_outflow"],
                    "consumption_expenses":totals["consumption_expenses"], "card_payments":totals["card_payments"],
                    "credit_purchases":totals["credit_purchases"], "card_refunds":totals["card_refunds"],
                    "invested_outflow":totals["invested_outflow"],"net":totals["net"],
                    "investment_total":round(sum(i["amount"] for i in investments),2),"categories":categories,
                    "flow":flow_data,"transactions":recent,"accounts":accounts,"cards":card_rows,"bills":bills,
                    "investments":investments,"items":connected,"last_sync":get_setting("last_sync",""),
                    "reports":records("SELECT * FROM reports ORDER BY created_at DESC LIMIT 8"),"categories_list":CATEGORIES})


@app.get("/api/transactions")
@login_required
def api_transactions():
    start_raw = request.args.get("start", "").strip()
    end_raw = request.args.get("end", "").strip()
    try:
        start = date.fromisoformat(start_raw).isoformat() if start_raw else ""
        end = date.fromisoformat(end_raw).isoformat() if end_raw else ""
    except ValueError:
        return jsonify(error="Informe datas válidas para filtrar o extrato."), 400
    if start and end and start > end:
        return jsonify(error="A data inicial deve ser anterior ou igual à data final."), 400
    conditions = []
    params = []
    if start:
        conditions.append("substr(date,1,10)>=?")
        params.append(start)
    if end:
        conditions.append("substr(date,1,10)<=?")
        params.append(end)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    with database() as db:
        txs = [dict(row) for row in db.execute(
            "SELECT * FROM transactions" + where + " ORDER BY julianday(date) DESC, date DESC, updated_at DESC",
            tuple(params)).fetchall()]
    totals = transaction_totals(txs, local_now().date())
    return jsonify(transactions=txs, count=len(txs), totals=totals)


def _forecast_description_key(value: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"\d+", " ", text)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z]+", " ", text)).strip()


def _likely_fixed_expense(description: str, category: str, monthly_values: list[float]) -> bool:
    key = _forecast_description_key(description)
    fixed_terms = ("aluguel", "locacao", "imobiliaria", "condominio", "copel", "sanepar", "sabesp",
                   "copasa", "cemig", "enel", "energia", "eletric", "agua", "internet", "banda larga",
                   "telecom", "telefone", "celular", "vivo", "claro", "tim", "seguro", "plano de saude",
                   "mensalidade", "escola", "faculdade", "curso", "parcela", "parcelado", "assinatura",
                   "subscription", "netflix", "paramount", "spotify", "prime video", "disney", "youtube premium",
                   "icloud", "google one", "hbo max", "globoplay", "concessionaria")
    if any(term in key for term in fixed_terms) or re.search(r"\d+\s*/\s*\d+", str(description or "")):
        return True
    variable_terms = ("supermerc", "mercado", "hipermerc", "mercearia", "padaria", "hortifruti", "restaurante",
                      "lanchonete", "pizzaria", "ifood", "uber", "99app", "combust", "gasolina", "posto ",
                      "estacionamento", "farmacia", "drogaria", "shopping", "loja ", "amazon", "mercadolivre",
                      "mercado pago", "cinema", "delivery", "pet shop", "auto mec", "mecanica")
    if any(term in key for term in variable_terms):
        return False
    if category in {"Moradia", "Assinaturas", "Educação"}:
        return True
    if category in {"Alimentação", "Compras", "Transporte", "Lazer"}:
        return False
    transfer_prefixes = ("transferencia enviada", "transferencia efetuada", "pix enviado", "pagamento efetuado")
    if not key.startswith(transfer_prefixes):
        return False
    median_value = statistics.median(monthly_values) if monthly_values else 0
    return median_value > 0 and (max(monthly_values) - min(monthly_values)) / median_value <= 0.08

def _forecast_account_label(account: dict) -> str:
    institution = str(account.get("institution") or "").strip()
    product = str(account.get("credit_level") or account.get("marketing_name") or "Cartão de crédito").replace("_", " ").strip().title()
    digits = re.sub(r"\D", "", str(account.get("number") or ""))
    if digits:
        product += " · final " + digits[-4:]
    return f"{institution} · {product}" if institution else product

def build_forecast() -> dict:
    today = local_now().date()
    history_end = today - timedelta(days=1)
    fixed_start = history_end - timedelta(days=179)
    recent_start = history_end - timedelta(days=89)
    with database() as db:
        txs = [dict(row) for row in db.execute(
            "SELECT transaction_id,account_id,date,description,amount,flow,category,institution,account_name,account_type,status "
            "FROM transactions WHERE flow='expense' AND substr(date,1,10)>=? AND substr(date,1,10)<=? "
            "AND upper(coalesce(status,'')) NOT IN ('PENDING','PENDENTE') "
            "ORDER BY substr(date,1,10) DESC, updated_at DESC",
            (fixed_start.isoformat(), history_end.isoformat())).fetchall()]
        account_rows = [dict(row) for row in db.execute(
            "SELECT a.account_id,a.name,a.marketing_name,a.type,a.credit_level,a.credit_status,a.number,"
            "COALESCE(NULLIF(a.institution,''),i.institution,'') AS institution "
            "FROM accounts a LEFT JOIN items i ON i.item_id=a.item_id").fetchall()]
        override_rows = [dict(row) for row in db.execute("SELECT * FROM forecast_overrides").fetchall()]
    accounts = {row["account_id"]: row for row in account_rows}
    for tx in txs:
        account_id = str(tx.get("account_id") or "")
        if account_id and account_id not in accounts:
            accounts[account_id] = {"account_id": account_id, "name": tx.get("account_name") or "",
                                    "marketing_name": "", "institution": tx.get("institution") or "",
                                    "type": tx.get("account_type") or "BANK"}
    overrides = {row["item_key"]: row for row in override_rows}
    inactive_credit_ids = {key for key, account in accounts.items()
                           if str(account.get("type") or "").upper() == "CREDIT"
                           and str(account.get("credit_status") or "").upper() in {"CANCELLED", "CANCELED", "CLOSED", "INACTIVE"}}
    parsed = []
    for tx in txs:
        if str(tx.get("account_id") or "") in inactive_credit_ids:
            continue
        try:
            tx_date = date.fromisoformat(str(tx.get("date") or "")[:10])
        except ValueError:
            continue
        tx["_date"] = tx_date
        tx["_account_id"] = str(tx.get("account_id") or "")
        tx["_account_type"] = str(tx.get("account_type") or "BANK").upper()
        tx["_category"] = str(tx.get("category") or "Outros")
        tx["_amount"] = max(0.0, float(tx.get("amount") or 0))
        parsed.append(tx)

    groups: dict[tuple[str, str], dict] = {}
    for tx in parsed:
        description_key = _forecast_description_key(tx.get("description") or "")
        if len(description_key) < 3:
            continue
        group_key = (tx["_account_id"], description_key)
        group = groups.setdefault(group_key, {"months": {}, "transaction_ids": set(), "label": "",
                                               "category": tx["_category"], "account_type": tx["_account_type"]})
        if not group["label"]:
            group["label"] = str(tx.get("description") or "Despesa recorrente")[:120]
        group["months"][tx["_date"].strftime("%Y-%m")] = group["months"].get(tx["_date"].strftime("%Y-%m"), 0.0) + tx["_amount"]
        group["transaction_ids"].add(tx.get("transaction_id"))
    recurring_ids: set[str] = set()
    items = []
    for (account_id, description_key), group in groups.items():
        if (len(group["months"]) < 3 or len(group["transaction_ids"]) < 3
                or not _likely_fixed_expense(group["label"], group["category"], list(group["months"].values()))):
            continue
        recurring_ids.update(x for x in group["transaction_ids"] if x)
        account = accounts.get(account_id, {})
        is_card = str(account.get("type") or group["account_type"]).upper() == "CREDIT"
        digest = hashlib.sha256((account_id + "\0" + description_key).encode("utf-8")).hexdigest()[:24]
        item_key = "fixed:" + digest
        item = {"item_key": item_key, "label": group["label"], "amount": round(float(statistics.median(group["months"].values())), 2),
                "suggested_amount": round(float(statistics.median(group["months"].values())), 2),
                "cost_type": "fixed", "category": group["category"], "card_account_id": account_id if is_card else "",
                "account_label": _forecast_account_label(account) if is_card else "", "source": "history", "is_custom": False}
        items.append(item)

    recent_by_account: dict[str, list[dict]] = {}
    for tx in parsed:
        if recent_start <= tx["_date"] <= history_end:
            recent_by_account.setdefault(tx["_account_id"], []).append(tx)
    exposure_days = {}
    for account_id, account_txs in recent_by_account.items():
        first_day = min(tx["_date"] for tx in account_txs)
        exposure_days[account_id] = min(90, max(30, (history_end - first_day).days + 1))

    variable_categories: dict[str, float] = {}
    variable_cards: dict[str, float] = {}
    for account_id, account_txs in recent_by_account.items():
        scale = 30.0 / max(30, exposure_days.get(account_id, 30))
        for tx in account_txs:
            if tx.get("transaction_id") in recurring_ids:
                continue
            estimate = tx["_amount"] * scale
            if tx["_account_type"] == "CREDIT":
                variable_cards[account_id] = variable_cards.get(account_id, 0.0) + estimate
            else:
                category = tx["_category"]
                variable_categories[category] = variable_categories.get(category, 0.0) + estimate

    for category, amount in sorted(variable_categories.items()):
        item_key = "category:" + hashlib.sha256(category.encode("utf-8")).hexdigest()[:24]
        items.append({"item_key": item_key, "label": category + " · demais despesas", "amount": round(amount, 2),
                      "suggested_amount": round(amount, 2), "cost_type": "variable", "category": category,
                      "card_account_id": "", "account_label": "", "source": "history", "is_custom": False})

    credit_ids = {key for key, account in accounts.items()
                  if str(account.get("type") or "").upper() == "CREDIT" and key not in inactive_credit_ids}
    credit_ids.update(variable_cards)
    for account_id in sorted(credit_ids, key=lambda value: _forecast_account_label(accounts.get(value, {})).casefold()):
        account = accounts.get(account_id, {})
        account_label = _forecast_account_label(account)
        digest = hashlib.sha256(account_id.encode("utf-8")).hexdigest()[:24]
        amount = round(variable_cards.get(account_id, 0.0), 2)
        items.append({"item_key": "card:" + digest, "label": "Compras variáveis · " + account_label,
                      "amount": amount, "suggested_amount": amount, "cost_type": "variable", "category": "Cartão de crédito",
                      "card_account_id": account_id, "account_label": account_label, "source": "history", "is_custom": False})

    for item in items:
        override = overrides.get(item["item_key"])
        item["enabled"] = bool(override["enabled"]) if override else True
        item["adjusted"] = bool(override and (override["label_override"] or override["amount_override"] is not None or override["type_override"]))
        if override:
            if override["label_override"]:
                item["label"] = override["label_override"]
            if override["amount_override"] is not None:
                item["amount"] = round(float(override["amount_override"]), 2)
            if override["type_override"] in ("fixed", "variable"):
                item["cost_type"] = override["type_override"]
        if item["card_account_id"]:
            item["account_label"] = _forecast_account_label(accounts.get(item["card_account_id"], {"name": item["account_label"]}))

    custom_rows = [row for row in override_rows if row["is_custom"]]
    for row in custom_rows:
        card_account_id = row["card_account_id"]
        account_label = _forecast_account_label(accounts.get(card_account_id, {})) if card_account_id else ""
        items.append({"item_key": row["item_key"], "label": row["label_override"],
                      "amount": round(float(row["amount_override"] or 0), 2),
                      "suggested_amount": round(float(row["amount_override"] or 0), 2),
                      "cost_type": row["type_override"] or "variable", "category": "Personalizado",
                      "card_account_id": card_account_id, "account_label": account_label, "source": "manual",
                      "is_custom": True, "enabled": bool(row["enabled"]), "adjusted": True})

    included = [item for item in items if item["enabled"]]
    fixed_total = round(sum(item["amount"] for item in included if item["cost_type"] == "fixed"), 2)
    variable_total = round(sum(item["amount"] for item in included if item["cost_type"] == "variable"), 2)
    credit_ids = {key for key, account in accounts.items()
                  if str(account.get("type") or "").upper() == "CREDIT" and key not in inactive_credit_ids}
    credit_ids.update(item["card_account_id"] for item in items if item["card_account_id"])
    card_totals = {account_id: {"account_id": account_id,
                                "label": _forecast_account_label(accounts.get(account_id, {})), "amount": 0.0}
                   for account_id in credit_ids}
    for item in included:
        account_id = item["card_account_id"]
        if account_id:
            card_totals.setdefault(account_id, {"account_id": account_id,
                                                "label": item["account_label"] or _forecast_account_label(accounts.get(account_id, {})),
                                                "amount": 0.0})
            card_totals[account_id]["amount"] += item["amount"]
    cards = sorted(({**card, "amount": round(card["amount"], 2)} for card in card_totals.values()),
                   key=lambda card: card["label"].casefold())
    return {"start_date": today.isoformat(), "end_date": (today + timedelta(days=29)).isoformat(),
            "history_start": recent_start.isoformat(), "history_end": history_end.isoformat(),
            "history_count": len(parsed), "total": round(fixed_total + variable_total, 2),
            "fixed_total": fixed_total, "variable_total": variable_total,
            "cards_total": round(sum(card["amount"] for card in cards), 2), "cards": cards,
            "items": sorted(items, key=lambda item: (item["cost_type"] != "fixed", item["label"].casefold()))}


@app.get("/api/forecast")
@login_required
def api_forecast():
    return jsonify(build_forecast())


@app.post("/api/forecast/items")
@login_required
@api_csrf_required
def api_create_forecast_item():
    body = json_body()
    label = str(body.get("label") or "").strip()[:120]
    cost_type = str(body.get("cost_type") or "variable")
    try:
        amount = round(float(body.get("amount")), 2)
    except (TypeError, ValueError):
        return jsonify(error="Informe um valor válido para o custo."), 400
    if not label or cost_type not in ("fixed", "variable") or not amount >= 0 or amount > 10000000000:
        return jsonify(error="Informe descrição, tipo e um valor igual ou maior que zero."), 400
    card_account_id = str(body.get("card_account_id") or "")
    if card_account_id:
        with database() as db:
            card = db.execute("SELECT account_id FROM accounts WHERE account_id=? AND upper(type)='CREDIT' "
                              "AND upper(coalesce(credit_status,'')) NOT IN ('CANCELLED','CANCELED','CLOSED','INACTIVE')",
                              (card_account_id,)).fetchone()
        if not card:
            return jsonify(error="O cartão escolhido não foi encontrado."), 400
    item_key = "custom:" + secrets.token_urlsafe(15)
    now = iso_now()
    with database() as db:
        db.execute("INSERT INTO forecast_overrides(item_key,label_override,amount_override,type_override,enabled,is_custom,card_account_id,created_at,updated_at) VALUES(?,?,?,?,1,1,?,?,?)",
                   (item_key, label, amount, cost_type, card_account_id, now, now))
    return jsonify(ok=True, item_key=item_key), 201


@app.patch("/api/forecast/items/<item_key>")
@login_required
@api_csrf_required
def api_update_forecast_item(item_key: str):
    body = json_body()
    with database() as db:
        row = db.execute("SELECT * FROM forecast_overrides WHERE item_key=?", (item_key,)).fetchone()
        existing = dict(row) if row else None
    if body.get("reset"):
        if existing and existing["is_custom"]:
            return jsonify(error="Custos personalizados devem ser excluídos, não restaurados."), 400
        with database() as db:
            db.execute("DELETE FROM forecast_overrides WHERE item_key=? AND is_custom=0", (item_key,))
        return jsonify(ok=True)
    is_custom = bool(existing and existing["is_custom"])
    if not is_custom:
        available = {item["item_key"] for item in build_forecast()["items"] if not item["is_custom"]}
        if item_key not in available:
            return jsonify(error="Este item não está mais disponível na previsão. Atualize a página."), 404
    current_label = existing["label_override"] if existing else ""
    current_amount = existing["amount_override"] if existing else None
    current_type = existing["type_override"] if existing else None
    current_enabled = bool(existing["enabled"]) if existing else True
    if "label" in body:
        current_label = str(body["label"] or "").strip()[:120]
        if not current_label:
            return jsonify(error="A descrição não pode ficar vazia."), 400
    if "amount" in body:
        try:
            current_amount = round(float(body["amount"]), 2)
        except (TypeError, ValueError):
            return jsonify(error="Informe um valor válido para o custo."), 400
        if not current_amount >= 0 or current_amount > 10000000000:
            return jsonify(error="O valor precisa ser igual ou maior que zero."), 400
    if "cost_type" in body:
        current_type = str(body["cost_type"])
        if current_type not in ("fixed", "variable"):
            return jsonify(error="Escolha custo fixo ou variável."), 400
    if "enabled" in body:
        current_enabled = bool(body["enabled"])
    now = iso_now()
    with database() as db:
        db.execute("INSERT INTO forecast_overrides(item_key,label_override,amount_override,type_override,enabled,is_custom,card_account_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                   "ON CONFLICT(item_key) DO UPDATE SET label_override=excluded.label_override,amount_override=excluded.amount_override,type_override=excluded.type_override,enabled=excluded.enabled,updated_at=excluded.updated_at",
                   (item_key, current_label, current_amount, current_type, int(current_enabled), int(is_custom),
                    existing["card_account_id"] if existing else "", existing["created_at"] if existing else now, now))
    return jsonify(ok=True)


@app.delete("/api/forecast/items/<item_key>")
@login_required
@api_csrf_required
def api_delete_forecast_item(item_key: str):
    with database() as db:
        row = db.execute("SELECT is_custom FROM forecast_overrides WHERE item_key=?", (item_key,)).fetchone()
        if row and row["is_custom"]:
            db.execute("DELETE FROM forecast_overrides WHERE item_key=?", (item_key,))
            return jsonify(ok=True, deleted=True)
        db.execute("DELETE FROM forecast_overrides WHERE item_key=? AND is_custom=0", (item_key,))
    return jsonify(ok=True, deleted=False)


@app.get("/api/settings")
@login_required
def api_get_settings():
    defaults={"report_frequency":"weekly","daily_time":"20:00","weekly_day":"0","weekly_time":"09:00",
              "timezone":"America/Sao_Paulo","sync_interval_hours":"8","whatsapp_base_url":os.environ.get("WPP_BASE_URL","http://127.0.0.1:21465"),
              "whatsapp_session":"financas","codex_path":"codex","codex_model":"gpt-6-luna","codex_effort":"max"}
    keys=list(defaults)
    values={key:get_setting(key,defaults[key]) for key in keys}
    values["pluggy_configured"]=bool(get_secret("pluggy_client_id") and get_secret("pluggy_client_secret"))
    values["pluggy_sandbox"]=get_setting("pluggy_sandbox","false")=="true"
    values["whatsapp_secret_configured"]=bool(get_secret("whatsapp_secret"))
    values["whatsapp_connected"]=bool(get_secret("whatsapp_token"))
    values["whatsapp_phone"]=get_setting("whatsapp_phone","")
    return jsonify(values)


@app.post("/api/settings")
@login_required
@api_csrf_required
def api_save_settings():
    body=json_body()
    allowed={"report_frequency":{"off","daily","weekly","daily_weekly"},"daily_time":None,"weekly_day":None,
             "weekly_time":None,"timezone":None,"sync_interval_hours":None,"whatsapp_base_url":None,
             "whatsapp_session":None,"whatsapp_phone":None,"codex_path":None,"codex_model":None,
             "codex_effort":{"low","medium","high","xhigh","max"}}
    for key, choices in allowed.items():
        if key not in body:
            continue
        value=str(body[key]).strip()
        if choices and value not in choices:
            return jsonify(error=f"Valor inválido para {key}."),400
        if key.endswith("_time") and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d",value):
            return jsonify(error="Informe o horário no formato HH:MM."),400
        if key=="weekly_day" and value not in {"0","1","2","3","4","5","6"}:
            return jsonify(error="Dia da semana inválido."),400
        if key=="sync_interval_hours":
            try:
                if int(value) not in {0,4,8,12,24}:
                    raise ValueError()
            except ValueError:
                return jsonify(error="A sincronização deve ocorrer a cada 4, 8, 12 ou 24 horas, ou ficar desligada."),400
        if key=="timezone":
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError,ValueError):
                return jsonify(error="Fuso horário desconhecido."),400
        if key=="whatsapp_base_url" and value and not value.startswith(("http://","https://")):
            return jsonify(error="O endereço WPPConnect deve começar com http:// ou https://."),400
        set_setting(key,value)
    if "pluggy_client_id" in body:
        if body["pluggy_client_id"]:
            set_secret("pluggy_client_id",str(body["pluggy_client_id"]).strip())
        if body.get("remove_pluggy"):
            delete_secret("pluggy_client_id"); delete_secret("pluggy_client_secret")
            _pluggy_cache.update(key="",expires=0)
        elif body.get("pluggy_client_secret"):
            set_secret("pluggy_client_secret",str(body["pluggy_client_secret"]).strip())
            _pluggy_cache.update(key="",expires=0)
    if body.get("whatsapp_secret"):
        set_secret("whatsapp_secret",str(body["whatsapp_secret"]).strip())
        delete_secret("whatsapp_token")
    return jsonify(ok=True,message="Configurações salvas.")


@app.post("/api/openfinance/token")
@login_required
@api_csrf_required
def api_openfinance_token():
    try:
        pluggy_api_key()
        response=pluggy_request("/connect_token","POST",{"options":{"clientUserId":"controle-financeiro-pessoal"}})
        token=response.get("accessToken") or response.get("connectToken")
        if not token:
            raise ExternalServiceError("A Pluggy não retornou o token de conexão.")
        return jsonify(connectToken=token,includeSandbox=get_setting("pluggy_sandbox","false")=="true")
    except ExternalServiceError as exc:
        return jsonify(error=str(exc)),502


@app.post("/api/openfinance/items")
@login_required
@api_csrf_required
def api_openfinance_add_item():
    body=json_body()
    item=body.get("item") or {}
    item_id=str(item.get("id") or body.get("item_id") or "").strip()
    if not item_id or len(item_id)>160:
        return jsonify(error="Não foi possível identificar a conexão Pluggy."),400
    connector=item.get("connector") or {}
    institution=connector.get("name") or item.get("name") or "Instituição financeira"
    with database() as db:
        db.execute("INSERT INTO items(item_id,institution,status,created_at) VALUES(?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET institution=excluded.institution,status=excluded.status",
                   (item_id,institution,item.get("status") or "CONNECTED",iso_now()))
    try:
        count=sync_item(item_id)
        set_setting("last_sync",iso_now())
        return jsonify(ok=True,imported=count)
    except ExternalServiceError as exc:
        return jsonify(error=str(exc),connected=True),502


@app.post("/api/openfinance/sync")
@login_required
@api_csrf_required
def api_openfinance_sync():
    try:
        count=sync_all_items()
        return jsonify(ok=True,imported=count,last_sync=get_setting("last_sync",""))
    except ExternalServiceError as exc:
        return jsonify(error=str(exc)),502


@app.delete("/api/openfinance/items/<item_id>")
@login_required
@api_csrf_required
def api_openfinance_delete_item(item_id: str):
    try:
        pluggy_request("/items/"+urllib.parse.quote(item_id,safe=""),"DELETE")
    except ExternalServiceError as exc:
        return jsonify(error=str(exc)),502
    with database() as db:
        db.execute("DELETE FROM investments WHERE source='pluggy' AND item_id=?", (item_id,))
        db.execute("DELETE FROM items WHERE item_id=?",(item_id,))
    return jsonify(ok=True)


@app.patch("/api/transactions/<transaction_id>")
@login_required
@api_csrf_required
def api_update_transaction(transaction_id: str):
    body=json_body()
    category=str(body.get("category","")).strip()
    if category not in CATEGORIES:
        return jsonify(error="Categoria inválida."),400
    with database() as db:
        row=db.execute("SELECT direction,account_type FROM transactions WHERE transaction_id=?",(transaction_id,)).fetchone()
        if not row:
            return jsonify(error="Movimentação não encontrada."),404
        flow=category_flow(category,row["direction"],row["account_type"])
        db.execute("UPDATE transactions SET category=?,category_source='manual',flow=? WHERE transaction_id=?",
                   (category,flow,transaction_id))
    return jsonify(ok=True)


@app.post("/api/openfinance/investments/sync")
@login_required
@api_csrf_required
def api_sync_investments():
    with database() as db:
        items = [dict(row) for row in db.execute("SELECT item_id,institution FROM items ORDER BY created_at").fetchall()]
    total = 0
    errors = []
    for item in items:
        try:
            total += sync_item_investments(item["item_id"], item["institution"])
        except ExternalServiceError as exc:
            errors.append(str(exc))
    if errors and not total:
        return jsonify(error=errors[0]), 502
    return jsonify(ok=True, imported=total, errors=errors)


@app.post("/api/investments")
@login_required
@api_csrf_required
def api_add_investment():
    body=json_body()
    name=str(body.get("name","")).strip()[:120]
    kind=str(body.get("kind","")).strip()[:80]
    institution=str(body.get("institution","")).strip()[:120]
    notes=str(body.get("notes","")).strip()[:500]
    updated_on=str(body.get("updated_on") or local_now().date().isoformat())
    try:
        amount=round(float(body.get("amount")),2)
        date.fromisoformat(updated_on)
    except (TypeError,ValueError):
        return jsonify(error="Confira o valor e a data do investimento."),400
    if not name or not kind or amount<0:
        return jsonify(error="Informe nome, tipo e um valor igual ou maior que zero."),400
    with database() as db:
        db.execute("INSERT INTO investments(name,kind,institution,amount,updated_on,notes,created_at) VALUES(?,?,?,?,?,?,?)",
                   (name,kind,institution,amount,updated_on,notes,iso_now()))
    return jsonify(ok=True)


@app.patch("/api/investments/<int:investment_id>")
@login_required
@api_csrf_required
def api_edit_investment(investment_id: int):
    body=json_body()
    name=str(body.get("name","")).strip()[:120]
    kind=str(body.get("kind","")).strip()[:80]
    institution=str(body.get("institution","")).strip()[:120]
    notes=str(body.get("notes","")).strip()[:500]
    updated_on=str(body.get("updated_on") or local_now().date().isoformat())
    try:
        amount=round(float(body.get("amount")),2); date.fromisoformat(updated_on)
    except (TypeError,ValueError):
        return jsonify(error="Confira o valor e a data do investimento."),400
    if not name or not kind or amount<0:
        return jsonify(error="Informe nome, tipo e um valor igual ou maior que zero."),400
    with database() as db:
        cur=db.execute("UPDATE investments SET name=?,kind=?,institution=?,amount=?,updated_on=?,notes=? WHERE id=? AND source='manual'",
                       (name,kind,institution,amount,updated_on,notes,investment_id))
        if not cur.rowcount:
            return jsonify(error="Investimento manual não encontrado."),404
    return jsonify(ok=True)


@app.delete("/api/investments/<int:investment_id>")
@login_required
@api_csrf_required
def api_delete_investment(investment_id: int):
    with database() as db:
        db.execute("DELETE FROM investments WHERE id=? AND source='manual'",(investment_id,))
    return jsonify(ok=True)


@app.post("/api/whatsapp/connect")
@login_required
@api_csrf_required
def api_whatsapp_connect():
    base=get_setting("whatsapp_base_url",os.environ.get("WPP_BASE_URL","http://127.0.0.1:21465")).strip().rstrip("/")
    session=get_setting("whatsapp_session","financas").strip()
    secret=get_secret("whatsapp_secret")
    if not base or not session or not secret:
        return jsonify(error="Salve o endereço, o nome da sessão e a Secret Key do WPPConnect em Configurações."),400
    try:
        token_data=_request_json(f"{base}/api/{urllib.parse.quote(session,safe='')}/{urllib.parse.quote(secret,safe='')}/generate-token","POST")
        # WPPConnect expects the bcrypt token in Authorization. The `full` value
        # includes the session prefix and is intended for path based auth.
        token=token_data.get("token") or token_data.get("full") or ""
        if not token:
            raise ExternalServiceError("WPPConnect não retornou um token de sessão.")
        set_secret("whatsapp_token",token)
        result=_request_json(f"{base}/api/{urllib.parse.quote(session,safe='')}/start-session","POST",
                             {"Authorization":"Bearer "+token}, {"webhook":"","waitQrCode":True}, timeout=60)
        qr=result.get("qrcode") or result.get("qrCode")
        if isinstance(qr,dict):
            qr=qr.get("base64") or qr.get("url") or qr.get("code")
        return jsonify(ok=True,status=result.get("status","INITIALIZING"),qrcode=qr)
    except ExternalServiceError as exc:
        return jsonify(error=str(exc)),502


@app.get("/api/whatsapp/status")
@login_required
def api_whatsapp_status():
    session=get_setting("whatsapp_session","financas").strip()
    if not get_secret("whatsapp_token"):
        return jsonify(connected=False,status="NOT_CONFIGURED",qrcode="")
    try:
        data=whatsapp_request(f"/api/{urllib.parse.quote(session,safe='')}/check-connection-session")
        raw_status=data.get("status") or data.get("state") or "DISCONNECTED"
        status="CONNECTED" if raw_status is True else ("DISCONNECTED" if raw_status is False else str(raw_status))
        connected=status.upper() in {"CONNECTED","ISLOGGED","INCHAT","QRREADSUCCESS"}
        qr=""
        if not connected:
            qr=whatsapp_qrcode()
        return jsonify(connected=connected,status=status,qrcode=qr)
    except ExternalServiceError as exc:
        return jsonify(connected=False,status="ERROR",error=str(exc),qrcode="")


@app.post("/api/reports/send")
@login_required
@api_csrf_required
def api_send_report():
    body=json_body()
    kind=body.get("kind","daily")
    if kind not in ("daily","weekly"):
        return jsonify(error="Tipo de relatório inválido."),400
    if kind == "weekly":
        job_id = start_weekly_report_job()
        return jsonify(ok=True, job_id=job_id, status="running"), 202
    try:
        result=deliver_report(kind,run_ai=False)
        return jsonify(ok=True,message=result["message"])
    except ExternalServiceError as exc:
        return jsonify(error=str(exc)),502


@app.get("/api/reports/jobs/<job_id>")
@login_required
def api_report_job(job_id: str):
    with _report_jobs_lock:
        job = dict(_report_jobs[job_id]) if job_id in _report_jobs else None
    if not job:
        return jsonify(error="A análise semanal não está mais disponível. Atualize os relatórios."),404
    return jsonify(job)


@app.get("/api/reports")
@login_required
def api_reports():
    return jsonify(reports=records("SELECT * FROM reports ORDER BY created_at DESC LIMIT 20"))


@app.get("/health")
def health():
    return jsonify(status="ok")


init_db()
if not os.environ.get("DISABLE_SCHEDULER"):
    threading.Thread(target=scheduler_loop,name="finance-scheduler",daemon=True).start()

if __name__ == "__main__":
    host=os.environ.get("APP_HOST","127.0.0.1")
    port=int(os.environ.get("APP_PORT","8000"))
    if os.environ.get("APP_ENV") == "production":
        from waitress import serve
        serve(app,host=host,port=port,threads=8)
    else:
        app.run(host=host,port=port,debug=False,threaded=True)
