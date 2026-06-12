LOGO_URL = "https://fs.getcourse.ru/fileservice/file/download/a/256825/sc/352/h/8ad55738e40c1fabbc97e7bcd909cb88.png"
SITE_URL = "https://zerocoder.ru/"
PHONE = "8-999-333-69-78"
LEGAL_NOTICE = 'РЕКЛАМА ООО "ЗЕРОКОДЕР"\nИНН 9715401631'

CHANNELS = {
    "email":           {"name": "Почта ГК",           "source": "email",    "medium": "zerocoder",        "content": "announce", "format": "email",    "email_variant_index": 0},
    "email_unisender": {"name": "Почта Unisender",    "source": "email",    "medium": "unisender",        "content": "announce", "format": "email",    "email_variant_index": 1, "strip_gc_vars": True},
    "tg_gc":           {"name": "ТГ бот (ГК)",        "source": "telegram", "medium": "zerocoder-gc",     "content": "announce", "format": "tg_html",  "tg_variant_index": 0},
    "tg_voronki":      {"name": "ТГ бот (Воронки)",   "source": "telegram", "medium": "zerocodity",       "content": "announce", "format": "tg_bots", "tg_variant_index": 1, "rename_first_name": True},
    "pomoshnik":       {"name": "Помощник по безопасности", "source": "telegram", "medium": "antiscam_zero", "content": "announce", "format": "tg_bots", "tg_variant_index": 1, "rename_first_name": True},
    "neurocat":        {"name": "Нейрокот",            "source": "telegram", "medium": "neurocat",         "content": "announce", "format": "tg_markdown", "tg_variant_index": 1, "strip_gc_vars": True},
    "max":             {"name": "Max",                 "source": "max",      "medium": "zerocoder",        "content": "announce", "format": "tg_html",  "tg_variant_index": 1},
    "tests":           {"name": "Тесты Зерокодер",    "source": "telegram", "medium": "zerocoder_it_test","content": "announce", "format": "tg_bots", "tg_variant_index": 1, "rename_first_name": True},
}
