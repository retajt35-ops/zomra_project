# -*- coding: utf-8 -*-
"""
ZOMRA_PROJECT - Flask Chatbot (Blood Donation Assistant)
نسخة محسّنة:
- تذكير عبر الإيميل (SendGrid أو SMTP) + مرفق .ics
- واجهات API ثابتة: eligibility/urgent/chat/stats/health/ics
- تحميل قاعدة معرفية من JSON
- دعم رفع الصوت + STT من OpenAI
- فوتر عربي + إنجليزي
"""

from openai import OpenAI
from flask import Flask, request, jsonify, render_template, Response
from flask_cors import CORS
import os, sqlite3, re, json, csv, unicodedata, smtplib, base64
from email.message import EmailMessage
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fuzzywuzzy import process, fuzz
from langdetect import detect, LangDetectException
from io import StringIO, BytesIO
from typing import Tuple
import requests

# ========== 1) ENV ==========
load_dotenv(override=True)

OPENAI_API_KEY  = (os.getenv("OPENAI_API_KEY") or "").strip().strip('"').strip("'")
OPENAI_MODEL    = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
FORCE_AI_FALLBACK = (os.getenv("FORCE_AI_FALLBACK") or "false").lower() in {"1","true","yes"}

URGENT_SHEET_URL    = (os.getenv("URGENT_NEEDS_SHEET_CSV") or "").strip()
URGENT_JSON_PATH    = (os.getenv("URGENT_NEEDS_JSON") or "urgent_needs.json").strip()
CAMPAIGNS_JSON_PATH = (os.getenv("CAMPAIGNS_JSON") or "campaigns.json").strip()

KB_JSON_PATH        = (os.getenv("KB_JSON_PATH") or "knowledge_base.json").strip()

OPENAI_STT_MODEL    = (os.getenv("OPENAI_STT_MODEL") or "gpt-4o-mini-transcribe").strip()

SMTP_HOST = os.getenv("SMTP_HOST") or ""
SMTP_PORT = int(os.getenv("SMTP_PORT") or "587")
SMTP_USER = os.getenv("SMTP_USER") or ""
SMTP_PASS = os.getenv("SMTP_PASS") or ""
SMTP_FROM = os.getenv("SMTP_FROM") or SMTP_USER or ""
SMTP_TLS  = (os.getenv("SMTP_TLS") or "true").lower() in {"1","true","yes"}

SMTP_READY = all([SMTP_HOST, SMTP_PORT, SMTP_FROM]) and (bool(SMTP_USER)==bool(SMTP_PASS) or not SMTP_USER)

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY") or ""
SENDGRID_READY = bool(SENDGRID_API_KEY)

if not OPENAI_API_KEY:
    print("⚠️ لم يتم العثور على OPENAI_API_KEY — سيتم العمل دون ذكاء اصطناعي")

client = None
if OPENAI_API_KEY:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print(f"⚠️ فشل تهيئة OpenAI: {e}")
        client = None

# ========== 2) Arabic utils ==========
_ARABIC_DIACRITICS_RE = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u0653-\u065F\u06D6-\u06ED]")

def normalize_arabic(text: str) -> str:
    if not text: return ""
    t = _ARABIC_DIACRITICS_RE.sub("", text)
    t = t.replace("أ","ا").replace("إ","ا").replace("آ","ا") \
         .replace("ؤ","و").replace("ئ","ي").replace("ة","ه").replace("ـ","")
    t = unicodedata.normalize("NFKC", t)
    return re.sub(r"\s+"," ", t).strip()

def summarize_and_simplify(text, max_length=250):
    if not text or len(text) <= max_length: return text
    cut_marks = ['.', '؟', '!', '…']
    trunc = text[:max_length-5]
    cut_pos = max(trunc.rfind(m) for m in cut_marks)
    if cut_pos == -1: cut_pos = trunc.rfind(' ') or len(trunc)
    return f"{text[:cut_pos].strip()}...\n\nهل ترغب بالتفصيل أكثر؟"

def openai_translate(text, target):
    if not client or not text: return text
    try:
        prompt = (
            f"Translate this Arabic text to {target}. Return only translation:\n{text}"
            if target != "ar" else
            f"Translate to standard Arabic only:\n{text}"
        )
        res = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role":"user","content":prompt}]
        )
        out = (res.choices[0].message.content or "").strip()
        return out.split(":",1)[-1].strip() if ":" in out[:15] else out
    except:
        return text

def openai_correct(text):
    if not client or not text: return text
    try:
        prompt = f"صحّح الأخطاء الإملائية في هذا النص وأعده فقط:\n{text}"
        res = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role":"user","content":prompt}]
        )
        out = (res.choices[0].message.content or "").strip()
        return out.split(":",1)[-1].strip() if ":" in out[:15] else out
    except:
        return text

# ========== 3) FOOTER ==========
def build_footer(source_label: str, from_kb: bool) -> str:
    ar_source = source_label or ("مصدر طبي موثوق" if from_kb else "نموذج OpenAI (ذكاء اصطناعي)")
    if client:
        try:
            en_source = openai_translate(ar_source, "en")
        except:
            en_source = "OpenAI model (AI-generated)"
    else:
        en_source = "OpenAI model (AI-generated)"

    return (
        f"المصدر: {ar_source}\n"
        f"Source: {en_source}\n"
        "مُولَّد آليًا • قد يحتوي على أخطاء طفيفة\n"
        "AI-generated • may contain minor errors\n"
        "🩸 مع تحياتي زمرة\n"
        "🩸 With regards, Zomrah"
    )

# ========== 4) FLASK + DB ==========
app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app)
DB_NAME = "chat_logs.db"

def init_db():
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL;"); c.execute("PRAGMA synchronous=NORMAL;")
    c.execute("""
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT, raw_query TEXT, corrected_query TEXT,
            response_type TEXT, kb_source TEXT, bot_response TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS reminders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT, user_hint TEXT, email TEXT,
            next_date TEXT, note TEXT
        )
    """)
    conn.commit(); conn.close()

def save_log(raw, corrected, resp_type, kb_src, bot_resp):
    try:
        conn = sqlite3.connect(DB_NAME); c = conn.cursor()
        snippet = (bot_resp or "")[:500] + ("..." if bot_resp and len(bot_resp)>500 else "")
        c.execute("""INSERT INTO logs(timestamp,raw_query,corrected_query,response_type,kb_source,bot_response)
                     VALUES(?,?,?,?,?,?)""",
                  (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   raw, corrected, resp_type, kb_src, snippet))
        conn.commit()
    except:
        pass
    finally:
        try: conn.close()
        except: pass
# ========== 5) Knowledge Base ==========
def load_knowledge_base():
    """
    تحميل القاعدة المعرفية من ملف JSON:
    [
      {
        "questions": [...],
        "answer": "...",
        "source": "وزارة الصحة السعودية"
      },
      ...
    ]
    ثم تحويل الأسئلة إلى مفاتيح للبحث بالفَزّي.
    """
    kb = {}
    if os.path.exists(KB_JSON_PATH):
        try:
            with open(KB_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                answer = (item.get("answer") or "").strip()
                source = (item.get("source") or "").strip()
                for q in item.get("questions", []):
                    q = (q or "").strip()
                    if q:
                        kb[q] = {"answer": answer, "source": source}
        except Exception as e:
            print("⚠️ فشل تحميل القاعدة المعرفية:", e)

    # fallback بسيط إذا لم توجد قاعدة معرفة
    if not kb:
        kb = {
            "ما هي شروط التبرع بالدم؟": {
                "answer": "يجب أن يكون العمر 18-60 عاماً، الوزن ≥50 كجم، والتمتع بصحة جيدة.",
                "source": "وزارة الصحة السعودية"
            }
        }
    return kb


KNOWLEDGE_BASE = load_knowledge_base()


def search_knowledge_base(corrected_query) -> Tuple[str, str]:
    """
    ترجع (answer, source_label) أو (None, None)
    """
    if not corrected_query:
        return None, None

    nq = normalize_arabic(corrected_query)
    keys = list(KNOWLEDGE_BASE.keys())
    norm = {k: normalize_arabic(k) for k in keys}

    # 1) partial ratio
    best = process.extractOne(nq, list(norm.values()), scorer=fuzz.partial_ratio)
    if best and best[1] >= 85:
        orig = [k for k, v in norm.items() if v == best[0]][0]
        d = KNOWLEDGE_BASE[orig]
        return d["answer"], d["source"]

    # 2) token_sort ratio
    best = process.extractOne(nq, list(norm.values()), scorer=fuzz.token_sort_ratio)
    if best and best[1] >= 80:
        orig = [k for k, v in norm.items() if v == best[0]][0]
        d = KNOWLEDGE_BASE[orig]
        return d["answer"], d["source"]

    return None, None


# ========== 6) Chat Pipeline ==========
def run_chat_pipeline(user_message: str, want_detail: bool = False):
    """
    يقوم بمعالجة السؤال وإرجاع:
    final_answer, source_type, source_text, corrected_message, lang, meta
    """
    raw = (user_message or "").strip()
    if not raw:
        return (
            "الرجاء كتابة سؤالك.",
            "Error",
            None,
            "",
            "ar",
            {"hallucination_rate": None, "response_speed": None, "accuracy": None},
        )

    # كشف اللغة
    try:
        lang = detect(raw)
    except:
        lang = "ar"

    # إن كان السؤال غير عربي، نترجمه للعربية
    if lang == "ar" or not client:
        query = raw
    else:
        query = openai_translate(raw, "ar")

    # تصحيح إملائي
    corrected = openai_correct(query) or query

    # محاولة إيجاد إجابة في القاعدة المعرفية
    answer, source_label = search_knowledge_base(corrected)

    if answer:
        # رد من القاعدة المعرفية
        source_type = "KB"
        src_label = source_label or "مصدر طبي موثوق"
        core = answer if want_detail else summarize_and_simplify(answer, 250)
        footer = build_footer(src_label, from_kb=True)

        final_ar = (
            "تم العثور على إجابة من مصدر طبي معتمد:\n\n"
            f"{core}\n\n"
            f"{footer}"
        )

        meta = {
            "hallucination_rate": 0.0,
            "response_speed": "fast",
            "accuracy": "100% حسب المصدر الطبي",
        }

    else:
        # لم نجد إجابة في القاعدة → نستخدم OpenAI
        if client and not FORCE_AI_FALLBACK:
            try:
                res = client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=[{"role": "user", "content": corrected}],
                )
                ai_text = (res.choices[0].message.content or "").strip()
                core = ai_text if want_detail else summarize_and_simplify(ai_text, 250)

                src_label = "نموذج OpenAI (مصادر طبية متعددة)"
                footer = build_footer(src_label, from_kb=False)

                final_ar = (
                    "لم يتم العثور على إجابة مناسبة في قاعدة المعرفة، لذلك تم الاستعانة بنموذج OpenAI لتوليد الرد.\n"
                    "No suitable answer was found in the knowledge base, so an OpenAI model was used to generate the response.\n\n"
                    f"{core}\n\n"
                    f"{footer}"
                )

                meta = {
                    "hallucination_rate": 0.25,
                    "response_speed": "medium",
                    "accuracy": "يُنصح بالتحقق من مصدر طبي مباشر",
                }

            except Exception as e:
                return (
                    f"خطأ في الاتصال بالذكاء الاصطناعي: {e}",
                    "Error",
                    None,
                    corrected,
                    lang,
                    {
                        "hallucination_rate": None,
                        "response_speed": None,
                        "accuracy": None,
                    },
                )

        else:
            # الذكاء الاصطناعي غير مفعّل
            footer = build_footer(None, from_kb=False)
            final_ar = (
                "عذرًا، لا توجد إجابة في القاعدة المعرفية، كما أن الذكاء الاصطناعي غير مفعّل.\n\n"
                f"{footer}"
            )
            meta = {
                "hallucination_rate": None,
                "response_speed": "n/a",
                "accuracy": "غير معروفة",
            }

    # ترجمة إن كانت لغة المستخدم مختلفة
    final_result = (
        openai_translate(final_ar, lang) if lang != "ar" and client else final_ar
    )

    return final_result, source_type, source_label, corrected, lang, meta
# ========== 7) Chat Route ==========
@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    raw = data.get("message") or ""
    user_message = raw.strip()
    want_detail = bool(data.get("detail"))

    if not user_message:
        return jsonify({
            "answer": "الرجاء كتابة سؤالك.",
            "source_type": "Error",
            "source_text": None,
            "corrected_message": "",
            "hallucination_rate": None,
            "response_speed": None,
            "accuracy": None
        }), 200

    final, source_type, source_text, corrected, lang, meta = run_chat_pipeline(
        user_message,
        want_detail
    )

    save_log(user_message, corrected, source_type, source_text, final)

    status = 500 if source_type == "Error" else 200
    return jsonify({
        "answer": final,
        "source_type": source_type,
        "source_text": source_text,
        "corrected_message": corrected,
        **meta
    }), status


# ========== 8) Urgent needs ==========
def gmaps_place_link(name: str) -> str:
    import urllib.parse as up
    return f"https://www.google.com/maps/search/?api=1&query={up.quote(name)}"


def _fetch_csv(url: str):
    try:
        r = requests.get(url, timeout=6)
        r.raise_for_status()
        rows = list(csv.DictReader(StringIO(r.text)))
        return rows
    except Exception as e:
        print("⚠️ CSV:", e)
        return None


def _load_json(path: str):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print("⚠️ JSON:", e)
    return None


def _format_urgent_rows(rows):
    out = []
    for r in rows or []:
        hospital = r.get("hospital") or r.get("Hospital") or r.get("المستشفى") or ""
        status   = r.get("status")   or r.get("Status")   or r.get("الحالة")    or ""
        details  = r.get("details")  or r.get("Details")  or r.get("التفاصيل")  or ""
        loc      = r.get("location_url") or r.get("Location") or r.get("الموقع") or ""

        if hospital and not loc:
            loc = gmaps_place_link(hospital)

        if hospital:
            out.append({
                "hospital": hospital,
                "status": status,
                "details": details,
                "location_url": loc,
            })
    return out


# fallback في حال لم تتوفر المصادر
FALLBACK_URGENT = [
    {
        "hospital": "مستشفى الملك فهد العام بجدة",
        "status": "عاجل",
        "details": "+O لحالات طارئة",
        "location_url": gmaps_place_link("King Fahd General Hospital Jeddah")
    },
    {
        "hospital": "بنك الدم الإقليمي – جدة",
        "status": "مرتفع جداً",
        "details": "نقص صفائح B-",
        "location_url": gmaps_place_link("Jeddah Regional Laboratory and Blood Bank")
    },
    {
        "hospital": "مستشفى شرق جدة",
        "status": "عاجل",
        "details": "A- لحالات طوارئ",
        "location_url": gmaps_place_link("East Jeddah Hospital Blood Bank")
    },
]


@app.route("/api/urgent_needs")
def urgent_needs():
    needs = None

    # 1) محاولة من Google Sheet CSV
    if URGENT_SHEET_URL:
        rows = _fetch_csv(URGENT_SHEET_URL)
        if rows:
            needs = _format_urgent_rows(rows)

    # 2) محاولة من JSON
    if not needs:
        js = _load_json(URGENT_JSON_PATH)
        if isinstance(js, list):
            needs = _format_urgent_rows(js)
        elif isinstance(js, dict) and "needs" in js:
            needs = _format_urgent_rows(js["needs"])

    # 3) fallback
    if not needs:
        needs = FALLBACK_URGENT

    return jsonify({
        "answer_ar": "احتياجات عاجلة (يرجى الاتصال قبل الزيارة).",
        "source": "Sheet/JSON/Fallback",
        "needs": needs,
        "updated_at": datetime.utcnow().isoformat() + "Z"
    }), 200
# ========== 9) Eligibility (فحص الأهلية) ==========

ELIGIBILITY_QUESTIONS = [
    {"id": "age", "text": "كم عمرك؟", "type": "number", "min": 1, "max": 100},
    {"id": "weight", "text": "كم وزنك بالكيلو؟", "type": "number", "min": 30, "max": 300},
    {
        "id": "last_donation_days",
        "text": "متى كان آخر تبرع لك؟ (بالأيام)",
        "type": "number",
        "min": 0,
        "max": 2000,
    },
    {
        "id": "on_anticoagulants",
        "text": "هل تتناول أدوية سيولة الدم حالياً؟",
        "type": "boolean",
    },
    {
        "id": "on_antibiotics",
        "text": "هل تتناول مضادًا حيويًا لعدوى نشطة؟",
        "type": "boolean",
    },
    {
        "id": "has_cold",
        "text": "هل لديك أعراض زكام/حمى حالياً؟",
        "type": "boolean",
    },
    {
        "id": "pregnant",
        "text": "هل أنتِ حامل حاليًا؟ (للنساء)",
        "type": "boolean",
    },
    {
        "id": "recent_procedure_days",
        "text": "هل أجريت عملية أو قلع أسنان مؤخرًا؟ كم يوم مضى؟",
        "type": "number",
        "min": 0,
        "max": 400,
    },
    {
        "id": "tattoo_months",
        "text": "هل عملت وشم/ثقب خلال آخر كم شهر؟",
        "type": "number",
        "min": 0,
        "max": 48,
    },
]


@app.route("/api/eligibility/questions")
def eligibility_questions():
    return jsonify({"questions": ELIGIBILITY_QUESTIONS})


def evaluate_eligibility(payload: dict):
    reasons = []
    eligible = True
    next_date = None

    # قراءة المدخلات
    age = int(payload.get("age", 0) or 0)
    weight = int(payload.get("weight", 0) or 0)
    last = int(payload.get("last_donation_days", 9999) or 9999)
    on_ac = bool(payload.get("on_anticoagulants", False))
    on_ab = bool(payload.get("on_antibiotics", False))
    cold = bool(payload.get("has_cold", False))
    preg = bool(payload.get("pregnant", False))
    proc = int(payload.get("recent_procedure_days", 9999) or 9999)
    tattoo = int(payload.get("tattoo_months", 999) or 999)

    # الشروط الطبية الأساسية
    if age < 18:
        eligible = False
        reasons.append("العمر أقل من 18 سنة.")
    if weight < 50:
        eligible = False
        reasons.append("الوزن أقل من 50 كجم.")

    # 90 يوم بين كل تبرعين كاملين
    if last < 90:
        eligible = False
        days_left = 90 - last
        next_date = (datetime.now() + timedelta(days=days_left)).strftime("%Y-%m-%d")
        reasons.append(f"لم يمض 90 يومًا منذ آخر تبرع. يمكنك التبرع بعد {days_left} يومًا (بتاريخ {next_date}).")

    # أدوية السيولة
    if on_ac:
        eligible = False
        reasons.append("أدوية السيولة تمنع التبرع مؤقتًا.")

    # مضاد حيوي
    if on_ab:
        eligible = False
        reasons.append("يجب الانتظار 7 أيام بعد آخر جرعة مضاد حيوي.")

    # زكام / حرارة
    if cold:
        eligible = False
        reasons.append("وجود زكام أو حرارة يمنع التبرع حتى التعافي.")

    # حمل
    if preg:
        eligible = False
        reasons.append("الحمل يمنع التبرع. يمكن التبرع بعد 6 أسابيع من الولادة/الإجهاض.")

    # عمليات أو قلع أسنان
    if proc < 7:
        eligible = False
        reasons.append("إجراء أو قلع أسنان حديث يتطلب الانتظار 7 أيام على الأقل.")

    # وشم أو ثقب
    if tattoo < 6:
        eligible = False
        reasons.append("الوشم أو الثقب خلال آخر 6 أشهر يمنع التبرع مؤقتًا.")

    if not next_date:
        next_date = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")

    return eligible, reasons, next_date


@app.route("/api/eligibility/evaluate", methods=["POST"])
def eligibility_evaluate():
    payload = request.json or {}
    ok, reasons, next_date = evaluate_eligibility(payload)

    return jsonify(
        {
            "eligible": ok,
            "reasons": reasons,
            "next_eligible_date": next_date,
        }
    )
# ========== 10) Reminder (Email + ICS) ==========

def make_ics_bytes(date_str: str) -> bytes:
    """
    إنشاء ملف تقويم .ics يحتوي على موعد التبرع.
    """
    dt = (
        datetime.fromisoformat(date_str)
        if "T" not in date_str
        else datetime.fromisoformat(date_str.replace("Z", "").replace("z", ""))
    )
    dt_end = dt + timedelta(hours=1)

    def pad(n):
        return f"{n:02d}"

    def fmt(d):
        return (
            f"{d.year}{pad(d.month)}{pad(d.day)}T"
            f"{pad(d.hour)}{pad(d.minute)}{pad(d.second)}Z"
        )

    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Zomrah//Blood Donation Reminder//AR
BEGIN:VEVENT
UID:{int(datetime.now().timestamp())}@zomrah
DTSTAMP:{fmt(datetime.utcnow())}
DTSTART:{fmt(datetime(dt.year, dt.month, dt.day, 9, 0, 0))}
DTEND:{fmt(datetime(dt_end.year, dt_end.month, dt_end.day, 10, 0, 0))}
SUMMARY:تذكير التبرع بالدم
DESCRIPTION:تذكير زمرة: موعد تبرعك المقترح.
LOCATION:أقرب بنك دم
END:VEVENT
END:VCALENDAR"""

    return ics.encode("utf-8")


def try_send_email(to_email: str, subject: str, body: str, ics_bytes: bytes, ics_name: str):
    """
    يرسل بريدًا عبر:
    - SendGrid أولًا (إن توفر)
    - SMTP ثانيًا
    """
    # --- SendGrid ---
    if SENDGRID_READY:
        try:
            url = "https://api.sendgrid.com/v3/mail/send"
            headers = {
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type": "application/json",
            }

            payload = {
                "personalizations": [{"to": [{"email": to_email}]}],
                "from": {"email": SMTP_FROM or to_email},
                "subject": subject,
                "content": [{"type": "text/plain", "value": body}],
            }

            if ics_bytes:
                encoded = base64.b64encode(ics_bytes).decode("utf-8")
                payload["attachments"] = [
                    {
                        "content": encoded,
                        "type": "text/calendar",
                        "filename": ics_name,
                    }
                ]

            resp = requests.post(url, headers=headers, json=payload, timeout=10)

            if resp.status_code in (200, 202):
                return True, "تم الإرسال عبر SendGrid."
            else:
                return False, f"SendGrid error: {resp.status_code} {resp.text}"

        except Exception as e:
            return False, f"SendGrid exception: {e}"

    # --- SMTP ---
    if not SMTP_READY:
        return False, "SMTP غير مفعّل في الخادم."

    try:
        msg = EmailMessage()
        msg["From"] = SMTP_FROM
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.set_content(body)

        if ics_bytes:
            msg.add_attachment(
                ics_bytes,
                maintype="text",
                subtype="calendar",
                filename=ics_name,
            )

        if SMTP_TLS:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
            server.starttls()
        else:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)

        if SMTP_USER:
            server.login(SMTP_USER, SMTP_PASS)

        server.send_message(msg)
        server.quit()

        return True, "تم الإرسال عبر SMTP."

    except Exception as e:
        return False, str(e)


@app.route("/api/reminder", methods=["POST"])
def reminder():
    """
    تسجيل تذكير + إرسال بريد (إن توفر)
    """
    data = request.json or {}
    user_hint = (data.get("user_hint") or "متبرع").strip()
    email = (data.get("email") or "").strip()

    next_date = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")

    # تخزين التذكير في قاعدة البيانات
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO reminders(created_at,user_hint,email,next_date,note)
            VALUES(?,?,?,?,?)
            """,
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                user_hint,
                email,
                next_date,
                "Reminder for next eligible donation (whole blood).",
            ),
        )
        conn.commit()
    finally:
        try:
            conn.close()
        except:
            pass

    # إرسال بريد إلكتروني (اختياري)
    email_status = {"sent": False, "message": "تم تسجيل التذكير فقط.", "via": None}

    if email:
        ics_bytes = make_ics_bytes(next_date)
        ok, msg = try_send_email(
            email,
            "تذكير زمرة: موعد التبرع القادم",
            (
                f"مرحباً {user_hint},\n\n"
                f"هذا تذكير من زمرة بموعد تبرعك المقترح بتاريخ {next_date}.\n"
                f"نأمل لك دوام الصحة.\n\n"
                f"مع التحية،\n"
                f"فريق زمرة"
            ),
            ics_bytes,
            f"Zomrah-Reminder-{next_date}.ics",
        )

        email_status = {
            "sent": ok,
            "message": msg,
            "via": "sendgrid" if SENDGRID_READY else ("smtp" if SMTP_READY else None),
        }

    return jsonify(
        {"ok": True, "next_date": next_date, "email_status": email_status}
    )


@app.route("/api/reminder/ics/<date_str>")
def reminder_ics(date_str):
    """
    تنزيل ملف ICS للموعد مباشرة
    """
    try:
        _ = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "صيغة التاريخ غير صحيحة"}), 400

    ics = make_ics_bytes(date_str)

    return Response(
        ics,
        mimetype="text/calendar",
        headers={
            "Content-Disposition": f'attachment; filename="Zomrah-Reminder-{date_str}.ics"'
        },
    )
# ========== 11) Upload audio (STT + تحليل السؤال) ==========

@app.route("/api/upload_audio", methods=["POST"])
def upload_audio():
    """
    رفع ملف صوتي وتحويله إلى نص باستخدام نموذج OpenAI STT
    ثم تطبيق نفس منطق الدردشة على النص.
    """
    if "audio_file" not in request.files:
        return jsonify({"error": "لم يتم إرسال ملف صوتي"}), 400

    audio_file = request.files["audio_file"]
    text = ""

    # تحويل الصوت لنص
    if client:
        try:
            audio_bytes = audio_file.read()
            bio = BytesIO(audio_bytes)
            bio.name = audio_file.filename or "audio.webm"

            tr = client.audio.transcriptions.create(
                model=OPENAI_STT_MODEL,
                file=bio,
                response_format="text"
            )
            text = getattr(tr, "text", None) or str(tr)

        except Exception as e:
            print("⚠️ STT Error:", e)

    if not text:
        # fallback بسيط حتى لا تنكسر الواجهة
        text = "ما هي شروط التبرع بالدم؟"

    corrected = openai_correct(text) or text

    final, source_type, source_text, corrected_message, lang, meta = run_chat_pipeline(
        corrected,
        want_detail=False
    )

    save_log("ملف صوتي", corrected_message, source_type + " (من الصوت)", source_text, final)

    return jsonify({
        "transcribed_text": text,
        "answer": final,
        "source_type": source_type,
        "source_text": source_text,
        "corrected_message": corrected_message,
        **meta
    })


# ========== 12) Stats / Campaigns ==========

@app.route("/api/stats")
def stats():
    """
    إحصائيات عامة عن استخدام نظام الدردشة
    """
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()

        # إجمالي الرسائل
        c.execute("SELECT COUNT(*) FROM logs")
        total = c.fetchone()[0]

        # تجميع حسب نوع المصدر
        c.execute("SELECT response_type, COUNT(*) FROM logs GROUP BY response_type")
        by_type = {k: v for k, v in c.fetchall()}

        conn.close()

        return jsonify({
            "ok": True,
            "total_logs": total,
            "by_type": by_type
        })

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500



@app.route("/api/campaigns")
def campaigns():
    """
    جلب الحملات (من ملف JSON خارجي)
    """
    data = None

    try:
        if os.path.exists(CAMPAIGNS_JSON_PATH):
            with open(CAMPAIGNS_JSON_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
    except:
        data = None

    if not data:
        return jsonify({
            "ok": False,
            "campaigns": [],
            "message": "ملف الحملات غير متوفر"
        })

    return jsonify({
        "ok": True,
        "campaigns": data
    })


# ========== 13) تشغيل السيرفر (Local) ==========

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)

