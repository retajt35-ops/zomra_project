# -*- coding: utf-8 -*-
"""
ZOMRA_PROJECT - Flask (Zomrah)
- KB + اختياري OpenAI
- Urgent needs (Sheet/JSON/Fallback)
- Eligibility (موسّع)
- Reminders: DB + Email (SMTP)
"""

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import os, sqlite3, re, json, csv, unicodedata
from datetime import datetime, timedelta
from dotenv import load_dotenv

# ====== إعدادات اختيارية للذكاء الاصطناعي ======
try:
    from openai import OpenAI
except:
    OpenAI = None

# ====== بريد (SMTP) ======
import smtplib, ssl
from email.message import EmailMessage

# ====== بحث نصي غامض عربي ======
from fuzzywuzzy import process, fuzz
from langdetect import detect, LangDetectException
from io import StringIO

load_dotenv(override=True)

OPENAI_API_KEY  = (os.getenv("OPENAI_API_KEY") or "").strip().strip('"').strip("'")
OPENAI_MODEL    = (os.getenv("OPENAI_MODEL") or "gpt-4o-mini").strip()
FORCE_AI_FALLBACK = (os.getenv("FORCE_AI_FALLBACK") or "false").lower() in {"1","true","yes"}

URGENT_SHEET_URL   = (os.getenv("URGENT_NEEDS_SHEET_CSV") or "").strip()
URGENT_JSON_PATH   = (os.getenv("URGENT_NEEDS_JSON") or "urgent_needs.json").strip()
CAMPAIGNS_JSON_PATH= (os.getenv("CAMPAIGNS_JSON") or "campaigns.json").strip()

# SMTP (إيميل)
SMTP_HOST   = (os.getenv("SMTP_HOST") or "").strip()
SMTP_PORT   = int(os.getenv("SMTP_PORT") or "465")
SMTP_USER   = (os.getenv("SMTP_USER") or "").strip()
SMTP_PASS   = (os.getenv("SMTP_PASS") or "").strip()
EMAIL_FROM  = (os.getenv("EMAIL_FROM") or SMTP_USER).strip()

client = None
if OPENAI_API_KEY and OpenAI:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        print(f"⚠️ فشل تهيئة OpenAI: {e}")
        client = None
else:
    if not OPENAI_API_KEY:
        print("⚠️ لم يتم العثور على OPENAI_API_KEY. سيتم العمل بوضع KB فقط.")

# ---------- Flask ----------
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
            note TEXT,
            channel TEXT,
            contact TEXT
        )
    """)
    conn.commit(); conn.close()

def save_log(raw_query, corrected_query, response_type, kb_source, bot_response):
    try:
        conn = sqlite3.connect(DB_NAME); c = conn.cursor()
        s = (bot_response or "")
        if len(s) > 800: s = s[:800] + "..."
        c.execute("""INSERT INTO logs (timestamp, raw_query, corrected_query, response_type, kb_source, bot_response)
                     VALUES (?, ?, ?, ?, ?, ?)""",
                  (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                   raw_query, corrected_query, response_type, kb_source, s))
        conn.commit()
    except Exception as e:
        print(f"⚠️ لم يُحفظ السجل: {e}")
    finally:
        try: conn.close()
        except: pass

# ---------- صفحات مساعدة ----------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "openai": bool(client),
        "model": OPENAI_MODEL,
        "urgent_sheet": bool(URGENT_SHEET_URL),
        "urgent_json": URGENT_JSON_PATH if os.path.exists(URGENT_JSON_PATH) else None,
        # فحص SMTP
        "smtp_ready": bool(SMTP_HOST and SMTP_USER and SMTP_PASS and EMAIL_FROM),
    })

# ---------- أدوات العربية ----------
_ARABIC_DIACRITICS_RE = re.compile(r"[\u0617-\u061A\u064B-\u0652\u0670\u0653-\u065F\u06D6-\u06ED]")
def normalize_arabic(text: str) -> str:
    if not text: return ""
    t = _ARABIC_DIACRITICS_RE.sub("", text)
    t = t.replace("أ","ا").replace("إ","ا").replace("آ","ا")
    t = t.replace("ؤ","و").replace("ئ","ي").replace("ة","ه")
    t = t.replace("ـ","")
    t = unicodedata.normalize("NFKC", t)
    t = re.sub(r"\s+"," ",t).strip()
    return t

def summarize_and_simplify(text, max_length=250):
    if not text or len(text) <= max_length: return text
    marks = ['.', '؟', '!', '…']
    trunc = text[: max_length - 5]
    cut = max(trunc.rfind(m) for m in marks)
    if cut == -1: cut = trunc.rfind(' ')
    if cut == -1: cut = len(trunc)
    return text[:cut].strip() + "\n\nهل ترغب بالتفصيل أكثر؟"

def oa_translate(text, target):
    if not client or not text: return text
    try:
        if target == 'ar':
            prompt = f"Translate to clear standard Arabic. Return only the translation:\n\n{text}"
        else:
            prompt = f"Translate this Arabic text to {target}. Return only the translation:\n\n{text}"
        r = client.chat.completions.create(model=OPENAI_MODEL, messages=[{"role":"user","content":prompt}])
        return (r.choices[0].message.content or "").strip()
    except Exception as e:
        print("⚠️ ترجمة:", e)
        return text

def oa_correct(text):
    if not client or not text: return text
    try:
        r = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role":"user","content":f"صحح الأخطاء الإملائية بالنص العربي وأعد النص فقط:\n\n{text}"}]
        )
        return (r.choices[0].message.content or "").strip()
    except Exception as e:
        print("⚠️ تصحيح:", e)
        return text

# ---------- قاعدة معرفية قصيرة ----------
KNOWLEDGE_BASE = {
    "ما هي شروط التبرع بالدم؟": {"answer": "العمر 18-60 سنة، الوزن ≥ 50 كجم، صحة جيدة، هيموغلوبين مناسب، لا وشم/ثقب آخر 6 أشهر، لا أمراض معدية. راجع المركز للتأكد.", "source":"KB"},
    "المدة الفاصلة بين التبرعات؟": {"answer": "الدم الكامل: 90 يومًا على الأقل. الصفائح/البلازما تختلف وقد تكون أقصر.", "source":"KB"},
    "هل التبرع بالدم مؤلم؟": {"answer": "الوخز لحظي وبسيط، السحب نفسه غير مؤلم عادةً ويستغرق دقائق.", "source":"KB"},
}

def search_kb(q):
    if not q: return None, None
    nq = normalize_arabic(q)
    qs = list(KNOWLEDGE_BASE.keys())
    norm_map = {k: normalize_arabic(k) for k in qs}
    vals = list(norm_map.values())

    p = process.extractOne(nq, vals, scorer=fuzz.partial_ratio)
    if p and p[1] >= 85:
        orig = next((k for k,v in norm_map.items() if v==p[0]), None)
        if orig: d = KNOWLEDGE_BASE[orig]; return d["answer"], d["source"]

    t = process.extractOne(nq, vals, scorer=fuzz.token_sort_ratio)
    if t and t[1] >= 80:
        orig = next((k for k,v in norm_map.items() if v==t[0]), None)
        if orig: d = KNOWLEDGE_BASE[orig]; return d["answer"], d["source"]
    return None, None

# ---------- الشات ----------
@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    msg = (data.get("message") or "").strip()
    want_detail = bool(data.get("detail"))

    if not msg:
        return jsonify({"answer":"اكتب سؤالك من فضلك."}), 200

    lang = "ar"
    try:
        lang = detect(msg)
    except LangDetectException:
        pass

    to_process = msg
    if lang != "ar" and client: to_process = oa_translate(msg, "ar")
    corrected = oa_correct(to_process) or to_process

    ans, src = search_kb(corrected)
    if ans:
        final = ans if want_detail else summarize_and_simplify(ans, 250)
        stype = "KB"
    else:
        if client and not FORCE_AI_FALLBACK:
            try:
                r = client.chat.completions.create(model=OPENAI_MODEL, messages=[{"role":"user","content":corrected}])
                text = (r.choices[0].message.content or "").strip()
                text = text if want_detail else summarize_and_simplify(text, 250)
                final = "لم نجد إجابة في القاعدة؛ استعنا بالذكاء الاصطناعي:\n\n" + text + "\n\n(مولَّد آليًا)"
                stype = "AI"; src = "AI"
            except Exception as e:
                final = f"عذرًا، تعذّر استخدام الذكاء الاصطناعي: {e}"
                stype = "Error"
        else:
            final = "عذرًا، لا توجد إجابة في القاعدة والمعالجة الآلية غير مفعّلة الآن."
            stype = "KB-Only"

    if lang != "ar" and client: final = oa_translate(final, lang)
    save_log(msg, corrected, stype, src, final)
    return jsonify({"answer":final, "source_type":stype, "source_text":src, "corrected_message":corrected}), 200

# ---------- Urgent Needs ----------
def _fetch_csv(url):
    import requests
    try:
        r = requests.get(url, timeout=6); r.raise_for_status()
        reader = csv.DictReader(StringIO(r.text))
        return [dict(row) for row in reader]
    except Exception as e:
        print("⚠️ فشل جلب CSV:", e); return None

def _load_json(path):
    try:
        if os.path.exists(path):
            with open(path,"r",encoding="utf-8") as f: return json.load(f)
    except Exception as e:
        print("⚠️ قراءة JSON:", e)
    return None

def gmaps_place_link(name):
    import urllib.parse as up
    return f"https://www.google.com/maps/search/?api=1&query={up.quote(name)}"

def _fmt_urgent(rows):
    out=[]
    for r in rows or []:
        h = r.get("hospital") or r.get("المستشفى") or r.get("Hospital") or ""
        st= r.get("status")   or r.get("الحالة")    or r.get("Status")   or ""
        dt= r.get("details")  or r.get("التفاصيل")  or r.get("Details")  or ""
        loc=r.get("location_url") or r.get("Location") or r.get("الموقع") or ""
        if h and not loc: loc = gmaps_place_link(h)
        if h: out.append({"hospital":h, "status":st, "details":dt, "location_url":loc})
    return out

FALLBACK_URGENT = [
    {"hospital":"مستشفى الملك فهد العام بجدة","status":"عاجل","details":"+O طوارئ","location_url":gmaps_place_link("King Fahd General Hospital Jeddah")},
    {"hospital":"بنك الدم الإقليمي – جدة","status":"مرتفع جداً","details":"نقص صفائح B-","location_url":gmaps_place_link("Jeddah Regional Laboratory and Blood Bank")},
    {"hospital":"مستشفى شرق جدة","status":"عاجل","details":"A- طوارئ","location_url":gmaps_place_link("East Jeddah Hospital Blood Bank")},
]

@app.route("/api/urgent_needs")
def urgent_needs():
    needs=None
    if URGENT_SHEET_URL:
        rows=_fetch_csv(URGENT_SHEET_URL)
        if rows: needs=_fmt_urgent(rows)
    if not needs:
        js=_load_json(URGENT_JSON_PATH)
        if isinstance(js,list): needs=_fmt_urgent(js)
    if not needs: needs=FALLBACK_URGENT
    return jsonify({
        "answer_ar":"احتياجات عاجلة للتبرع—يرجى الاتصال قبل الحضور.",
        "answer_en":"Urgent needs—please call before visiting.",
        "needs":needs,
        "updated_at": datetime.utcnow().isoformat()+"Z"
    })

# ---------- Eligibility (موسّع) ----------
BASE_WAIT_DAYS = 90  # دم كامل

@app.route("/api/eligibility/questions")
def eligibility_questions():
    return jsonify({"ok": True})

def evaluate_eligibility(payload: dict):
    """
    قواعد مبسّطة:
    - العمر ≥ 18
    - الوزن ≥ 50
    - آخر تبرع ≥ 90 يوم (دم كامل)
    - لا مضاد/سيولة/حمى/زكام حالياً
    - لا وشم/ثقب آخر 6 أشهر
    - إناث: لا حمل/رضاعة
    - سفر حديث لبعض المناطق → تأجيل مؤقت
    """
    reasons=[]
    eligible=True
    next_date=None

    age   = int(payload.get("age",0) or 0)
    weight= float(payload.get("weight",0) or 0)
    gender= (payload.get("gender") or "").lower()  # male/female/other
    last_days = int(payload.get("last_donation_days", 9999) or 9999)
    on_ac = bool(payload.get("on_anticoagulants", False))
    on_ab = bool(payload.get("on_antibiotics", False))
    has_cold = bool(payload.get("has_cold", False))
    pregnant = bool(payload.get("pregnant", False))
    breastfeeding = bool(payload.get("breastfeeding", False))
    recent_proc_days = int(payload.get("recent_procedure_days", 9999) or 9999)
    tattoo_months = int(payload.get("tattoo_months", 999) or 999)
    recent_travel = bool(payload.get("recent_travel", False))

    if age < 18: eligible=False; reasons.append("العمر أقل من 18.")
    if weight < 50: eligible=False; reasons.append("الوزن أقل من 50 كجم.")
    if last_days < BASE_WAIT_DAYS:
        eligible=False
        left = BASE_WAIT_DAYS - last_days
        next_date = (datetime.now()+timedelta(days=left)).strftime("%Y-%m-%d")
        reasons.append(f"لم يمض {BASE_WAIT_DAYS} يومًا منذ آخر تبرع. متاح بعد {left} يومًا ({next_date}).")

    if on_ac: eligible=False; reasons.append("أدوية السيولة تمنع التبرع مؤقتًا.")
    if on_ab: eligible=False; reasons.append("أجّل التبرع 7 أيام بعد آخر جرعة مضاد حيوي.")
    if has_cold: eligible=False; reasons.append("أعراض زكام/حمى—أجّل حتى التعافي 7 أيام.")
    if recent_proc_days < 7: eligible=False; reasons.append("إجراء/قلع أسنان حديث—انتظر 7 أيام.")
    if tattoo_months < 6: eligible=False; reasons.append("وشم/ثقب خلال آخر 6 أشهر—تأجيل مؤقت.")

    if gender == "female":
        if pregnant: eligible=False; reasons.append("الحمل يمنع التبرع—يستأنف بعد 6 أسابيع من الولادة/الإجهاض.")
        if breastfeeding: eligible=False; reasons.append("الرضاعة تمنع التبرع في بعض البروتوكولات—استشيري مركز الدم.")

    if recent_travel:
        eligible=False; reasons.append("سفر حديث قد يستلزم تأجيل مؤقت (حسب الإرشادات المحلية).")

    if not next_date:
        next_date = (datetime.now()+timedelta(days=BASE_WAIT_DAYS)).strftime("%Y-%m-%d")

    state = "eligible" if eligible else ("temporary" if reasons else "eligible")
    return eligible, reasons, next_date, state

@app.route("/api/eligibility/evaluate", methods=["POST"])
def elig_eval():
    payload = request.json or {}
    eligible, reasons, next_date, state = evaluate_eligibility(payload)
    return jsonify({"eligible":eligible, "reasons":reasons, "next_eligible_date":next_date, "state":state})

# ---------- Reminders ----------
@app.route("/api/reminder", methods=["POST"])
def reminder():
    payload = request.json or {}
    user_hint = (payload.get("user_hint") or "").strip() or "User"
    channel = (payload.get("channel") or "email").strip()
    contact = (payload.get("contact") or "").strip()  # email
    next_date = (datetime.now()+timedelta(days=BASE_WAIT_DAYS)).strftime("%Y-%m-%d")
    note = "Reminder for next eligible donation (whole blood)."
    conn = sqlite3.connect(DB_NAME); c = conn.cursor()
    c.execute("INSERT INTO reminders (created_at, user_hint, next_date, note, channel, contact) VALUES (?, ?, ?, ?, ?, ?)",
              (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_hint, next_date, note, channel, contact))
    conn.commit(); conn.close()
    return jsonify({"ok": True, "next_date": next_date})

def send_email(to_email: str, subject: str, body: str) -> (bool, str):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and EMAIL_FROM):
        return False, "SMTP غير مفعّل في الخادم."
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = EMAIL_FROM
        msg["To"] = to_email
        msg.set_content(body)
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
        return True, ""
    except Exception as e:
        return False, str(e)

@app.route("/api/reminder/email", methods=["POST"])
def reminder_email():
    data = request.json or {}
    to_email = (data.get("email") or "").strip()
    next_date = (data.get("next_date") or "").strip()
    if not to_email: return jsonify({"ok": False, "error":"البريد مطلوب."}), 400
    if not next_date: next_date = (datetime.now()+timedelta(days=BASE_WAIT_DAYS)).strftime("%Y-%m-%d")
    subject = "تذكير زمرة: موعد تبرعك القادم"
    body = f"أهلاً بك 👋\n\nتذكير زمرة: موعد تبرعك المقترح بتاريخ {next_date}.\nسنكون سعداء بزيارتك في أقرب بنك دم.\n\nمع التحية."
    ok, err = send_email(to_email, subject, body)
    return (jsonify({"ok":ok}) if ok else jsonify({"ok":False,"error":err}), 200 if ok else 500)

# ---------- حملات (اختياري) ----------
@app.route("/api/campaigns")
def campaigns():
    data=_load_json(CAMPAIGNS_JSON_PATH)
    if not data: return jsonify({"ok":False, "campaigns":[], "message":"لا يوجد ملف حملات"}), 200
    return jsonify({"ok":True, "campaigns":data}), 200

# ---------- Run ----------
if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
