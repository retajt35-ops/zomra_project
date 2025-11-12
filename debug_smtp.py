# debug_smtp.py
# تشغيل: python debug_smtp.py
# هذا السكربت يجرب إرسال إيميل ويطبع كل تفاصيل الاتصال والاستجابة لتساعد على تشخيص أخطاء SMTP (مثل 535 Bad Credentials).

import os, smtplib, traceback
from email.message import EmailMessage
from datetime import datetime

# قراءة المتغيرات من البيئة (أو استبدل القيم هنا مؤقتاً للاختبار)
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER or "")
SMTP_TLS  = (os.getenv("SMTP_TLS", "true").lower() in ("1","true","yes"))

DEBUG_TO = os.getenv("DEBUG_SMTP_TO", SMTP_USER or "")  # من سيرسل له رسالة الاختبار

def make_message(to_addr):
    msg = EmailMessage()
    msg["From"] = SMTP_FROM or "debug@local"
    msg["To"]   = to_addr
    msg["Subject"] = f"[Zomrah DEBUG] Test email {datetime.utcnow().isoformat()}"
    msg.set_content("هذه رسالة اختبار من debug_smtp.py للتحقق من إعدادات SMTP.")
    return msg

def run_test():
    print("=== SMTP DEBUG START ===")
    print(f"HOST={SMTP_HOST} PORT={SMTP_PORT} USER={'(provided)' if SMTP_USER else '(none)'} FROM={SMTP_FROM} TLS={SMTP_TLS}")
    if not DEBUG_TO:
        print("!! لم تحدد مستلم الاختبار. اضف DEBUG_SMTP_TO في البيئة أو اجعل SMTP_USER قيمة.")
        return

    msg = make_message(DEBUG_TO)

    try:
        print("فتح اتصال SMTP...")
        server = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
        server.set_debuglevel(1)   # هذا يطبع الحوار الكامل مع الخادم
        ehlo = server.ehlo()
        print("EHLO response:", ehlo)

        if SMTP_TLS:
            print("بدء TLS (STARTTLS)...")
            starttls_resp = server.starttls()
            print("STARTTLS response:", starttls_resp)
            server.ehlo()

        if SMTP_USER:
            print("محاولة تسجيل الدخول (login)...")
            try:
                server.login(SMTP_USER, SMTP_PASS)
                print("تم تسجيل الدخول بنجاح.")
            except smtplib.SMTPAuthenticationError as auth_err:
                print("!!! SMTPAuthenticationError أثناء login !!!")
                print("رمز الخطأ/الرسالة من الخادم:", auth_err)
                # اطبع الستاك تريس الكامل
                traceback.print_exc()
                server.quit()
                return
            except Exception as e:
                print("!!! استثناء أثناء login: ", e)
                traceback.print_exc()
                server.quit()
                return
        else:
            print("لم يتم تمرير بيانات مستخدم (SMTP_USER فارغ) — سيتم محاولة إرسال بدون مصادقة.")

        print(f"إرسال رسالة اختبار إلى {DEBUG_TO} ...")
        server.send_message(msg)
        print("تم الإرسال (لم يعطِ استثناء).")
        server.quit()
        print("تم إغلاق الاتصال بنجاح.")
    except Exception as e:
        print("!!! استثناء عام أثناء الاتصال/الإرسال !!!")
        traceback.print_exc()
    finally:
        print("=== SMTP DEBUG END ===")

if __name__ == "__main__":
    run_test()
