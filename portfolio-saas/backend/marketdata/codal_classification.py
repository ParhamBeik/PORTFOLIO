"""Deterministic Codal letter-family classification and revision metadata."""

import re

from .models import CodalAnnouncement


LETTER_CATEGORY = {
    "let6": CodalAnnouncement.Category.STATEMENTS,
    "let8": CodalAnnouncement.Category.PORTFOLIO,
    "let11": CodalAnnouncement.Category.GENERAL,
    "let16": CodalAnnouncement.Category.ASSEMBLY_DECISION,
    "let17": CodalAnnouncement.Category.ASSEMBLY_DECISION,
    "let18": CodalAnnouncement.Category.ASSEMBLY_DECISION,
    "let19": CodalAnnouncement.Category.GOVERNANCE,
    "let20": CodalAnnouncement.Category.ASSEMBLY_DECISION,
    "let21": CodalAnnouncement.Category.ASSEMBLY_DECISION,
    "let22": CodalAnnouncement.Category.ASSEMBLY_DECISION,
    "let28": CodalAnnouncement.Category.CAPITAL_INCREASE,
    "let55": CodalAnnouncement.Category.CAPITAL_INCREASE,
    "let56": CodalAnnouncement.Category.AUDITOR_REPORT,
    "let58": CodalAnnouncement.Category.PRODUCTION_SALES,
    "let60": CodalAnnouncement.Category.GOVERNANCE,
    "let90": CodalAnnouncement.Category.AUDITOR_REPORT,
    "let128": CodalAnnouncement.Category.GENERAL,
    "let174": CodalAnnouncement.Category.STATEMENTS,
    "let248": CodalAnnouncement.Category.GOVERNANCE,
    "let260": CodalAnnouncement.Category.GOVERNANCE,
    "let2020": CodalAnnouncement.Category.ASSEMBLY_DECISION,
}

TITLE_CATEGORY = (
    (("امیدنامه", "پذیره نویسی", "عرضه عمومی"), CodalAnnouncement.Category.PROSPECTUS),
    (("شرکت فرعی", "تلفیقی", "زیرمجموعه"), CodalAnnouncement.Category.SUBSIDIARIES),
    (("حسابرسی", "حسابرس", "اظهارنظر"), CodalAnnouncement.Category.AUDITOR_REPORT),
    (("افزایش سرمایه", "ثبت سرمایه"), CodalAnnouncement.Category.CAPITAL_INCREASE),
    (("مجمع", "تقسیم سود"), CodalAnnouncement.Category.ASSEMBLY_DECISION),
    (("حاکمیت شرکتی", "کمیته", "کنترل داخلی"), CodalAnnouncement.Category.GOVERNANCE),
    (("فعالیت هیئت مدیره", "گزارش هیئت"), CodalAnnouncement.Category.BOARD_REPORT),
    (("پرتفوی", "سرمایه گذاری"), CodalAnnouncement.Category.PORTFOLIO),
    (("تولید و فروش", "فعالیت ماهانه"), CodalAnnouncement.Category.PRODUCTION_SALES),
    (("صورت مالی", "صورت سود", "ترازنامه", "جریان وجوه"), CodalAnnouncement.Category.STATEMENTS),
    (("افشای اطلاعات", "شفاف سازی", "شفاف‌سازی"), CodalAnnouncement.Category.GENERAL),
)


def _letter_type(announcement):
    match = re.search(r"let\d+", f"{announcement.code} {announcement.link}", re.I)
    return match.group(0).lower() if match else ""


def _period(title, fallback=""):
    matches = re.findall(r"1[34]\d{2}[-/]\d{1,2}[-/]\d{1,2}", title or "")
    if matches:
        return matches[-1].replace("/", "-")
    return fallback[:10]


def classify_announcement(announcement, parsed_text=""):
    title = f"{announcement.title or ''} {parsed_text[:4000]}"
    letter_type = _letter_type(announcement)
    category = LETTER_CATEGORY.get(letter_type)
    # let58 covers both operating issuers and investment companies.
    if letter_type == "let58" and any(word in title for word in ("پرتفوی", "سرمایه گذاری")):
        category = CodalAnnouncement.Category.PORTFOLIO
    if not category:
        category = next(
            (candidate for words, candidate in TITLE_CATEGORY if any(word in title for word in words)),
            None,
        )
    is_correction = any(word in title for word in ("اصلاحیه", "اصلاح", "جایگزین"))
    is_consolidated = any(word in title for word in ("تلفیقی", "گروه و شرکت"))
    audited = announcement.is_audited
    if audited is None:
        if "حسابرسی نشده" in title:
            audited = False
        elif any(word in title for word in ("حسابرسی شده", "گزارش حسابرس", "اظهارنظر حسابرس")):
            audited = True
    return {
        "category": category,
        "report_type": CodalAnnouncement.Category(category).label if category else "unknown",
        "letter_type": letter_type,
        "period_end": _period(title, announcement.date_title),
        "is_audited": audited,
        "is_consolidated": is_consolidated,
        "is_correction": is_correction,
    }
