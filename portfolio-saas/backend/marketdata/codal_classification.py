"""Title-based classification of Codal announcements into a document type and tier.

BrsApi omits `category` on 98.7% of the warehouse (75,663 of 76,666 rows), so this
module recovers a usable signal from the Persian title text, which is highly
structured. The provider's own `category`, when present, always wins over the
title guess -- it is ground truth from BrsApi, not an inference -- and the source
that decided is recorded (`classified_by`) so a later, better title rule can be
told apart from a provider-confirmed row.

Tiering is owner-confirmed for the keywords named in the task:
  TIER 1 -- financial statements (audited/unaudited), monthly production &
            sales, interim/quarterly reports.
  TIER 2 -- auditor opinion, AGM decisions, capital increase, clarifications.
  TIER 3 -- meeting notices, address changes, everything else.
A few provider `category` values (4, 8, 9, 11) and a few common title buckets
(board activity report, material disclosure, dividend schedule, ...) are not
covered by the owner's list; they are given their own `doc_type` label (so the
coverage report can see them separately) with a best-effort tier, marked
"inferred" below -- flag these for owner review before trusting the tier.
"""
from __future__ import annotations

import re
from typing import Callable, NamedTuple, Optional

from marketdata.jalali import fold_digits

TIER_1 = 1  # financial statements, production & sales, interim reports
TIER_2 = 2  # auditor opinion, AGM decisions, capital increase, clarifications
TIER_3 = 3  # meeting notices, address changes, everything else

_LETTER_VARIANTS = str.maketrans({
    "ي": "ی",  # ARABIC YEH -> PERSIAN YEH (ي -> ی)
    "ك": "ک",  # ARABIC KAF -> PERSIAN KEHEH (ك -> ک)
})
_ZWNJ = "‌"
_WS_RE = re.compile(r"\s+")


def normalize_title(text: str) -> str:
    """Canonicalize Persian text for keyword matching.

    Unifies Arabic/Persian letter variants, drops the ZWNJ so a word split
    across it (`صورت‌های` -> `صورتهای`) reads as one token, folds Persian/
    Arabic-Indic digits to ASCII, and collapses whitespace.
    """
    if not text:
        return ""
    text = fold_digits(text).translate(_LETTER_VARIANTS)
    text = text.replace(_ZWNJ, "")
    return _WS_RE.sub(" ", text).strip()


class Classification(NamedTuple):
    doc_type: str
    tier: int
    classified_by: str  # "category" | "title" | "default"


# Provider `category` -> (doc_type, tier). Keyed on
# marketdata.models.CodalAnnouncement.Category. 3 and 5 are the two values the
# owner confirmed against real data; the rest are a reasonable first pass.
CATEGORY_RULES: dict[int, tuple[str, int]] = {
    1: ("general_disclosure", TIER_3),      # General Disclosures
    2: ("financial_statements", TIER_1),    # Periodic Financial Statements
    3: ("production_sales", TIER_1),        # Monthly Production & Sales -- confirmed
    4: ("board_activity_report", TIER_2),   # Board of Directors Report -- inferred
    5: ("auditor_opinion", TIER_2),         # Auditor Notes & Opinion -- confirmed
    6: ("agm_decision", TIER_2),            # General Assembly Decision
    7: ("capital_increase", TIER_2),        # Capital Increase Announcement
    8: ("investment_portfolio", TIER_2),    # Monthly Investment Portfolio -- inferred
    9: ("governance", TIER_3),              # Corporate Governance -- inferred
    10: ("financial_statements", TIER_1),   # Subsidiary Financial Statements
    11: ("prospectus", TIER_2),             # IPO & Bond Prospectus -- inferred
}


def _has(*keywords: str) -> Callable[[str], bool]:
    return lambda t: any(k in t for k in keywords)


def _has_all(*keywords: str) -> Callable[[str], bool]:
    return lambda t: all(k in t for k in keywords)


# Ordered (doc_type, tier, predicate) rules, evaluated top to bottom; first
# match wins. Mostly Tier 1 -> Tier 2 -> Tier 3, EXCEPT the clarification
# rule jumps the queue: a title can legitimately carry both a clarification
# phrase and a financial-statements phrase (owner example: "توضیحات در خصوص
# اطلاعات و صورت های مالی منتشر شده" is commentary ABOUT financials, not the
# statements themselves), and the more specific "this is commentary" signal
# has to win over the generic substring match or every such title would be
# misfiled as Tier 1.
TITLE_RULES: list[tuple[str, int, Callable[[str], bool]]] = [
    # Clarification/explanation titles are checked first: "توضیحات در خصوص
    # اطلاعات و صورت های مالی منتشر شده" (owner example) is commentary ABOUT
    # published financials, not the statements themselves, even though it
    # contains the "صورت های مالی" substring the Tier 1 rule below matches on.
    ("clarification", TIER_2, _has("شفاف سازی", "توضیحات در خصوص")),
    # --- Tier 1: financial substance ---
    ("interim_financials", TIER_1, _has("میاندوره")),
    ("financial_statements", TIER_1, _has("صورتهای مالی", "صورت های مالی")),
    ("production_sales", TIER_1, _has("تولید و فروش", "فعالیت ماهانه")),
    # --- Tier 2: material but not primary financials ---
    ("auditor_opinion", TIER_2, _has("اظهارنظر حسابرس")),
    ("agm_decision", TIER_2, _has("تصمیمات مجمع")),
    ("capital_increase", TIER_2, _has("افزایش سرمایه")),
    # --- Tier 3: procedural / low-signal, but named for coverage reporting
    #     instead of falling into an undifferentiated "other" bucket ---
    ("meeting_notice", TIER_3, _has("آگهی دعوت")),
    ("meeting_notice", TIER_3, _has_all("دعوت", "مجمع")),
    ("address_change", TIER_3, _has("تغییر نشانی")),
    ("compliance_notice", TIER_3, _has("دستورالعمل پذیرش")),
    # Owner-confirmed 2026-08-14 as TIER 2, not the Tier-3 default: Codal's
    # channel for market-moving news (contract signings, lawsuit outcomes,
    # tender results, major asset sales). ~6% of all announcements, so its
    # placement materially changes what the tiered fetch prioritises.
    ("material_disclosure", TIER_2, _has("افشای اطلاعات بااهمیت")),
    ("dividend_schedule", TIER_3, _has("زمانبندی پرداخت سود")),
    ("board_change", TIER_3, _has("ترکیب اعضای هیئت مدیره")),
    ("board_activity_report", TIER_3, _has("فعالیت هیئت مدیره")),
    ("committee", TIER_3, _has("کمیته")),
    ("symbol_halt", TIER_3, _has("توقف نماد", "تعلیق نماد")),
    ("investment_portfolio", TIER_3, _has("صورت وضعیت پورتفوی")),
]


def classify(
    title: str, category: Optional[int] = None, category_title: str = ""
) -> Classification:
    """Classify one Codal announcement into (doc_type, tier, classified_by).

    `category` wins whenever BrsApi supplied one -- it is not a guess. Only the
    ~99% of rows where the provider omitted it fall through to the title regex,
    and any title that matches nothing lands safely in Tier 3 ("other") rather
    than raising.
    """
    if category is not None and category in CATEGORY_RULES:
        doc_type, tier = CATEGORY_RULES[category]
        return Classification(doc_type, tier, "category")

    normalized = normalize_title(title or "")
    for doc_type, tier, predicate in TITLE_RULES:
        if predicate(normalized):
            return Classification(doc_type, tier, "title")

    return Classification("other", TIER_3, "default")


# --- Structural metadata for CodalReport (period_end, audited/consolidated/
# correction flags, letter_type) -- a different concern from classify()'s
# doc_type/tier above, so it lives alongside rather than merged into it: this
# feeds CodalReport fields the pipeline needs to group revisions and gate
# publication, not the coverage-report doc_type bucket. Restored from the
# pipeline stripped in commit 2ea22be.
from .models import CodalAnnouncement as _CodalAnnouncement

_LETTER_CATEGORY = {
    "let6": _CodalAnnouncement.Category.STATEMENTS,
    "let8": _CodalAnnouncement.Category.PORTFOLIO,
    "let11": _CodalAnnouncement.Category.GENERAL,
    "let16": _CodalAnnouncement.Category.ASSEMBLY_DECISION,
    "let17": _CodalAnnouncement.Category.ASSEMBLY_DECISION,
    "let18": _CodalAnnouncement.Category.ASSEMBLY_DECISION,
    "let19": _CodalAnnouncement.Category.GOVERNANCE,
    "let20": _CodalAnnouncement.Category.ASSEMBLY_DECISION,
    "let21": _CodalAnnouncement.Category.ASSEMBLY_DECISION,
    "let22": _CodalAnnouncement.Category.ASSEMBLY_DECISION,
    "let28": _CodalAnnouncement.Category.CAPITAL_INCREASE,
    "let55": _CodalAnnouncement.Category.CAPITAL_INCREASE,
    "let56": _CodalAnnouncement.Category.AUDITOR_REPORT,
    "let58": _CodalAnnouncement.Category.PRODUCTION_SALES,
    "let60": _CodalAnnouncement.Category.GOVERNANCE,
    "let90": _CodalAnnouncement.Category.AUDITOR_REPORT,
    "let128": _CodalAnnouncement.Category.GENERAL,
    "let174": _CodalAnnouncement.Category.STATEMENTS,
    "let248": _CodalAnnouncement.Category.GOVERNANCE,
    "let260": _CodalAnnouncement.Category.GOVERNANCE,
    "let2020": _CodalAnnouncement.Category.ASSEMBLY_DECISION,
}

_TITLE_CATEGORY = (
    (("امیدنامه", "پذیره نویسی", "عرضه عمومی"), _CodalAnnouncement.Category.PROSPECTUS),
    (("گزارش حسابرس", "اظهارنظر حسابرس", "حسابرس مستقل"), _CodalAnnouncement.Category.AUDITOR_REPORT),
    (("افزایش سرمایه", "ثبت سرمایه"), _CodalAnnouncement.Category.CAPITAL_INCREASE),
    (("مجمع", "تقسیم سود"), _CodalAnnouncement.Category.ASSEMBLY_DECISION),
    (("حاکمیت شرکتی", "کمیته", "کنترل داخلی"), _CodalAnnouncement.Category.GOVERNANCE),
    (("فعالیت هیئت مدیره", "گزارش هیئت"), _CodalAnnouncement.Category.BOARD_REPORT),
    (("پرتفوی", "سرمایه گذاری"), _CodalAnnouncement.Category.PORTFOLIO),
    (("تولید و فروش", "فعالیت ماهانه"), _CodalAnnouncement.Category.PRODUCTION_SALES),
    (("صورت مالی", "صورتهای مالی", "صورت های مالی", "صورت سود", "ترازنامه", "جریان وجوه"), _CodalAnnouncement.Category.STATEMENTS),
    (("شرکت فرعی", "زیرمجموعه"), _CodalAnnouncement.Category.SUBSIDIARIES),
    (("افشای اطلاعات", "شفاف سازی", "شفاف‌سازی"), _CodalAnnouncement.Category.GENERAL),
)


def _letter_type(announcement):
    match = re.search(r"let\d+", f"{announcement.code} {announcement.link}", re.I)
    return match.group(0).lower() if match else ""


def _period(title, fallback=""):
    import jdatetime

    for source in (normalize_title(title), normalize_title(fallback)):
        matches = re.findall(r"(?<!\d)(1[34]\d{2})[-/](\d{1,2})[-/](\d{1,2})(?!\d)", source)
        for year, month, day in reversed(matches):
            try:
                parsed = jdatetime.date(int(year), int(month), int(day))
            except ValueError:
                continue
            return f"{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}"
    return ""


def classify_announcement(announcement, parsed_text=""):
    """Structural metadata for one CodalReport: category, period, revision flags.

    `category` here is the provider's numeric Category (1-11), derived from
    the letter-code in the announcement's URL/code when present (ground
    truth) and title keywords otherwise -- independent of `classify()`'s
    doc_type/tier above, which serves a different consumer (coverage report).
    """
    # Body text contains references to other reports and subsidiaries. It must
    # not change the identity, period, or correction status of this notice.
    title = normalize_title(announcement.title or "")
    letter_type = _letter_type(announcement)
    category = _LETTER_CATEGORY.get(letter_type)
    # let58 covers both operating issuers and investment companies.
    if letter_type == "let58" and any(word in title for word in ("پرتفوی", "سرمایه گذاری")):
        category = _CodalAnnouncement.Category.PORTFOLIO
    if not category:
        category = next(
            (candidate for words, candidate in _TITLE_CATEGORY if any(word in title for word in words)),
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
        "report_type": _CodalAnnouncement.Category(category).label if category else "unknown",
        "letter_type": letter_type,
        "period_end": _period(title, announcement.date_title),
        "is_audited": audited,
        "is_consolidated": is_consolidated,
        "is_correction": is_correction,
    }


if __name__ == "__main__":
    # ponytail: smallest runnable check for a pure-function module -- the real
    # coverage lives in tests/test_codal_classification.py.
    assert classify("صورت‌های مالی سال مالی منتهی به ۱۴۰۴/۱۲/۲۹ (حسابرسی نشده)") == (
        "financial_statements", TIER_1, "title",
    )
    assert classify("آگهی دعوت به مجمع عمومی عادی سالیانه نوبت دوم") == (
        "meeting_notice", TIER_3, "title",
    )
    assert classify("قزوز", category=5) == ("auditor_opinion", TIER_2, "category")
    assert classify("چیز عجیب و غریب") == ("other", TIER_3, "default")
    print("codal_classification self-check OK")
