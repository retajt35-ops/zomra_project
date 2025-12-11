# -*- coding: utf-8 -*-
"""
ZOMRA_PROJECT - Flask Chatbot (Blood Donation Assistant)

النسخة المعدلة حسب طلب المستخدم:
- مصدر القاعدة: "القاعدة المعرفية" فقط.
- إذا لم نجد إجابة في القاعدة: رسالة موحدة مع OpenAI + فوتر باللغة المناسبة.
- تلخيص summarize_and_simplify يدعم عربي/إنجليزي.
- Intent "خدمة العملاء" يرسل رابط واتساب جاهز.
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
from io import StringIO
from typing import Tuple
import requests
import urllib.parse as up

# ==============================
# 1) ENV / Config
# ==============================
load_dotenv(override=True)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

OPENAI_API_KEY   = (os.getenv("OPENAI_API_KEY") or "").strip().strip('"').strip("'")
OPENAI_MODEL     = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
FORCE_AI_FALLBACK = (os.getenv("FORCE_AI_FALLBACK") or "false").lower() in {"1", "true", "yes"}

URGENT_SHEET_URL    = (os.getenv("URGENT_NEEDS_SHEET_CSV") or "").strip()
URGENT_JSON_PATH = "static/urgent_needs.json"
CAMPAIGNS_JSON_PATH = "static/campaigns.json"

SMTP_HOST = os.getenv("SMTP_HOST") or ""
SMTP_PORT = int(os.getenv("SMTP_PORT") or "587")
SMTP_USER = os.getenv("SMTP_USER") or ""
SMTP_PASS = os.getenv("SMTP_PASS") or ""
SMTP_FROM = os.getenv("SMTP_FROM") or ""
SMTP_TLS  = (os.getenv("SMTP_TLS") or "true").lower() in {"1", "true", "yes"}

SMTP_READY = all([SMTP_HOST, SMTP_PORT, SMTP_FROM]) and (bool(SMTP_USER) == bool(SMTP_PASS) or not SMTP_USER)

SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY") or ""
SENDGRID_FROM    = os.getenv("SENDGRID_FROM") or SMTP_FROM or ""
EMAIL_FROM_NAME  = os.getenv("EMAIL_FROM") or "Zomra Project"
SENDGRID_READY   = bool(SENDGRID_API_KEY)

if not OPENAI_API_KEY:
    print("⚠️ لم يتم العثور على OPENAI_API_KEY في .env. سيتم العمل دون ذكاء اصطناعي (وضع KB فقط).")

client = None
if OPENAI_API_KEY:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print(f"⚠️ فشل تهيئة OpenAI: {e}")
        client = None

# ==============================
# 2) Arabic / Text utils
# ==============================
_ARABIC_DIACRITICS_RE = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u0653-\u065F\u06D6-\u06ED]")

def normalize_arabic(text: str) -> str:
    """إزالة التشكيل وتوحيد بعض الحروف لتسهيل البحث بالتقريب."""
    if not text:
        return ""
    t = _ARABIC_DIACRITICS_RE.sub("", text)
    t = (
        t.replace("أ", "ا")
         .replace("إ", "ا")
         .replace("آ", "ا")
         .replace("ؤ", "و")
         .replace("ئ", "ي")
         .replace("ة", "ه")
         .replace("ـ", "")
    )
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def summarize_and_simplify(text: str, max_length: int = 250, lang: str = "ar") -> str:
    """تلخيص بسيط مع احترام الجمل (يدعم عربي وإنجليزي)."""
    if not text or len(text) <= max_length:
        return text
    cut_marks = [".", "؟", "!", "…", "؟"]
    trunc = text[: max_length - 5]
    cut_pos = max(trunc.rfind(m) for m in cut_marks)
    if cut_pos == -1:
        cut_pos = trunc.rfind(" ")
        if cut_pos == -1:
            cut_pos = len(trunc)
    summary = trunc[:cut_pos].strip()
    if lang == "en":
        return f"{summary}...\n\nWould you like more details?"
    else:
        return f"{summary}...\n\nهل ترغب بالتفصيل أكثر؟"

def openai_translate(text: str, target_language_code: str) -> str:
    """ترجمة بسيطة باستخدام OpenAI عند توفره."""
    if not client or not text:
        return text
    try:
        if target_language_code == "ar":
            prompt = f"Translate to standard Arabic. Return only the translation:\n\n{text}"
        elif target_language_code == "en":
            prompt = f"Translate the following text to English. Return only the translation:\n\n{text}"
        else:
            prompt = (
                f"Translate the following Arabic text to {target_language_code}. "
                f"Return only the translation:\n\n{text}"
            )
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=256,
        )
        out = (resp.choices[0].message.content or "").strip()
        return out.split(":", 1)[-1].strip() if ":" in out[:15] else out
    except Exception as e:
        print("⚠️ ترجمة:", e)
        return text

def translate_field_for_lang(text: str, lang: str) -> str:
    """ترجمة حقل واحد إذا كانت اللغة المطلوبة ليست العربية."""
    if not text:
        return text
    if lang == "ar":
        return text
    return openai_translate(text, lang)

def openai_correct(text: str) -> str:
    """تصحيح الإملاء العربي باستخدام OpenAI إن توفر (غير مستخدم في الشات للتسريع)."""
    if not client or not text:
        return text
    try:
        prompt = f"صحّح الأخطاء الإملائية في النص العربي التالي وأعد النص المصحح فقط:\n\n{text}"
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=128,
        )
        out = (resp.choices[0].message.content or "").strip()
        return out.split(":", 1)[-1].strip() if ":" in out[:15] else out
    except Exception as e:
        print("⚠️ تصحيح:", e)
        return text

# فوتر عربي / إنجليزي
FOOTER_AR = (
    "مُولَّد آليًا • قد يحتوي على أخطاء طفيفة\n"
    "مع تحياتي فريق زمرة 🩸"
)
FOOTER_EN = (
    "AI-generated • may contain minor errors\n"
    "With regards, Zomrah Team 🩸"
)

# ==============================
# Intent: خدمة العملاء / الدعم
# ==============================
CUSTOMER_SERVICE_RE = re.compile(
    r"(خدمة.?العملاء|الدعم|التواصل|تواصل|واتساب|whatsapp|customer service|support|contact\s+us|contact)",
    re.IGNORECASE
)

def is_customer_service_intent(text: str) -> bool:
    if not text:
        return False
    return bool(CUSTOMER_SERVICE_RE.search(text))

WHATSAPP_URL = "https://wa.me/966504635135?text=" + up.quote(
    "أرغب في التواصل مع خدمة عملاء زمرة"
)

# ==============================
# 3) Flask + DB
# ==============================
app = Flask(__name__, template_folder="templates", static_folder="static")
CORS(app)

DB_NAME = "chat_logs.db"

def init_db():
    """تهيئة قواعد البيانات (logs + reminders)."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL;")
    c.execute("PRAGMA synchronous=NORMAL;")
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS logs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            raw_query TEXT,
            corrected_query TEXT,
            response_type TEXT,
            kb_source TEXT,
            bot_response TEXT
        )
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS reminders(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            user_hint TEXT,
            email TEXT,
            next_date TEXT,
            note TEXT
        )
        """
    )
    conn.commit()
    conn.close()

def save_log(raw_query, corrected_query, response_type, kb_source, bot_response):
    """حفظ ملخص الرد في جدول logs لأغراض الإحصاء والمتابعة."""
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        snippet = (bot_response or "")[:500] + (
            "..." if bot_response and len(bot_response) > 500 else ""
        )
        c.execute(
            """
            INSERT INTO logs(timestamp,raw_query,corrected_query,response_type,kb_source,bot_response)
            VALUES(?,?,?,?,?,?)
            """,
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                raw_query,
                corrected_query,
                response_type,
                kb_source,
                snippet,
            ),
        )
        conn.commit()
    except Exception as e:
        print("⚠️ لم يُحفظ السجل:", e)
    finally:
        try:
            conn.close()
        except Exception:
            pass

with app.app_context():
    try:
        init_db()
        print("✅ DB initialized (app_context).")
    except Exception as e:
        print("⚠️ فشل تهيئة قاعدة البيانات:", e)

# ==============================
# 4) Base Routes
# ==============================
@app.route("/")
def index():
    """واجهة الشات (تستخدم templates/index.html)."""
    return render_template("index.html")

@app.route("/health")
def health():
    """صحة النظام - تستخدمها الواجهة لمعرفة حالة SMTP / SendGrid / OpenAI."""
    return jsonify(
        {
            "ok": True,
            "openai": bool(client),
            "model": OPENAI_MODEL,
            "urgent_sheet": bool(URGENT_SHEET_URL),
            "urgent_json": URGENT_JSON_PATH if os.path.exists(URGENT_JSON_PATH) else None,
            "campaigns_json": CAMPAIGNS_JSON_PATH if os.path.exists(CAMPAIGNS_JSON_PATH) else None,
            "twilio_ready": False,
            "smtp_ready": bool(SMTP_READY),
            "sendgrid_ready": bool(SENDGRID_READY),
            "email_from_name": EMAIL_FROM_NAME,
            "sendgrid_from": SENDGRID_FROM,
        }
    )

# ==============================
# 5) Knowledge Base
# ==============================
def load_knowledge_base(path: str = "knowledge_base.json"):
    kb = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                answer = item.get("answer", "")
                # نحتفظ بالمصدر في الملف لكن عند الإظهار سنستبدله بـ "القاعدة المعرفية"
                src = (
                    item.get("source_type")
                    or item.get("source")
                    or "القاعدة المعرفية"
                )
                for q in item.get("questions", []):
                    kb[q] = {"answer": answer, "source": src}
            if kb:
                print(f"✅ تم تحميل قاعدة معرفية من {path} بعدد {len(kb)} سؤالاً.")
                return kb
        except Exception as e:
            print("⚠️ فشل تحميل knowledge_base.json:", e)

    print("ℹ️ سيتم استخدام قاعدة معرفية افتراضية بسيطة.")
    return {
        "ما هي شروط التبرع بالدم؟": {
            "answer": "يجب أن يكون العمر 18-60 عاماً والوزن ≥50 كجم وبصحة جيدة وبدون أمراض معدية. يفضّل مراجعة المستشفى قبل التبرع.",
            "source": "القاعدة المعرفية",
        },
        "المدة الفاصلة بين التبرعات؟": {
            "answer": "التبرع الكامل: 90 يومًا على الأقل بين كل تبرعين. مكوّنات الدم قد تختلف.",
            "source": "القاعدة المعرفية",
        },
        "هل التبرع بالدم مؤلم؟": {
            "answer": "وخزة الإبرة سريعة وخفيفة عادةً، والسحب نفسه يستغرق دقائق، مع راحة بسيطة بعد التبرع.",
            "source": "القاعدة المعرفية",
        },
    }

KNOWLEDGE_BASE = load_knowledge_base()

def search_knowledge_base(corrected_query: str):
    """
    البحث بالتقريب في القاعدة المعرفية باستخدام fuzzywuzzy.
    ترجع: (answer, source, similarity_score من 0 إلى 100)
    """
    if not corrected_query:
        return None, None, 0

    nq = normalize_arabic(corrected_query)
    keys = list(KNOWLEDGE_BASE.keys())
    norm = {k: normalize_arabic(k) for k in keys}
    vals = list(norm.values())

    if not vals:
        return None, None, 0

    best_partial = process.extractOne(nq, vals, scorer=fuzz.partial_ratio)
    best_token   = process.extractOne(nq, vals, scorer=fuzz.token_sort_ratio)

    candidate = None
    if best_partial and best_token:
        candidate = best_partial if best_partial[1] >= best_token[1] else best_token
    else:
        candidate = best_partial or best_token

    if not candidate:
        return None, None, 0

    best_norm_text, score = candidate
    orig = next((k for k, v in norm.items() if v == best_norm_text), None)
    if not orig:
        return None, None, 0

    d = KNOWLEDGE_BASE[orig]
    return d["answer"], d.get("source"), int(score)

# ==============================
# 6) Chat Endpoint
# ==============================
@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    raw = data.get("message") or ""
    user_message = raw.strip()
    want_detail = bool(data.get("detail"))

    ui_lang = (data.get("lang") or "").lower()
    if ui_lang not in ("ar", "en"):
        ui_lang = "ar"  # افتراضي عربي

    if not user_message:
        msg = "الرجاء كتابة سؤالك." if ui_lang == "ar" else "Please type your question."
        return jsonify(
            {
                "answer": msg,
                "source_type": "Error",
                "source_text": None,
                "not_understood": True,
                "corrected_message": user_message,
            }
        ), 200

    # --------------------------
    # أولوية: Intent خدمة العملاء
    # --------------------------
    if is_customer_service_intent(user_message):
        if ui_lang == "ar":
            txt = (
                "للتواصل مع خدمة عملاء زمرة عبر واتساب، اضغط على الرابط التالي:\n\n"
                f'<a href="{WHATSAPP_URL}" target="_blank" rel="noopener">'
                "فتح واتساب</a>\n\n"
                f"{FOOTER_AR}"
            )
        else:
            txt = (
                "To contact Zomrah customer support via WhatsApp, click the link below:\n\n"
                f'<a href="{WHATSAPP_URL}" target="_blank" rel="noopener">'
                "Open WhatsApp</a>\n\n"
                f"{FOOTER_EN}"
            )

        save_log(user_message, user_message, "Support", "Customer Service", txt)
        return jsonify(
            {
                "answer": txt,
                "source_type": "Support",
                "source_text": "Customer Service",
                "corrected_message": user_message,
                "not_understood": False,
            }
        ), 200

    # كشف لغة النص (سريع)
    detected_lang = "ar"
    try:
        detected_lang = detect(user_message)
    except LangDetectException:
        pass

    target_lang = ui_lang  # نجيب بنفس لغة الواجهة قدر الإمكان

    # --------------------------
    # 1) نحاول من قاعدة المعرفة
    # --------------------------
    SIM_THRESHOLD = 85
    kb_answer = None
    kb_source = None
    kb_score = 0

    if detected_lang == "ar":
        kb_answer, kb_source, kb_score = search_knowledge_base(user_message)

    if kb_answer and kb_score >= SIM_THRESHOLD:
        source_type = "KB"
        # حسب الطلب: دائماً "القاعدة المعرفية"
        source_text = "القاعدة المعرفية" if target_lang == "ar" else "Knowledge base"
        not_understood = False

        if target_lang == "en" and client:
            # نترجم الجواب والقالب للإنجليزية
            core_text = kb_answer if want_detail else summarize_and_simplify(kb_answer, 220, "en")
            final_text = (
                "Source: Knowledge base\n\n"
                f"{core_text}\n\n"
                f"{FOOTER_EN}"
            )
        else:
            core_ar = kb_answer if want_detail else summarize_and_simplify(kb_answer, 220, "ar")
            final_text = (
                f"المصدر: القاعدة المعرفية\n\n"
                f"{core_ar}\n\n"
                f"{FOOTER_AR}"
            )

        save_log(user_message, user_message, source_type, source_text, final_text)
        return jsonify(
            {
                "answer": final_text,
                "source_type": source_type,
                "source_text": source_text,
                "corrected_message": user_message,
                "not_understood": not_understood,
            }
        ), 200

    # --------------------------
    # 2) لم نجد إجابة في القاعدة → AI أو فولباك
    # --------------------------
    not_understood = True

    def fallback_message(lang: str, ai_error: bool = False) -> Tuple[str, str, str]:
        """
        ترجع: (final_text, source_type, source_text)
        """
        wa_url = WHATSAPP_URL
        wa_btn_ar = (
            f'<a href="{wa_url}" '
            'target="_blank" rel="noopener" '
            'style="display:inline-block;margin-top:8px;padding:8px 14px;'
            'border-radius:999px;background:#25D366;color:#fff;'
            'text-decoration:none;font-weight:700;">'
            'التواصل عبر واتساب'
            '</a>'
        )
        wa_btn_en = (
            f'<a href="{wa_url}" '
            'target="_blank" rel="noopener" '
            'style="display:inline-block;margin-top:8px;padding:8px 14px;'
            'border-radius:999px;background:#25D366;color:#fff;'
            'text-decoration:none;font-weight:700;">'
            'Contact via WhatsApp'
            '</a>'
        )

        if lang == "en":
            base = "I couldn’t clearly understand your question."
            if ai_error:
                base += "\nThere was also an issue connecting to the AI service."
            base += "\nYou can contact the Zomrah team via WhatsApp:\n\n"
            base += wa_btn_en + "\n\n" + FOOTER_EN
            return base, "Fallback", "Zomrah team"
        else:
            base = "لم أستطع فهم سؤالك بشكل كافٍ."
            if ai_error:
                base += "\nكما حدثت مشكلة في الاتصال بخدمة الذكاء الاصطناعي."
            base += "\nيمكنك التواصل مع فريق زمرة عبر واتساب:\n\n"
            base += wa_btn_ar + "\n\n" + FOOTER_AR
            return base, "Fallback", "فريق زمرة"

    # لو ما في OpenAI أو مفعّل FORCE_AI_FALLBACK ⇒ فولباك دقيق
    if (not client) or FORCE_AI_FALLBACK:
        final_text, source_type, source_text = fallback_message(target_lang, ai_error=False)
        save_log(user_message, user_message, source_type, source_text, final_text)
        return jsonify(
            {
                "answer": final_text,
                "source_type": source_type,
                "source_text": source_text,
                "corrected_message": user_message,
                "not_understood": not_understood,
            }
        ), 200

    # --------------------------
    # 3) استخدام OpenAI مع الرسالة الجديدة
    # --------------------------
    try:
        prompt_lang = "العربية" if target_lang == "ar" else "الإنجليزية"
        system_instruction = (
            f"أنت مساعد طبي يجيب عن أسئلة التبرع بالدم وفق إرشادات وزارة الصحة السعودية فقط.\n"
            f"- أجب باختصار قدر الإمكان.\n"
            f"- أجب بلغة الواجهة المطلوبة: {prompt_lang}.\n"
            f"- إن لم تكن متأكداً، اعتذر بلطف واطلب مراجعة الطبيب أو التواصل مع فريق زمرة.\n"
        )

        res = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_instruction},
                {"role": "user", "content": user_message},
            ],
            max_tokens=220,
            temperature=0.3,
        )
        ai_text = (res.choices[0].message.content or "").strip()

        if not ai_text or len(ai_text) < 15:
            final_text, source_type, source_text = fallback_message(target_lang, ai_error=False)
        else:
            source_type = "AI"
            source_text = "OpenAI"

            if target_lang == "en":
                core_txt = ai_text if want_detail else summarize_and_simplify(ai_text, 230, "en")
                final_text = (
                    "We could not find an answer in the knowledge base, "
                    "so OpenAI generated the following response:\n\n"
                    f"{core_txt}\n\n"
                    f"{FOOTER_EN}"
                )
            else:
                core_txt = ai_text if want_detail else summarize_and_simplify(ai_text, 230, "ar")
                final_text = (
                    "لم نعثر على إجابة في قاعدة المعرفة؛ استعنا بـ OpenAI لصياغة الرد التالي:\n\n"
                    f"{core_txt}\n\n"
                    f"{FOOTER_AR}"
                )

    except Exception as e:
        final_text, source_type, source_text = fallback_message(target_lang, ai_error=True)

    save_log(user_message, user_message, source_type, source_text, final_text)

    return jsonify(
        {
            "answer": final_text,
            "source_type": source_type,
            "source_text": source_text,
            "corrected_message": user_message,
            "not_understood": not_understood,
        }
    ), 200

# ==============================
# 7) Urgent Needs
# ==============================
def gmaps_place_link(name: str) -> str:
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

def _format_urgent_rows(rows, lang: str = "ar"):
    out = []
    for r in rows or []:
        hospital = (
            r.get("hospital")
            or r.get("Hospital")
            or r.get("المستشفى")
            or ""
        )
        status = r.get("status") or r.get("Status") or r.get("الحالة") or ""
        details = (
            r.get("details")
            or r.get("Details")
            or r.get("التفاصيل")
            or ""
        )
        loc = (
            r.get("location_url")
            or r.get("Location")
            or r.get("الموقع")
            or ""
        )
        if hospital and not loc:
            loc = gmaps_place_link(hospital)

        if hospital:
            if lang == "en":
                hospital_t = translate_field_for_lang(hospital, "en")
                status_t   = translate_field_for_lang(status or "", "en")
                details_t  = translate_field_for_lang(details or "", "en")
            else:
                hospital_t, status_t, details_t = hospital, status, details

            out.append(
                {
                    "hospital": hospital_t,
                    "status": status_t,
                    "details": details_t,
                    "location_url": loc,
                }
            )
    return out

FALLBACK_URGENT = [
    {
        "hospital": "مستشفى الملك فهد العام بجدة",
        "status": "عاجل",
        "details": "+O لحالات طارئة",
        "location_url": gmaps_place_link("King Fahd General Hospital Jeddah"),
    },
    {
        "hospital": "بنك الدم الإقليمي – جدة",
        "status": "مرتفع جداً",
        "details": "نقص صفائح B-",
        "location_url": gmaps_place_link("Jeddah Regional Laboratory and Blood Bank"),
    },
    {
        "hospital": "مستشفى شرق جدة",
        "status": "عاجل",
        "details": "A- لحالات طوارئ",
        "location_url": gmaps_place_link("East Jeddah Hospital Blood Bank"),
    },
]

@app.route("/api/urgent_needs")
def urgent_needs():
    """جلب قائمة الاحتياج العاجل من Google Sheet أو JSON أو fallback."""
    lang = (request.args.get("lang") or "ar").lower()
    if lang not in ("ar", "en"):
        lang = "ar"

    needs = None

    if URGENT_SHEET_URL:
        rows = _fetch_csv(URGENT_SHEET_URL)
        if rows:
            needs = _format_urgent_rows(rows, lang=lang)

    if not needs:
        js = _load_json(URGENT_JSON_PATH)
        if isinstance(js, dict) and isinstance(js.get("needs"), list):
            needs = _format_urgent_rows(js["needs"], lang=lang)
        elif isinstance(js, list):
            needs = _format_urgent_rows(js, lang=lang)

    if not needs:
        needs = _format_urgent_rows(FALLBACK_URGENT, lang=lang)

    base_text_ar = "احتياجات عاجلة (يرجى الاتصال قبل الزيارة)."
    base_text_en = "Urgent needs (please call the hospital before visiting)."

    answer_ar = base_text_ar
    answer_en = base_text_en

    return jsonify(
        {
            "answer_ar": answer_ar,
            "answer_en": answer_en,
            "source": "Sheet/JSON/Fallback",
            "needs": needs,
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
    ), 200

# ==============================
# 8) Eligibility (فحص الأهلية)
# ==============================
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

    age = int(payload.get("age", 0) or 0)
    weight = int(payload.get("weight", 0) or 0)
    last = int(payload.get("last_donation_days", 9999) or 9999)
    on_ac = bool(payload.get("on_anticoagulants", False))
    on_ab = bool(payload.get("on_antibiotics", False))
    cold = bool(payload.get("has_cold", False))
    preg = bool(payload.get("pregnant", False))
    proc = int(payload.get("recent_procedure_days", 9999) or 9999)
    tattoo = int(payload.get("tattoo_months", 999) or 999)

    if age < 18:
        eligible = False
        reasons.append("العمر أقل من 18 سنة.")
    if weight < 50:
        eligible = False
        reasons.append("الوزن أقل من 50 كجم.")

    if last < 90:
        eligible = False
        days_left = 90 - last
        next_date = (datetime.now() + timedelta(days=days_left)).strftime("%Y-%m-%d")
        reasons.append(
            f"لم يمض 90 يومًا منذ آخر تبرع. متاح بعد {days_left} يومًا ({next_date})."
        )

    if on_ac:
        eligible = False
        reasons.append("أدوية السيولة تمنع التبرع حاليًا.")
    if on_ab:
        eligible = False
        reasons.append("أجّل التبرع 7 أيام بعد آخر جرعة مضاد حيوي.")
    if cold:
        eligible = False
        reasons.append("أعراض زكام/حمى: أجّل حتى التعافي.")
    if preg:
        eligible = False
        reasons.append("الحمل يمنع التبرع. يُستأنف بعد 6 أسابيع من الولادة/الإجهاض.")
    if proc < 7:
        eligible = False
        reasons.append("إجراء/قلع أسنان حديث: انتظر 7 أيام على الأقل.")
    if tattoo < 6:
        eligible = False
        reasons.append("وشم/ثقب خلال آخر 6 أشهر: يؤجل التبرع.")

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

# ==============================
# 9) Reminder (Email + ICS)
# ==============================
def make_ics_bytes(date_str: str) -> bytes:
    dt = (
        datetime.fromisoformat(date_str)
        if "T" not in date_str
        else datetime.fromisoformat(date_str.replace("Z", "").replace("z", ""))
    )
    dt_end = dt + timedelta(hours=1)

    def pad(n: int) -> str:
        return f"{n:02d}"

    def fmt(d: datetime) -> str:
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

def try_send_email(
    to_email: str, subject: str, body: str, ics_bytes: bytes, ics_name: str
) -> Tuple[bool, str]:
    if SENDGRID_READY:
        try:
            url = "https://api.sendgrid.com/v3/mail/send"
            headers = {
                "Authorization": f"Bearer {SENDGRID_API_KEY}",
                "Content-Type": "application/json",
            }

            from_email = SENDGRID_FROM or SMTP_FROM or to_email

            payload = {
                "personalizations": [{"to": [{"email": to_email}]}],
                "from": {
                    "email": from_email,
                    "name": EMAIL_FROM_NAME,
                },
                "subject": subject,
                "content": [{"type": "text/plain", "value": body}],
            }

            if ics_bytes:
                encoded = base64.b64encode(ics_bytes).decode("utf-8")
                payload.setdefault("attachments", []).append(
                    {
                        "content": encoded,
                        "type": "text/calendar",
                        "filename": ics_name,
                    }
                )

            resp = requests.post(url, headers=headers, json=payload, timeout=10)
            if resp.status_code in (200, 202):
                return True, "تم الإرسال عبر SendGrid."
            else:
                return False, f"SendGrid error: {resp.status_code} {resp.text}"
        except Exception as e:
            return False, f"SendGrid exception: {e}"

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
    data = request.json or {}
    user_hint = (data.get("user_hint") or "متبرع").strip()
    email = (data.get("email") or "").strip()
    next_date = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")

    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute(
            "INSERT INTO reminders(created_at,user_hint,email,next_date,note) VALUES(?,?,?,?,?)",
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                user_hint,
                email,
                next_date,
                "Reminder for next eligible donation (whole blood).",
            ),
        )
        conn.commit()
    except Exception as e:
        print("⚠️ خطأ في حفظ التذكير في قاعدة البيانات:", e)
    finally:
        try:
            conn.close()
        except Exception:
            pass

    email_status = {
        "sent": False,
        "message": "تم تسجيل الموعد فقط.",
        "via": None,
    }

    if email:
        ics = make_ics_bytes(next_date)
        ok, msg = try_send_email(
            email,
            "تذكير زمرة: موعد التبرع القادم",
            (
                f"مرحباً {user_hint},\n\n"
                f"هذا تذكير من زمرة بموعد تبرعك المقترح بتاريخ {next_date}.\n"
                f"يمكنك أيضًا إضافة الموعد من داخل التطبيق أو من ملف التقويم المرفق.\n\n"
                f"مع التحية،\nفريق زمرة."
            ),
            ics,
            f"Zomrah-Reminder-{next_date}.ics",
        )
        email_status = {
            "sent": ok,
            "message": msg,
            "via": "sendgrid" if SENDGRID_READY else ("smtp" if SMTP_READY else None),
        }

    return jsonify({"ok": True, "next_date": next_date, "email_status": email_status})

@app.route("/api/reminder/ics/<date_str>")
def reminder_ics(date_str):
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

# ==============================
# 10) Upload audio (Mock)
# ==============================
@app.route("/api/upload_audio", methods=["POST"])
def upload_audio():
    if "audio_file" not in request.files:
        return jsonify({"error": "لم يتم إرسال ملف صوتي"}), 400

    text = "ما هي شروط التبرع بالدم؟"
    corrected = text
    answer, src, score = search_knowledge_base(corrected)

    if answer:
        final = summarize_and_simplify(answer, 250, "ar")
        st = "KB (من الصوت)"
        src_label = "القاعدة المعرفية"
    else:
        final = "تم تحويل الصوت؛ لا إجابة محددة في القاعدة المعرفية."
        st = "Error (من الصوت)"
        src_label = None

    save_log("ملف صوتي", corrected, st, src_label, final)

    return jsonify(
        {
            "transcribed_text": corrected,
            "answer": final,
            "source_type": st,
            "source_text": src_label,
            "corrected_message": corrected,
        }
    )

# ==============================
# 11) Stats / Campaigns
# ==============================
@app.route("/api/stats")
def stats():
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM logs")
        total = c.fetchone()[0]
        c.execute("SELECT response_type, COUNT(*) FROM logs GROUP BY response_type")
        by_type = {k: v for k, v in c.fetchall()}
        conn.close()
        return jsonify({"ok": True, "total_logs": total, "by_type": by_type})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/campaigns")
def campaigns():
    data = _load_json(CAMPAIGNS_JSON_PATH)
    if not data:
        return jsonify(
            {
                "ok": False,
                "campaigns": [],
                "message": "ملف الحملات غير متوفر",
            }
        )
    return jsonify({"ok": True, "campaigns": data})

# ==============================
# 12) Run (Local)
# ==============================
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)



