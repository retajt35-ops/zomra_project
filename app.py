# -*- coding: utf-8 -*-
"""
ZOMRA_PROJECT - Flask Chatbot (Blood Donation Assistant)
نسخة معدّلة:
- إصلاح إرسال التذكير عبر الإيميل مع كشف جاهزية SMTP
- تحسين نموذج الأهلية وإرجاع أسباب واضحة + تاريخ مقترح
- تحسين الرد الافتراضي وصياغته
- /health تعرض smtp_ready
"""

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import os
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv
import re
import unicodedata
from fuzzywuzzy import process, fuzz
from langdetect import detect, LangDetectException
import smtplib
from email.mime.text import MIMEText
import json
import csv
from io import StringIO
import requests

# ===================== بيئة التشغيل =====================
load_dotenv(override=True)

OPENAI_API_KEY    = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL      = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
FORCE_AI_FALLBACK = (os.getenv("FORCE_AI_FALLBACK") or "false").lower() in {"1","true","yes"}

SMTP_HOST  = (os.getenv("SMTP_HOST") or "").strip()
SMTP_PORT  = int(os.getenv("SMTP_PORT") or 587)
SMTP_USER  = (os.getenv("SMTP_USER") or "").strip()
SMTP_PASS  = (os.getenv("SMTP_PASS") or "").strip()
SMTP_FROM  = (os.getenv("SMTP_FROM") or SMTP_USER or "").strip()

URGENT_SHEET_URL    = (os.getenv("URGENT_NEEDS_SHEET_CSV") or "").strip()
URGENT_JSON_PATH    = (os.getenv("URGENT_NEEDS_JSON") or "urgent_needs.json").strip()
CAMPAIGNS_JSON_PATH = (os.getenv("CAMPAIGNS_JSON") or "campaigns.json").strip()

# ===================== Flask/DB =====================
app = Flask(__name__, template_folder="templates")
CORS(app)
DB_NAME = "chat_logs.db"

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA synchronous=NORMAL;")
    c.execute("""
      CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT,
        raw_query TEXT,
        corrected_query TEXT,
        response_type TEXT,
        kb_source TEXT,
        bot_response TEXT
      )
    """)
    c.execute("""
      CREATE TABLE IF NOT EXISTS reminders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT,
        user_hint TEXT,
        next_date TEXT,
        email TEXT
      )
    """)
    conn.commit()
    conn.close()

def save_log(raw_query, corrected_query, response_type, kb_source, bot_response):
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        t = (bot_response or "")[:500]
        if bot_response and len(bot_response) > 500:
            t += "..."
        c.execute("""INSERT INTO logs (timestamp, raw_query, corrected_query, response_type, kb_source, bot_response)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   raw_query, corrected_query, response_type, kb_source, t))
        conn.commit()
    except Exception as e:
        print("log error:", e)
    finally:
        try: conn.close()
        except: pass

# ===================== أدوات العربية =====================
_ARABIC_DIACRITICS_RE = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u0653-\u065F\u06D6-\u06ED]")

def normalize_arabic(text: str) -> str:
    if not text: return ""
    t = _ARABIC_DIACRITICS_RE.sub("", text)
    t = t.replace("أ","ا").replace("إ","ا").replace("آ","ا")
    t = t.replace("ؤ","و").replace("ئ","ي").replace("ة","ه")
    t = t.replace("ـ","")
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"\s+"," ", t).strip()
    return t

def summarize_and_simplify(text, max_length=250):
    if not text or len(text) <= max_length:
        return text
    cut_marks = ['.', '؟', '!', '…', '\n']
    trunc = text[: max_length - 5]
    cut_pos = max(trunc.rfind(m) for m in cut_marks)
    if cut_pos == -1:
        cut_pos = trunc.rfind(' ')
        if cut_pos == -1:
            cut_pos = len(trunc)
    summary = text[:cut_pos].strip()
    return f"{summary}...\n\nهل ترغب بالتفصيل أكثر؟"

# ===================== قاعدة معرفية بسيطة =====================
KNOWLEDGE_BASE = {
    "ما هي شروط التبرع بالدم؟": {
        "answer": "يشترط أن يكون العمر 18-60 سنة والوزن ≥ 50 كجم وأن تكون بصحة جيدة. تُؤجَّل بعض الحالات مثل: عدوى نشطة، استخدام مميعات، وشم/ثقب خلال 6 أشهر، إجراء جراحي حديث. يُفضّل التأكد من المستشفى.",
        "source": "KB"
    },
    "المدة الفاصلة بين التبرعات؟": {
        "answer": "المدة الفاصلة للتبرع بالدم الكامل 90 يومًا (3 أشهر).",
        "source": "KB"
    },
    "هل التبرع بالدم مؤلم؟": {
        "answer": "وخز الإبرة فقط هو الجزء الذي قد يسبّب ألمًا خفيفًا وسريعًا؛ السحب بحد ذاته يستغرق دقائق.",
        "source": "KB"
    },
}

def search_knowledge_base(corrected_query):
    if not corrected_query:
        return None, None
    normalized_query = normalize_arabic(corrected_query)
    kb_questions = list(KNOWLEDGE_BASE.keys())
    norm_map = {q: normalize_arabic(q) for q in kb_questions}
    norm_values = list(norm_map.values())

    best_partial = process.extractOne(normalized_query, norm_values, scorer=fuzz.partial_ratio)
    if best_partial and best_partial[1] >= 85:
        matched = best_partial[0]
        original_q = next((k for k, v in norm_map.items() if v == matched), None)
        if original_q:
            data = KNOWLEDGE_BASE[original_q]
            return data["answer"], data["source"]

    best_token = process.extractOne(normalized_query, norm_values, scorer=fuzz.token_sort_ratio)
    if best_token and best_token[1] >= 80:
        matched = best_token[0]
        original_q = next((k for k, v in norm_map.items() if v == matched), None)
        if original_q:
            data = KNOWLEDGE_BASE[original_q]
            return data["answer"], data["source"]

    return None, None

# ===================== صفحات/صحة =====================
@app.route("/")
def index():
    return render_template("index.html")

def smtp_ready():
    return all([SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM])

@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "smtp_ready": smtp_ready(),
        "openai": bool(OPENAI_API_KEY),
        "model": OPENAI_MODEL
    })

# ===================== شات =====================
@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    user_message = (data.get("message") or "").strip()
    want_detail = bool(data.get("detail"))

    if not user_message:
        return jsonify({"answer":"الرجاء كتابة سؤالك."}), 200

    try:
        lang = detect(user_message)
    except LangDetectException:
        lang = "ar"

    corrected = user_message  # إبقاءها بسيطة

    answer, source_text = search_knowledge_base(corrected)
    if answer:
        final = answer if want_detail else summarize_and_simplify(answer, 250)
        save_log(user_message, corrected, "KB", source_text, final)
        return jsonify({"answer": final, "corrected_message": corrected}), 200

    # رد افتراضي واضح بدون عبارات مزعجة
    fallback = (
        "لم أجد إجابة في القاعدة المعرفية. يمكنك سؤالاً آخر مثل:\n"
        "• شروط الأهلية\n• المدة الفاصلة بين التبرعات\n• أقرب مركز تبرع\n"
        "وسأحاول مساعدتك بأفضل شكل."
    )
    final = fallback if want_detail else summarize_and_simplify(fallback, 250)
    save_log(user_message, corrected, "KB-Only", None, final)
    return jsonify({"answer": final, "corrected_message": corrected}), 200

# ===================== الاحتياج العاجل =====================
def _fetch_csv(url: str):
    try:
        r = requests.get(url, timeout=6)
        r.raise_for_status()
        reader = csv.DictReader(StringIO(r.text))
        return [dict(row) for row in reader]
    except Exception as e:
        print("sheet error:", e)
        return None

def _load_json(path: str):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print("json error:", e)
    return None

def _format_urgent_rows(rows):
    out=[]
    for r in rows or []:
        hospital = r.get("hospital") or r.get("Hospital") or r.get("المستشفى") or ""
        status   = r.get("status") or r.get("Status") or r.get("الحالة") or ""
        details  = r.get("details") or r.get("Details") or r.get("التفاصيل") or ""
        loc      = r.get("location_url") or r.get("Location") or r.get("الموقع") or ""
        if hospital:
            out.append({"hospital":hospital,"status":status,"details":details,"location_url":loc})
    return out

FALLBACK_URGENT = [
    {"hospital":"مستشفى الملك فهد العام بجدة","status":"عاجل","details":"+O مطلوب","location_url":"https://www.google.com/maps/search/?api=1&query=King%20Fahd%20General%20Hospital%20Jeddah"},
    {"hospital":"بنك الدم الإقليمي – جدة","status":"مرتفع جداً","details":"نقص صفائح B-","location_url":"https://www.google.com/maps/search/?api=1&query=Jeddah%20Regional%20Laboratory%20and%20Blood%20Bank"},
]

@app.route("/api/urgent_needs", methods=["GET"])
def urgent_needs():
    needs_rows = None
    if URGENT_SHEET_URL:
        rows = _fetch_csv(URGENT_SHEET_URL)
        if rows:
            needs_rows = _format_urgent_rows(rows)
    if not needs_rows:
        js = _load_json(URGENT_JSON_PATH)
        if isinstance(js, list):
            needs_rows = _format_urgent_rows(js)
    if not needs_rows:
        needs_rows = FALLBACK_URGENT
    return jsonify({
        "answer_ar":"قائمة الاحتياجات العاجلة من مصادر موثوقة — يرجى الاتصال قبل الزيارة.",
        "needs": needs_rows,
        "updated_at": datetime.utcnow().isoformat()+"Z"
    }), 200

# ===================== الأهلية =====================
def evaluate_eligibility(payload: dict):
    reasons = []
    eligible = True
    next_date = None

    age         = int(payload.get("age", 0) or 0)
    weight      = int(payload.get("weight", 0) or 0)
    last_days   = int(payload.get("last_donation_days", 9999) or 9999)
    on_ac       = bool(payload.get("on_anticoagulants", False))
    on_ab       = bool(payload.get("on_antibiotics", False))
    has_cold    = bool(payload.get("has_cold", False))
    pregnant    = bool(payload.get("pregnant", False))
    recent_proc = int(payload.get("recent_procedure_days", 9999) or 9999)
    tattoo_m    = int(payload.get("tattoo_months", 999) or 999)

    if age < 18:      eligible = False; reasons.append("العمر أقل من 18 سنة.")
    if weight < 50:   eligible = False; reasons.append("الوزن أقل من 50 كجم.")
    if last_days < 90:
        eligible = False
        days_left = 90 - last_days
        next_date = (datetime.now() + timedelta(days=days_left)).strftime("%Y-%m-%d")
        reasons.append(f"لم يمض 90 يومًا منذ آخر تبرع. متاح بعد {days_left} يومًا ({next_date}).")
    if on_ac:         eligible = False; reasons.append("أدوية السيولة تمنع التبرع حاليًا.")
    if on_ab:         eligible = False; reasons.append("أجّل التبرع 7 أيام بعد آخر جرعة مضاد حيوي.")
    if has_cold:      eligible = False; reasons.append("أعراض زكام/حمى: أجّل حتى التعافي.")
    if pregnant:      eligible = False; reasons.append("الحمل يمنع التبرع. يُستأنف بعد 6 أسابيع من الولادة/الإجهاض.")
    if recent_proc < 7: eligible=False; reasons.append("إجراء/قلع أسنان حديث: انتظر 7 أيام على الأقل.")
    if tattoo_m   < 6: eligible=False; reasons.append("وشم/ثقب خلال آخر 6 أشهر: يؤجل التبرع.")

    if not next_date:
        next_date = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")
    return eligible, reasons, next_date

@app.route("/api/eligibility/evaluate", methods=["POST"])
def eligibility_evaluate():
    payload = request.json or {}
    ok, reasons, next_date = evaluate_eligibility(payload)
    return jsonify({"eligible": ok, "reasons": reasons, "next_eligible_date": next_date})

# ===================== تذكير (سجل + بريد) =====================
BASE_WAIT_DAYS = 90

def send_email(to_email: str, subject: str, body: str) -> tuple[bool, str]:
    if not smtp_ready():
        return False, "SMTP غير مفعّل"
    try:
        msg = MIMEText(body, _charset="utf-8")
        msg["Subject"] = subject
        msg["From"] = SMTP_FROM
        msg["To"] = to_email

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=12) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_FROM, [to_email], msg.as_string())
        return True, ""
    except Exception as e:
        return False, str(e)

@app.route("/api/reminder", methods=["POST"])
def reminder():
    payload = request.json or {}
    user_hint = (payload.get("user_hint") or "User").strip() or "User"
    next_date = (datetime.now() + timedelta(days=BASE_WAIT_DAYS)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("INSERT INTO reminders (created_at, user_hint, next_date, email) VALUES (?, ?, ?, ?)",
              (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_hint, next_date, None))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "next_date": next_date})

@app.route("/api/reminder/email", methods=["POST"])
def reminder_email():
    data = request.json or {}
    to_email = (data.get("email") or "").strip()
    next_date = (data.get("next_date") or "").strip()
    if not to_email:
        return jsonify({"ok": False, "error": "البريد مطلوب."}), 400
    if not next_date:
        next_date = (datetime.now() + timedelta(days=BASE_WAIT_DAYS)).strftime("%Y-%m-%d")

    subject = "تذكير زمرة بموعد التبرع بالدم"
    body = f"مرحبًا!\n\nهذا تذكير من زمرة بموعد تبرعك المقترح بتاريخ {next_date}.\nنتمنّى لك صحة دائمة.\n\nفريق زمرة"

    ok, err = send_email(to_email, subject, body)
    if ok:
        # تخزين البريد مع التذكير الأحدث
        conn = sqlite3.connect(DB_NAME); c = conn.cursor()
        c.execute("UPDATE reminders SET email=? WHERE id=(SELECT MAX(id) FROM reminders)", (to_email,))
        conn.commit(); conn.close()
        return jsonify({"ok": True})
    else:
        return jsonify({"ok": False, "error": err}), 200

# ===================== حملات (اختياري) =====================
@app.route("/api/campaigns", methods=["GET"])
def campaigns():
    js = _load_json(CAMPAIGNS_JSON_PATH)
    if not js:
        return jsonify({"ok": False, "campaigns": [], "message": "لا يوجد ملف حملات"}), 200
    return jsonify({"ok": True, "campaigns": js}), 200

# ===================== Run =====================
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
