# -*- coding: utf-8 -*-
"""
ZOMRA_PROJECT - Flask Chatbot (Blood Donation Assistant)
نسخة محسّنة للتشغيل المحلي على الجوال:
- تشغيل على الشبكة: host=0.0.0.0 مع ssl_context="adhoc" لاختبار HTTPS
- /health يعيد twilio_ready=False لتجنب استدعاء SMS
"""

from openai import OpenAI
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import os
import sqlite3
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fuzzywuzzy import process, fuzz
from langdetect import detect, LangDetectException
import re
import unicodedata
import csv
import json
from io import StringIO

# **********************************************
# 1) التهيئة والبيئة
# **********************************************
load_dotenv(override=True)

OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip().strip('"').strip("'")
OPENAI_MODEL = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
FORCE_AI_FALLBACK = (os.getenv("FORCE_AI_FALLBACK") or "false").lower() in {"1", "true", "yes"}

URGENT_SHEET_URL = (os.getenv("URGENT_NEEDS_SHEET_CSV") or "").strip()
URGENT_JSON_PATH = (os.getenv("URGENT_NEEDS_JSON") or "urgent_needs.json").strip()
CAMPAIGNS_JSON_PATH = (os.getenv("CAMPAIGNS_JSON") or "campaigns.json").strip()

if not OPENAI_API_KEY:
    print("⚠️ لم يتم العثور على OPENAI_API_KEY في .env. سيتم العمل دون ذكاء اصطناعي (وضع KB فقط).")

# OpenAI client (اختياري)
client = None
if OPENAI_API_KEY:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print(f"⚠️ فشل تهيئة OpenAI: {e}")
        client = None

# **********************************************
# 2) أدوات العربية والملخص
# **********************************************
_ARABIC_DIACRITICS_RE = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u0653-\u065F\u06D6-\u06ED]")

def normalize_arabic(text: str) -> str:
    if not text:
        return ""
    t = _ARABIC_DIACRITICS_RE.sub("", text)
    t = t.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
    t = t.replace("ؤ", "و").replace("ئ", "ي").replace("ة", "ه")
    t = t.replace("ـ", "")
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def summarize_and_simplify(text, max_length=250):
    if not text or len(text) <= max_length:
        return text
    cut_marks = ['.', '؟', '!', '…']
    trunc = text[: max_length - 5]
    cut_pos = max(trunc.rfind(m) for m in cut_marks)
    if cut_pos == -1:
        cut_pos = trunc.rfind(' ')
        if cut_pos == -1:
            cut_pos = len(trunc)
    summary = text[:cut_pos].strip()
    return f"{summary}...\n\nهل ترغب بالتفصيل أكثر؟"

def openai_translate(text, target_language_code):
    if not client or not text:
        return text
    try:
        if target_language_code == 'ar':
            prompt = f"Translate the following text to clear standard Arabic. Return only the translation:\n\n{text}"
        else:
            prompt = f"Translate the following Arabic text to {target_language_code}. Return only the translation:\n\n{text}"
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
        translated = (resp.choices[0].message.content or "").strip()
        if ":" in translated[:15]:
            translated = translated.split(":", 1)[-1].strip()
        return translated or text
    except Exception as e:
        print(f"⚠️ مشكلة في الترجمة ({target_language_code}): {e}")
        return text

def openai_correct(text):
    if not client or not text:
        return text
    try:
        prompt = f"قم بتصحيح الأخطاء الإملائية في النص العربي التالي، وأعد النص المصحح فقط:\n\n{text}"
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
        )
        corrected = (resp.choices[0].message.content or "").strip()
        if ":" in corrected[:15]:
            corrected = corrected.split(":", 1)[-1].strip()
        return corrected or text
    except Exception as e:
        print(f"⚠️ مشكلة في التصحيح: {e}")
        return text

# **********************************************
# 3) Flask & SQLite
# **********************************************
app = Flask(__name__, template_folder="templates", static_folder="static")
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
            note TEXT
        )
    """)
    conn.commit(); conn.close()

def save_log(raw_query, corrected_query, response_type, kb_source, bot_response):
    try:
        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()
        response_to_log = (bot_response or "")[:500]
        if bot_response and len(bot_response) > 500:
            response_to_log += "..."
        c.execute(
            """INSERT INTO logs (timestamp, raw_query, corrected_query, response_type, kb_source, bot_response)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                raw_query,
                corrected_query,
                response_type,
                kb_source,
                response_to_log,
            ),
        )
        conn.commit()
    except Exception as e:
        print(f"⚠️ لم يُحفظ السجل: {e}")
    finally:
        try: conn.close()
        except: pass

# **********************************************
# 4) صفحات مساعدة
# **********************************************
@app.route("/")
def serve_index():
    return render_template("index.html")

@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "openai": bool(client),
        "model": OPENAI_MODEL,
        "urgent_sheet": bool(URGENT_SHEET_URL),
        "urgent_json": URGENT_JSON_PATH if os.path.exists(URGENT_JSON_PATH) else None,
        "campaigns_json": CAMPAIGNS_JSON_PATH if os.path.exists(CAMPAIGNS_JSON_PATH) else None,
        # مهم للواجهة: نخليها False عشان ما تحاول ترسل SMS
        "twilio_ready": False
    })

# **********************************************
# 5) القاعدة المعرفية (ثابتة)
# **********************************************
KNOWLEDGE_BASE = {
    "ما هي شروط التبرع بالدم؟": {
        "answer": "يجب أن يكون عمر المتبرع بين 18 و 60 عاماً، ووزنه لا يقل عن 50 كجم، وأن يكون بصحة جيدة ولا يعاني من أمراض مزمنة أو معدية مثل الإيدز أو التهاب الكبد. يجب أن يكون مستوى الهيموجلوبين مناسباً، وعادة ما يُطلب 12.5 للنساء و 13 للرجال. كما يفضل تجنب التبرع إذا كنت تتناول بعض الأدوية، أو خضعت لوشم أو ثقب خلال الستة أشهر الماضية، أو أجريت عملية جراحية كبرى مؤخراً. هذه الشروط مطبقة في أغلب بنوك الدم في المملكة العربية السعودية لضمان أعلى معايير الجودة والسلامة للمتبرع والمتلقي. يرجى دائماً مراجعة المستشفى قبل التبرع.",
        "source": "من القاعدة المعرفية",
    },
    "المدة الفاصلة بين التبرعات؟": {
        "answer": "يجب أن تكون المدة الفاصلة بين كل تبرع بالدم كامل **ثلاثة أشهر (90 يوماً)** على الأقل. بالنسبة للتبرع بمكونات الدم يمكن أن تكون المدة أقصر.",
        "source": "من القاعدة المعرفية",
    },
    "هل التبرع بالدم مؤلم؟": {
        "answer": "الوخز بالإبرة هو الجزء الوحيد الذي قد يسبب ألماً خفيفاً وسريعاً، عملية السحب نفسها غير مؤلمة وتستغرق دقائق.",
        "source": "من القاعدة المعرفية",
    },
}

# **********************************************
# 6) البحث الغامض مع تطبيع عربي
# **********************************************
from typing import Tuple

def search_knowledge_base(corrected_query) -> Tuple[str, str]:
    if not corrected_query:
        return None, None
    normalized_query = normalize_arabic(corrected_query)
    kb_questions = list(KNOWLEDGE_BASE.keys())
    norm_map = {q: normalize_arabic(q) for q in kb_questions}
    norm_values = list(norm_map.values())

    PARTIAL_SCORE_THRESHOLD = 85
    best_partial = process.extractOne(normalized_query, norm_values, scorer=fuzz.partial_ratio)
    if best_partial and best_partial[1] >= PARTIAL_SCORE_THRESHOLD:
        matched = best_partial[0]
        original_q = next((k for k, v in norm_map.items() if v == matched), None)
        if original_q:
            data = KNOWLEDGE_BASE[original_q]
            return data["answer"], data["source"]

    TOKEN_SORT_THRESHOLD = 80
    best_token = process.extractOne(normalized_query, norm_values, scorer=fuzz.token_sort_ratio)
    if best_token and best_token[1] >= TOKEN_SORT_THRESHOLD:
        matched = best_token[0]
        original_q = next((k for k, v in norm_map.items() if v == matched), None)
        if original_q:
            data = KNOWLEDGE_BASE[original_q]
            return data["answer"], data["source"]

    return None, None

# **********************************************
# 7) الشات
# **********************************************
@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    user_message = (data.get("message") or "").trim() if hasattr(str, 'trim') else (data.get("message") or "").strip()
    want_detail = bool(data.get("detail"))

    if not user_message:
        return jsonify({"answer": "الرجاء كتابة سؤالك.", "source_type": "Error", "source_text": None}), 200

    original_lang = "ar"
    try:
        original_lang = detect(user_message)
    except LangDetectException:
        pass

    query_to_process = user_message
    if original_lang != "ar" and client:
        query_to_process = openai_translate(user_message, "ar")

    corrected = openai_correct(query_to_process) or query_to_process

    answer, source_text = search_knowledge_base(corrected)
    if answer:
        source_type = "KB"
        final_answer = answer if want_detail else summarize_and_simplify(answer, max_length=250)
    else:
        if client and not FORCE_AI_FALLBACK:
            try:
                ai_response = client.chat.completions.create(
                    model=OPENAI_MODEL,
                    messages=[{"role": "user", "content": corrected}],
                )
                openai_answer = (ai_response.choices[0].message.content or "").strip()
                summarized = openai_answer if want_detail else summarize_and_simplify(openai_answer, max_length=250)
                source_type = "AI"
                source_text = "مُوَلَّد بواسطة AI"
                final_answer = (
                    "لم نعثر على إجابة في قاعدة المعرفة؛ استعنا Gemini لصياغة الرد التالي:\n\n"
                    f"{summarized}\n\n"
                    "مُولَّد آليًا • قد يحتوي على أخطاء طفيفة\nمع تحيات فريق زمرة"
                )
            except Exception as e:
                source_type = "Error"
                final_answer = f"عذراً، واجهتنا مشكلة في الاتصال بالذكاء الاصطناعي: {e}"
                save_log(user_message, corrected, source_type, None, final_answer)
                return jsonify({"answer": final_answer, "source_type": source_type, "corrected_message": corrected}), 500
        else:
            source_type = "KB-Only"
            final_answer = "عذراً، لم نجد إجابة محددة في القاعدة المعرفية، والذكاء الاصطناعي غير مفعّل حالياً."

    if original_lang != "ar" and client:
        final_answer = openai_translate(final_answer, original_lang)

    save_log(user_message, corrected, source_type, source_text, final_answer)
    return jsonify({
        "answer": final_answer,
        "source_type": source_type,
        "source_text": source_text,
        "corrected_message": corrected
    }), 200

# **********************************************
# 8) الاحتياجات العاجلة
# **********************************************
def gmaps_place_link(name: str) -> str:
    import urllib.parse as up
    return f"https://www.google.com/maps/search/?api=1&query={up.quote(name)}"

def _fetch_csv(url: str):
    import requests
    try:
        r = requests.get(url, timeout=6)
        r.raise_for_status()
        text = r.text
        reader = csv.DictReader(StringIO(text))
        rows = [dict(row) for row in reader]
        return rows
    except Exception as e:
        print(f"⚠️ فشل جلب CSV: {e}")
        return None

def _load_json(path: str):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"⚠️ فشل قراءة JSON {path}: {e}")
    return None

def _format_urgent_rows(rows):
    out = []
    for r in rows or []:
        hospital = r.get("hospital") or r.get("Hospital") or r.get("المستشفى") or ""
        status = r.get("status") or r.get("Status") or r.get("الحالة") or ""
        details = r.get("details") or r.get("Details") or r.get("التفاصيل") or ""
        loc = r.get("location_url") or r.get("Location") or r.get("الموقع") or ""
        if hospital and not loc:
            loc = gmaps_place_link(hospital)
        if hospital:
            out.append({"hospital": hospital, "status": status, "details": details, "location_url": loc})
    return out

FALLBACK_URGENT = [
    {
        "hospital": "مستشفى الملك فهد العام بجدة",
        "status": "عاجل",
        "details": "مطلوب +O لحالات طارئة",
        "location_url": gmaps_place_link("King Fahd General Hospital Jeddah"),
    },
    {
        "hospital": "بنك الدم الإقليمي – جدة (المختبر الإقليمي)",
        "status": "مرتفع جداً",
        "details": "نقص صفائح B-",
        "location_url": gmaps_place_link("Jeddah Regional Laboratory and Blood Bank"),
    },
    {
        "hospital": "مستشفى شرق جدة",
        "status": "عاجل",
        "details": "مطلوب A- لحالات طوارئ",
        "location_url": gmaps_place_link("East Jeddah Hospital Blood Bank"),
    },
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

    data = {
        "answer_ar": "قائمة احتياجات عاجلة للتبرع بالدم من جهات معتمدة — يُفضّل الاتصال قبل الحضور.",
        "answer_en": "Urgent blood donation needs from verified sources — please call before visiting.",
        "source": "مصدر ديناميكي (Sheet/JSON) أو fallback ثابت",
        "needs": needs_rows,
        "updated_at": datetime.utcnow().isoformat() + "Z",
    }
    return jsonify(data), 200

# **********************************************
# 9) الأهلية للتبرع
# **********************************************
ELIGIBILITY_QUESTIONS = [
    {"id": "age", "text": "كم عمرك؟", "type": "number", "min": 1, "max": 100},
    {"id": "weight", "text": "كم وزنك بالكيلو؟", "type": "number", "min": 30, "max": 300},
    {"id": "last_donation_days", "text": "متى كان آخر تبرع لك؟ (بالأيام)", "type": "number", "min": 0, "max": 2000},
    {"id": "on_anticoagulants", "text": "هل تتناول أدوية سيولة الدم حالياً؟", "type": "boolean"},
    {"id": "on_antibiotics", "text": "هل تتناول مضادًا حيويًا لعدوى نشطة؟", "type": "boolean"},
    {"id": "has_cold", "text": "هل لديك أعراض زكام/حمى حالياً؟", "type": "boolean"},
    {"id": "pregnant", "text": "هل أنتِ حامل حاليًا؟ (للنساء)", "type": "boolean"},
    {"id": "recent_procedure_days", "text": "هل أجريت عملية أو قلع أسنان مؤخرًا؟ كم يوم مضى؟", "type": "number", "min": 0, "max": 400},
    {"id": "tattoo_months", "text": "هل عملت وشم/ثقب خلال آخر كم شهر؟", "type": "number", "min": 0, "max": 48},
]

@app.route("/api/eligibility/questions", methods=["GET"])
def eligibility_questions():
    return jsonify({"questions": ELIGIBILITY_QUESTIONS})

def evaluate_eligibility(payload: dict):
    reasons = []
    eligible = True
    next_date = None
    age = int(payload.get("age", 0) or 0)
    weight = int(payload.get("weight", 0) or 0)
    last_days = int(payload.get("last_donation_days", 9999) or 9999)
    on_ac = bool(payload.get("on_anticoagulants", False))
    on_ab = bool(payload.get("on_antibiotics", False))
    has_cold = bool(payload.get("has_cold", False))
    pregnant = bool(payload.get("pregnant", False))
    recent_proc = int(payload.get("recent_procedure_days", 9999) or 9999)
    tattoo_months = int(payload.get("tattoo_months", 999) or 999)

    if age < 18:
        eligible = False; reasons.append("العمر أقل من 18 سنة.")
    if weight < 50:
        eligible = False; reasons.append("الوزن أقل من 50 كجم.")
    if last_days < 90:
        eligible = False
        days_left = 90 - last_days
        next_date = (datetime.now() + timedelta(days=days_left)).strftime("%Y-%m-%d")
        reasons.append(f"لم يمض 90 يومًا منذ آخر تبرع. متاح بعد {days_left} يومًا ({next_date}).")
    if on_ac:
        eligible = False; reasons.append("أدوية السيولة تمنع التبرع حاليًا.")
    if on_ab:
        eligible = False; reasons.append("أجّل التبرع 7 أيام بعد آخر جرعة مضاد حيوي.")
    if has_cold:
        eligible = False; reasons.append("أعراض زكام/حمى: أجّل حتى التعافي.")
    if pregnant:
        eligible = False; reasons.append("الحمل يمنع التبرع. يُستأنف بعد 6 أسابيع من الولادة/الإجهاض.")
    if recent_proc < 7:
        eligible = False; reasons.append("إجراء/قلع أسنان حديث: انتظر 7 أيام على الأقل.")
    if tattoo_months < 6:
        eligible = False; reasons.append("وشم/ثقب خلال آخر 6 أشهر: يؤجل التبرع.")

    if not next_date:
        next_date = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")
    return eligible, reasons, next_date

@app.route("/api/eligibility/evaluate", methods=["POST"])
def eligibility_evaluate():
    payload = request.json or {}
    eligible, reasons, next_date = evaluate_eligibility(payload)
    return jsonify({"eligible": eligible, "reasons": reasons, "next_eligible_date": next_date})

# **********************************************
# 10) تذكير 90 يوم
# **********************************************
@app.route("/api/reminder", methods=["POST"])
def reminder():
    payload = request.json or {}
    user_hint = (payload.get("user_hint") or "").strip() or "User"
    next_date = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")
    note = "Reminder for next eligible donation (whole blood)."
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute(
        "INSERT INTO reminders (created_at, user_hint, next_date, note) VALUES (?, ?, ?, ?)",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_hint, next_date, note),
    )
    conn.commit(); conn.close()
    return jsonify({"ok": True, "next_date": next_date})

# **********************************************
# 11) رفع صوت (محاكاة)
# **********************************************
@app.route("/api/upload_audio", methods=["POST"])
def upload_audio():
    if "audio_file" not in request.files:
        return jsonify({"error": "لم يتم إرسال ملف صوتي"}), 400
    simulated_transcription = "ما هي شروط التبرع بالدم؟"
    corrected = openai_correct(simulated_transcription) or simulated_transcription
    answer, source_text = search_knowledge_base(corrected)
    if answer:
        source_type = "KB (من الصوت)"
        final_answer = summarize_and_simplify(answer, max_length=250)
    else:
        final_answer = "تم تحويل صوتك بنجاح. لكن لم يتم العثور على إجابة محددة."
        source_type = "Error (من الصوت)"; source_text = None
    save_log("ملف صوتي", corrected, source_type, source_text, final_answer)
    return jsonify({
        "transcribed_text": corrected,
        "answer": final_answer,
        "source_type": source_type,
        "source_text": source_text,
        "corrected_message": corrected
    }), 200

# **********************************************
# 12) إحصائيات
# **********************************************
@app.route("/api/stats", methods=["GET"])
def stats():
    try:
        conn = sqlite3.connect(DB_NAME); c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM logs"); total = c.fetchone()[0]
        c.execute("SELECT response_type, COUNT(*) FROM logs GROUP BY response_type")
        by_type = {row[0]: row[1] for row in c.fetchall()}
        conn.close()
        return jsonify({"ok": True, "total_logs": total, "by_type": by_type})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# **********************************************
# 13) حملات التبرع (اختياري)
# **********************************************
@app.route("/api/campaigns", methods=["GET"])
def campaigns():
    data = _load_json(CAMPAIGNS_JSON_PATH)
    if not data:
        return jsonify({"ok": False, "campaigns": [], "message": "ملف الحملات غير متوفر"}), 200
    return jsonify({"ok": True, "campaigns": data}), 200

# **********************************************
# Run
# **********************************************
if __name__ == "__main__":
    init_db()
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
