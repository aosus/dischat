from __future__ import annotations

from typing import Final

from dischat.config import Locale

MESSAGES: Final[dict[str, dict[Locale, str]]] = {
    "errors.unknown_command": {
        "en": "Unknown command.",
        "ar": "الأمر غير معروف.",
    },
    "pairing.prompt_code": {
        "en": "Send the 6-digit pairing code, or /cancel.",
        "ar": "أرسل رمز الربط المكوّن من 6 أرقام، أو استخدم /cancel للإلغاء.",
    },
    "pairing.code_sent": {
        "en": "I sent a pairing code to that Discourse account.",
        "ar": "أرسلت رمز الربط إلى ذلك الحساب في ديسكورس.",
    },
    "pairing.cancelled": {
        "en": "Pairing cancelled.",
        "ar": "تم إلغاء الربط.",
    },
    "pairing.success": {
        "en": "Pairing complete.",
        "ar": "اكتمل ربط الحساب.",
    },
    "pairing.unpaired": {
        "en": "You are not paired.",
        "ar": "أنت غير مرتبط بأي حساب.",
    },
    "pairing.unpaired_success": {
        "en": "Pairing removed.",
        "ar": "تم فك ربط الحساب.",
    },
    "pairing.whoami": {
        "en": "Paired as {username}.",
        "ar": "الحساب المرتبط هو {username}.",
    },
    "pairing.invalid_code": {
        "en": "That code is invalid or expired.",
        "ar": "هذا الرمز غير صالح أو منتهي الصلاحية.",
    },
    "posting.requires_pairing": {
        "en": "I can’t post this to Discourse because this room requires pairing.\n\nUse:\n/pair <username>",
        "ar": "لا يمكنني نشر هذه الرسالة في ديسكورس لأن هذه الغرفة تتطلب ربط الحساب أولًا.\n\nاستخدم:\n/pair <username>",
    },
    "pairing.whoami.unpaired": {
        "en": "You are not paired.",
        "ar": "أنت غير مرتبط بأي حساب.",
    },
    "watch.category_list": {
        "en": "Available categories: {categories}",
        "ar": "الأقسام المتاحة: {categories}",
    },
    "watch.category_list_empty": {
        "en": "No categories are available to watch.",
        "ar": "لا توجد أقسام متاحة للمتابعة.",
    },
    "watch.added": {
        "en": "Now watching category {slug}.",
        "ar": "تمت متابعة القسم {slug}.",
    },
    "watch.all_added": {
        "en": "Now watching all public categories.",
        "ar": "تتم الآن متابعة كل الأقسام العامة.",
    },
    "watch.removed": {
        "en": "Stopped watching category {slug}.",
        "ar": "تم إيقاف متابعة القسم {slug}.",
    },
    "watch.all_removed": {
        "en": "Stopped watching all categories.",
        "ar": "تم إيقاف متابعة كل الأقسام.",
    },
    "watch.current": {
        "en": "Current watches: {watches}",
        "ar": "المتابعات الحالية: {watches}",
    },
    "watch.none": {
        "en": "You have no watches configured.",
        "ar": "لا توجد متابعات مضبوطة لهذا الحساب.",
    },
    "watch.unknown_category": {
        "en": "Unknown category slug.",
        "ar": "معرّف القسم غير معروف.",
    },
}


def translate(key: str, locale: str) -> str:
    translations = MESSAGES.get(key)
    if translations is None:
        return key
    if locale == "ar":
        return translations["ar"]
    return translations["en"]


def translate_format(key: str, locale: str, **kwargs: str) -> str:
    return translate(key, locale).format(**kwargs)
