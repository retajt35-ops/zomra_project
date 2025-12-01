/* ============================================
   ZOMRA - Translation Dictionary
   النظام الهجين C
==============================================*/

window.Z_LANG = {
  current: "ar", // default

  setLang(lang) {
    this.current = lang;
    document.documentElement.lang = lang;
    document.documentElement.dir = lang === "ar" ? "rtl" : "ltr";
    document.dispatchEvent(new CustomEvent("langChanged", { detail: lang }));
  },

  t(key) {
    return this.strings[this.current][key] || key;
  },

  strings: {
    // ==========================
    // ======== Arabic ==========
    // ==========================
    ar: {
      chat_title: "محادثة مع زمرة 🩸",
      input_placeholder: "اكتب سؤالك هنا...",
      send: "إرسال",
      recording: "إيقاف التسجيل",
      mic: "إرسال صوتي",

      // Sidebar buttons
      urgent: "الاحتياج العاجل للدم",
      eligibility: "فحص الأهلية",
      reminder: "تذكير بموعد التبرع",
      locate_center: "حدد موقعي وأظهر المراكز",

      // Map filters
      search_placeholder: "ابحث باسم المستشفى أو الحي...",
      sector_all: "القطاع: الكل",
      sector_public: "حكومي فقط",
      sector_private: "خاص فقط",
      apply_filters: "تطبيق",
      reset_filters: "إعادة تعيين",
      nearest_center: "أقرب مركز الآن",

      faq1: "شروط التبرع الأساسية",
      faq2: "المدة الفاصلة بين التبرعات",
      faq3: "هل التبرع بالدم مؤلم؟",

      // Urgent needs
      urgent_title: "الاحتياج العاجل للدم (جدة وما حولها)",
      urgent_note: "يرجى الاتصال قبل الزيارة.",

      // Eligibility
      elig_title: "نموذج فحص الأهلية",
      yes: "نعم",
      no: "لا",
      male: "ذكر",
      female: "أنثى",
      age: "العمر",
      weight: "الوزن",
      last_donation: "آخر تبرع (بالأيام)",
      ac_meds: "أدوية سيولة الدم؟",
      ab_meds: "مضاد حيوي لعدوى نشطة؟",
      cold: "أعراض زكام/حمى؟",
      pregnant: "هل أنتِ حامل؟",
      recent_proc: "هل أجريت عملية/قلع أسنان؟",
      months_since: "كم شهرًا مضى؟",
      tattoo: "هل لديك وشم أو ثقب؟",
      eval_btn: "قيّم الأهلية",
      result_ok: "مؤهل للتبرع",
      result_bad: "غير مؤهل حاليًا",
      next_date: "أقرب موعد مناسب:",

      // Chat / Details
      details_more: "تفاصيل أكثر",
      details_less: "إظهار أقل",
      translate_btn_show: "عرض الترجمة الإنجليزية",
      translate_btn_hide: "إخفاء الترجمة",

      // Bot system messages
      bot_typing: "زمرة تكتب...",
      location_done: "تم تحديد موقعك.",
      location_fail: "فشل تحديد الموقع.",
      audio_msg: "(رسالة صوتية)",
    },

    // ==========================
    // ===== English ============
    // ==========================
    en: {
      chat_title: "Chat with Zomrah 🩸",
      input_placeholder: "Type your question...",
      send: "Send",
      recording: "Stop Recording",
      mic: "Voice Message",

      urgent: "Urgent Blood Need",
      eligibility: "Eligibility Check",
      reminder: "Donation Reminder",
      locate_center: "Find My Location & Show Centers",

      search_placeholder: "Search hospital or district...",
      sector_all: "Sector: All",
      sector_public: "Public Only",
      sector_private: "Private Only",
      apply_filters: "Apply",
      reset_filters: "Reset",
      nearest_center: "Nearest Center Now",

      faq1: "Basic donation requirements",
      faq2: "Donation interval",
      faq3: "Is blood donation painful?",

      urgent_title: "Critical Blood Need (Jeddah Area)",
      urgent_note: "Please contact the hospital before visiting.",

      elig_title: "Eligibility Assessment Form",
      yes: "Yes",
      no: "No",
      male: "Male",
      female: "Female",
      age: "Age",
      weight: "Weight",
      last_donation: "Last donation (days)",
      ac_meds: "Taking anticoagulants?",
      ab_meds: "Taking antibiotics?",
      cold: "Cold/fever symptoms?",
      pregnant: "Are you pregnant?",
      recent_proc: "Recent surgery/dental extraction?",
      months_since: "How many months ago?",
      tattoo: "Recent tattoo or piercing?",
      eval_btn: "Evaluate",
      result_ok: "Eligible to donate",
      result_bad: "Not eligible right now",
      next_date: "Next suitable date:",

      details_more: "Show More",
      details_less: "Show Less",
      translate_btn_show: "Show Arabic Translation",
      translate_btn_hide: "Hide Arabic Translation",

      bot_typing: "Zomrah is typing...",
      location_done: "Location detected.",
      location_fail: "Failed to detect location.",
      audio_msg: "(Voice Message)",
    }
  }
};
