from locale import de
from locale.base import BaseTranslation


__base_t = BaseTranslation()
__translations = {
    "en": __base_t,
    "de": de.Translation(),
}


def get_translation(request):
    for lang in request.headers.get("Accept-Language", "").split(","):
        (lang, *_) = lang.split(";", 1)
        if not lang:
            continue
        t = __translations.get(lang)
        if t:
            return t
    return __base_t
