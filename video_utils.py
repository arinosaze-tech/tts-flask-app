# video_utils.py
# -------------------------------------------------------------
# Video & Image utilities (Pixabay + Unsplash + FFmpeg slideshow)
# v4: Drop‑in with lightweight NLP + dual provider (Pixabay & Unsplash)
#  - Multilingual NLP (EN/FR/DE/FA), fuzzy matching (char‑trigram)
#  - Dynamic scenario-term mining from local .txt files (optional)
#  - Multi-query fallback + domain anchors
#  - **NEW:** Unsplash search (alongside Pixabay) with combined re‑ranking.
#  - Picks the *best* and most relevant image across both providers.
#  - Public API unchanged:
#       sentence_to_query(), sentence_to_query_extras(),
#       get_images_for_cues(), pixabay_search_and_download(),
#       build_slideshow_video_cfr(), mux_subs_and_audio_on_video(),
#       render_video_single_or_none()
# -------------------------------------------------------------

import subprocess, hashlib, os, string, re, unicodedata, math, random, json, collections
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Set

import requests
from requests.adapters import HTTPAdapter, Retry

# --- Load settings from settings_temp (GUI) if available; else settings.py ---
from pathlib import Path

try:
    import settings_temp as _s
except Exception:
    import settings as _s

# Safe defaults (used if a key is missing)
PIXABAY_SAFESEARCH   = getattr(_s, "PIXABAY_SAFESEARCH", "true")  # Pixabay expects "true"/"false"
AUTO_IMAGE_LANG      = getattr(_s, "AUTO_IMAGE_LANG", "auto")
IMAGES_PER_SENTENCE  = int(getattr(_s, "IMAGES_PER_SENTENCE", 1))
IMAGE_TIMEOUT        = int(getattr(_s, "IMAGE_TIMEOUT", 12))
IMAGE_RETRIES        = int(getattr(_s, "IMAGE_RETRIES", 3))

CACHE_IMG_DIR        = getattr(_s, "CACHE_IMG_DIR", Path(".cache_images"))
CACHE_VIDEO_DIR      = getattr(_s, "CACHE_VIDEO_DIR", Path(".cache_video"))

SAMPLE_RATE          = int(getattr(_s, "SAMPLE_RATE", 48000))
CHANNELS             = int(getattr(_s, "CHANNELS", 2))
VIDEO_SIZE           = str(getattr(_s, "VIDEO_SIZE", "1920x1080"))
VIDEO_FPS            = int(getattr(_s, "VIDEO_FPS", 30))

# Ensure caches exist
CACHE_IMG_DIR.mkdir(parents=True, exist_ok=True)
CACHE_VIDEO_DIR.mkdir(parents=True, exist_ok=True)


# ------------------------------- #
#            Config               #
# ------------------------------- #
class NLPConfig:
    MAX_QUERY_TOKENS = 6
    TRIGRAM_MATCH_THRESHOLD = 0.62  # typo-tolerant lexicon match
    TRIGRAM_TAG_THRESHOLD = 0.25    # tag similarity boost
    FREQ_MIN_SCENARIO = 2           # ignore ultra-rare mined tokens
    MAX_FALLBACKS = 8               # how many query fallbacks to try

class ProviderConfig:
    PIXABAY_PER_PAGE = 40
    UNSPLASH_PER_PAGE = 30
    ORIENTATION = "landscape"       # for Unsplash
    CONTENT_FILTER = "high"         # for Unsplash

# ------------------------------- #
#       Stopwords / Hints         #
# ------------------------------- #
STOPWORDS_EN = {
    "the","a","an","and","or","but","in","on","at","for","with","to","of","is","are","was","were",
    "this","that","these","those","please","can","could","you","i","it","my","your","me","we","they",
    "do","have","has","am","be","been","being","will","would","should","could","may","might",
    "make","made","take","bring","need","want","like","show","explain","print","charge","put","add",
    "less","more","without","no","not","isn","aren","does","did","doesn","don","cannot","ok",
    "size","small","medium","large","here","there","set","are","card","cash","by","from","with"
}
STOPWORDS_FR = {
    "je","tu","il","elle","nous","vous","ils","elles","de","des","du","le","la","les",
    "un","une","et","ou","mais","dans","en","au","aux","avec","pour","par","sur","sous",
    "ce","cet","cette","ces","mon","ma","mes","ton","ta","tes","son","sa","ses","leur","leurs",
    "est","suis","es","sommes","êtes","sont","ne","pas","que","qui","quoi","où","quand","comment",
    "aujourd’hui","d","l","n","c","j","t","s","moi","toi","lui","elle","leur","y","en","bien",
    "svp","s’il","vous","plaît","veuillez","faire","mettre","prendre","apporter","besoin","voudrais",
    "taille","petite","moyenne","grande","ici","là","sur","place","à","emporter","carte","espèces"
}
STOPWORDS_DE = {
    "der","die","das","ein","eine","einen","einem","einer","und","oder","aber","in","auf","an","bei",
    "für","mit","zu","von","ist","sind","war","waren","bitte","kann","können","könnten","sie","ich","es",
    "mein","meine","dein","deine","ihr","ihre","unser","unsere","dies","das","diese","jene",
    "machen","nehmen","bringen","brauche","möchte","mag","zeigen","erklären","drucken","belasten","geben",
    "weniger","mehr","ohne","nicht","größe","kleine","mittlere","große","hier","da","karte","bar"
}
STOPWORDS_FA = {"و","در","به","از","که","این","آن","برای","با","یا","اما","یک","هم","را","تا","ما","شما","او","ایشان","همه","هر","لطفاً","خواهش","می","شود","کنید","است","هستم","هستید"}

NEGATORS = {"no","not","without","ohne","sans","بدون","نیست","نکن"}

def _strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')

def _normalize_text(s: str) -> str:
    s = s.replace("’","'").replace("‑","-").replace("–","-").replace("—","-")
    s = _strip_accents(s.lower())
    s = s.translate(str.maketrans("", "", string.punctuation))
    s = re.sub(r"\s+", " ", s).strip()
    return s

def guess_lang(text: str) -> str:
    t = text
    if re.search(r"[اآبپتثجچحخدذرزژسشصضطظعغفقکگلمنوهی]", t):
        return "fa"
    if any(w in t.lower() for w in [" le "," la "," les "," une "," un "," je ","vous ","s'il "]):
        return "fr"
    if any(w in t.lower() for w in [" der "," die "," das "," ich "," bitte ","karte "]):
        return "de"
    return "en"

def _language_stopwords(lang: str):
    if lang.startswith("fr"):
        return STOPWORDS_FR
    if lang.startswith("de"):
        return STOPWORDS_DE
    if lang.startswith("fa"):
        return STOPWORDS_FA
    return STOPWORDS_EN

def _tokenize(text: str, lang: str) -> List[str]:
    t = _normalize_text(text)
    words = t.split()
    sw = _language_stopwords(lang)
    return [w for w in words if len(w) > 2 and w not in sw]

# ------------------------------- #
#        Char‑trigram utils       #
# ------------------------------- #
def _trigrams(s: str, n: int = 3) -> Set[str]:
    s = f"  {s}  "
    return set(s[i:i+n] for i in range(len(s)-n+1))

def _tri_sim(a: str, b: str) -> float:
    A, B = _trigrams(_normalize_text(a)), _trigrams(_normalize_text(b))
    if not A or not B:
        return 0.0
    inter = len(A & B)
    union = len(A | B)
    return inter / union

# ------------------------------- #
#        Domain Lexicon           #
# ------------------------------- #
LEXICON: Dict[str, Dict[str, List[str]]] = {
    # ---- Cafe / Food & Drinks ----
    "espresso": {"en": ["espresso"], "fr": ["expresso","espresso"], "de": ["espresso"], "fa": ["اسپرسو"]},
    "americano": {"en": ["americano"], "fr": ["allonge","americano"], "de": ["americano"], "fa": ["آمریکانو"]},
    "cappuccino": {"en": ["cappuccino"], "fr": ["cappuccino"], "de": ["cappuccino"], "fa": ["کاپوچینو"]},
    "latte": {"en": ["latte"], "fr": ["latte"], "de": ["latte"], "fa": ["لاته"]},
    "flat white": {"en": ["flat white"], "fr": ["flat white"], "de": ["flat white"], "fa": ["فلت وایت","فلت وايت"]},
    "black coffee": {"en": ["black coffee"], "fr": ["cafe noir"], "de": ["schwarzer kaffee"], "fa": ["قهوه ساده","قهوه سیاه"]},
    "green tea": {"en": ["green tea"], "fr": ["the vert"], "de": ["gruner tee","gruener tee","grüner tee"], "fa": ["چای سبز"]},
    "herbal tea": {"en": ["herbal tea"], "fr": ["tisane"], "de": ["krautertee","kräutertee"], "fa": ["دمنوش","چای گیاهی"]},
    "hot chocolate": {"en": ["hot chocolate"], "fr": ["chocolat chaud"], "de": ["heisse schokolade","heiße schokolade"], "fa": ["هات چاکلت","شکلات داغ"]},
    "croissant": {"en": ["croissant"], "fr": ["croissant"], "de": ["croissant"], "fa": ["کرواسان"]},
    "sandwich": {"en": ["sandwich"], "fr": ["sandwich"], "de": ["sandwich"], "fa": ["ساندویچ"]},
    "menu": {"en": ["menu"], "fr": ["menu","carte"], "de": ["speisekarte"], "fa": ["منو"]},
    "bill": {"en": ["bill","check"], "fr": ["addition"], "de": ["rechnung"], "fa": ["صورت حساب","فاکتور"]},
    "receipt": {"en": ["receipt"], "fr": ["recu","reçu"], "de": ["beleg","quittung"], "fa": ["رسید"]},

    # ---- Pharmacy / Health ----
    "cough syrup": {"en": ["cough syrup"], "fr": ["sirop contre la toux"], "de": ["hustensirup"], "fa": ["شربت سرفه"]},
    "pain tablets": {"en": ["pain tablets","painkillers"], "fr": ["comprimés contre la douleur"], "de": ["schmerztabletten"], "fa": ["قرص مسکن"]},
    "nasal spray": {"en": ["nasal spray"], "fr": ["spray nasal"], "de": ["nasenspray"], "fa": ["اسپری بینی"]},
    "thermometer": {"en": ["thermometer"], "fr": ["thermometre","thermomètre"], "de": ["thermometer"], "fa": ["دماسنج"]},
    "vitamins": {"en": ["vitamins"], "fr": ["vitamines"], "de": ["vitamine"], "fa": ["ویتامین"]},
    "pharmacy interior": {"en": ["pharmacy"], "fr": ["pharmacie"], "de": ["apotheke"], "fa": ["داروخانه"]},

    # ---- Airport / Travel ----
    "passport": {"en": ["passport"], "fr": ["passeport"], "de": ["reisepass"], "fa": ["پاسپورت","گذرنامه"]},
    "boarding pass": {"en": ["boarding pass"], "fr": ["carte dembarquement","carte d embarquement"], "de": ["bordkarte"], "fa": ["کارت پرواز"]},
    "check in desk": {"en": ["check in desk","check-in counter"], "fr": ["comptoir denregistrement","comptoir d enregistrement"], "de": ["check in schalter","check-in schalter"], "fa": ["کانتر پذیرش","گیشه پذیرش"]},
    "gate": {"en": ["gate"], "fr": ["porte dembarquement","porte"], "de": ["gate"], "fa": ["گیت"]},
    "carry on bag": {"en": ["carry on bag","cabin bag"], "fr": ["bagage cabine"], "de": ["handgepäck"], "fa": ["ساک دستی","چمدان کابین"]},
    "suitcase": {"en": ["suitcase","luggage"], "fr": ["valise"], "de": ["koffer"], "fa": ["چمدان"]},
    "airport interior": {"en": ["airport"], "fr": ["aeroport","aéroport"], "de": ["flughafen"], "fa": ["فرودگاه"]},

    # ---- Banking / Finance ----
    "bank account": {"en": ["bank account"], "fr": ["compte bancaire"], "de": ["bankkonto"], "fa": ["حساب بانکی"]},
    "debit card": {"en": ["bank card","debit card"], "fr": ["carte bancaire"], "de": ["bankkarte"], "fa": ["کارت بانکی"]},
    "pin code": {"en": ["pin code"], "fr": ["code pin"], "de": ["pin"], "fa": ["رمز کارت","پین"]},
    "bank transfer": {"en": ["money transfer","bank transfer"], "fr": ["virement"], "de": ["überweisung","ueberweisung"], "fa": ["انتقال وجه"]},
    "online banking": {"en": ["online banking"], "fr": ["banque en ligne"], "de": ["online banking"], "fa": ["بانکداری آنلاین"]},
    "bank interior": {"en": ["bank"], "fr": ["banque"], "de": ["bank"], "fa": ["بانک"]},

    # ---- Clothing Store / Fashion ----
    "jacket": {"en": ["jacket"], "fr": ["veste"], "de": ["jacke"], "fa": ["کت","کاپشن"]},
    "shirt": {"en": ["shirt"], "fr": ["chemise"], "de": ["hemd"], "fa": ["پیراهن"]},
    "trousers": {"en": ["trousers","pants"], "fr": ["pantalon"], "de": ["hose"], "fa": ["شلوار"]},
    "dress": {"en": ["dress"], "fr": ["robe"], "de": ["kleid"], "fa": ["پیراهن زنانه","لباس"]},
    "shoes": {"en": ["shoes"], "fr": ["chaussures"], "de": ["schuhe"], "fa": ["کفش"]},
    "belt": {"en": ["belt"], "fr": ["ceinture"], "de": ["gürtel","guertel"], "fa": ["کمربند"]},
    "fitting room": {"en": ["fitting room","changing room"], "fr": ["cabine dessayage","cabine d essayage"], "de": ["umkleidekabine"], "fa": ["اتاق پرو"]},
    "cashier": {"en": ["cashier","checkout"], "fr": ["caisse"], "de": ["kasse"], "fa": ["صندوق","صندوقدار"]},
    "clothing store interior": {"en": ["clothing store"], "fr": ["magasin de vetements","magasin de vêtements"], "de": ["kleidungsgeschaeft","kleidungsgeschäft"], "fa": ["فروشگاه لباس"]},

    # ---- Directions / City ----
    "bus stop": {"en": ["bus stop"], "fr": ["arret de bus","arrêt de bus"], "de": ["bushaltestelle"], "fa": ["ایستگاه اتوبوس"]},
    "park": {"en": ["park"], "fr": ["parc"], "de": ["park"], "fa": ["پارک"]},
    "map": {"en": ["map"], "fr": ["plan","carte"], "de": ["karte"], "fa": ["نقشه"]},
    "street": {"en": ["street"], "fr": ["rue"], "de": ["straße","strasse"], "fa": ["خیابان"]},
    "square": {"en": ["square","plaza"], "fr": ["place"], "de": ["platz"], "fa": ["میدان"]},
    "bridge": {"en": ["bridge"], "fr": ["pont"], "de": ["brücke","bruecke"], "fa": ["پل"]},
    "hotel exterior": {"en": ["hotel"], "fr": ["hotel","hôtel"], "de": ["hotel"], "fa": ["هتل"]},

    # ---- Doctor / Clinic ----
    "fever": {"en": ["fever"], "fr": ["fievre","fièvre"], "de": ["fieber"], "fa": ["تب"]},
    "cough": {"en": ["cough"], "fr": ["toux"], "de": ["husten"], "fa": ["سرفه"]},
    "sore throat": {"en": ["sore throat"], "fr": ["mal de gorge"], "de": ["halsschmerzen"], "fa": ["گلودرد"]},
    "stomach ache": {"en": ["stomach ache","stomachache"], "fr": ["mal de ventre"], "de": ["bauchschmerzen"], "fa": ["دل درد","درد معده"]},
    "prescription": {"en": ["prescription"], "fr": ["ordonnance"], "de": ["rezept"], "fa": ["نسخه پزشکی"]},
    "clinic interior": {"en": ["clinic","doctor office"], "fr": ["clinique","cabinet medical","cabinet médical"], "de": ["arztpraxis","klinik"], "fa": ["کلینیک","مطب"]},

    # ---- Hotel ----
    "reservation": {"en": ["reservation","booking"], "fr": ["reservation","réservation"], "de": ["reservierung"], "fa": ["رزرو"]},
    "check in": {"en": ["check in","check-in"], "fr": ["check in","check-in"], "de": ["einchecken"], "fa": ["پذیرش","چک این"]},
    "key card": {"en": ["key card"], "fr": ["carte de cle","carte de clé"], "de": ["schlüsselkarte"], "fa": ["کارت اتاق"]},
    "towel": {"en": ["towel","towels"], "fr": ["serviette","serviettes"], "de": ["handtuch","handtücher","handtuecher"], "fa": ["حوله"]},
    "extra pillow": {"en": ["extra pillow"], "fr": ["oreiller supplementaire","oreiller supplémentaire"], "de": ["extra kissen"], "fa": ["بالش اضافه"]},
    "invoice": {"en": ["invoice"], "fr": ["facture"], "de": ["rechnung"], "fa": ["فاکتور"]},
    "hotel reception": {"en": ["hotel reception","front desk"], "fr": ["reception hotel","réception hôtel"], "de": ["rezeption"], "fa": ["پذیرش هتل"]},

    # ---- Phone Shop / Electronics ----
    "smartphone": {"en": ["smartphone","mobile phone"], "fr": ["smartphone"], "de": ["smartphone","handy"], "fa": ["گوشی هوشمند","موبایل"]},
    "sim card": {"en": ["sim card","sim"], "fr": ["carte sim"], "de": ["sim karte","sim-karte"], "fa": ["سیم کارت"]},
    "charger": {"en": ["charger"], "fr": ["chargeur"], "de": ["ladegerät","ladegeraet"], "fa": ["شارژر"]},
    "cable": {"en": ["cable"], "fr": ["cable","câble"], "de": ["kabel"], "fa": ["کابل"]},
    "case": {"en": ["phone case","case"], "fr": ["coque"], "de": ["hülle","huelle"], "fa": ["قاب گوشی"]},
    "screen protector": {"en": ["screen protector"], "fr": ["protection ecran","protection d ecran","protection d’écran"], "de": ["schutzfolie"], "fa": ["محافظ صفحه","گلس"]},
    "electronics store": {"en": ["phone shop","electronics store"], "fr": ["magasin de telephones","magasin de téléphones"], "de": ["handy laden","handyladen"], "fa": ["فروشگاه موبایل"]},

    # ---- Post Office ----
    "parcel": {"en": ["parcel","package"], "fr": ["colis"], "de": ["paket"], "fa": ["بسته پستی"]},
    "letter": {"en": ["letter","mail"], "fr": ["lettre"], "de": ["brief"], "fa": ["نامه"]},
    "stamp": {"en": ["stamps","stamp"], "fr": ["timbres","timbre"], "de": ["briefmarken","briefmarke"], "fa": ["تمبر"]},
    "label": {"en": ["label"], "fr": ["etiquette","étiquette"], "de": ["etikett"], "fa": ["برچسب"]},
    "registered": {"en": ["registered mail"], "fr": ["recommande","recommandé"], "de": ["einschreiben"], "fa": ["سفارشی"]},
    "express delivery": {"en": ["express delivery"], "fr": ["livraison express"], "de": ["expresslieferung"], "fa": ["پست پیشتاز"]},
    "post office interior": {"en": ["post office"], "fr": ["poste"], "de": ["post"], "fa": ["اداره پست"]},
}

# Canonical → (query template, pixabay category)
QUERY_TEMPLATES: Dict[str, Tuple[str, Optional[str]]] = {
    # Cafe (food)
    "espresso": ("espresso coffee cup", "food"),
    "americano": ("americano coffee", "food"),
    "cappuccino": ("cappuccino with latte art", "food"),
    "latte": ("cafe latte cup", "food"),
    "flat white": ("flat white coffee", "food"),
    "black coffee": ("black coffee cup", "food"),
    "green tea": ("green tea cup", "food"),
    "herbal tea": ("herbal tea cup", "food"),
    "hot chocolate": ("hot chocolate mug", "food"),
    "croissant": ("croissant pastry on plate", "food"),
    "sandwich": ("sandwich on wooden board", "food"),
    "menu": ("restaurant menu on table", "food"),
    "bill": ("restaurant bill on table", "food"),
    "receipt": ("receipt on table", "food"),

    # Pharmacy / Health
    "cough syrup": ("cough syrup bottle pharmacy shelf", "health"),
    "pain tablets": ("pain relief tablets blister pack", "health"),
    "nasal spray": ("nasal spray bottle", "health"),
    "thermometer": ("digital thermometer", "health"),
    "vitamins": ("vitamin pills bottle", "health"),
    "pharmacy interior": ("pharmacy interior shelves", "health"),

    # Airport / Travel
    "passport": ("passport on airport counter", "travel"),
    "boarding pass": ("boarding pass at airport", "travel"),
    "check in desk": ("airport check-in counter", "travel"),
    "gate": ("airport gate sign", "travel"),
    "carry on bag": ("carry on bag at airport", "travel"),
    "suitcase": ("traveler with suitcase in airport", "travel"),
    "airport interior": ("modern airport interior", "travel"),

    # Banking / Finance
    "bank account": ("bank counter opening account", "business"),
    "debit card": ("credit debit card closeup", "business"),
    "pin code": ("entering pin at atm keypad", "business"),
    "bank transfer": ("online banking transfer screen", "business"),
    "online banking": ("online banking smartphone app", "business"),
    "bank interior": ("bank interior counter", "business"),

    # Clothing / Fashion
    "jacket": ("jacket on hanger clothing store", "fashion"),
    "shirt": ("men shirt on hanger", "fashion"),
    "trousers": ("trousers on rack", "fashion"),
    "dress": ("dress on mannequin", "fashion"),
    "shoes": ("shoes display in store", "fashion"),
    "belt": ("leather belt display", "fashion"),
    "fitting room": ("fitting room clothing store", "fashion"),
    "cashier": ("cashier counter store", "fashion"),
    "clothing store interior": ("clothing store interior", "fashion"),

    # Directions / City
    "bus stop": ("city bus stop", "transportation"),
    "park": ("city park path", "places"),
    "map": ("tourist reading city map", "people"),
    "street": ("city street view", "places"),
    "square": ("town square plaza", "places"),
    "bridge": ("city bridge over river", "places"),
    "hotel exterior": ("hotel exterior entrance", "travel"),

    # Doctor / Clinic
    "fever": ("fever thermometer patient", "health"),
    "cough": ("woman coughing medical", "health"),
    "sore throat": ("sore throat patient doctor", "health"),
    "stomach ache": ("stomach ache person", "health"),
    "prescription": ("doctor writing prescription", "health"),
    "clinic interior": ("clinic waiting room", "health"),

    # Hotel
    "reservation": ("hotel reservation at reception", "travel"),
    "check in": ("hotel check-in reception desk", "travel"),
    "key card": ("hotel key card at reception", "travel"),
    "towel": ("two towels on bed hotel", "travel"),
    "extra pillow": ("extra pillow on bed", "travel"),
    "invoice": ("invoice receipt hotel", "business"),
    "hotel reception": ("hotel reception front desk", "travel"),

    # Phone Shop / Electronics
    "smartphone": ("smartphone display in store", "computer"),
    "sim card": ("sim card on hand", "computer"),
    "charger": ("phone charger on table", "computer"),
    "cable": ("usb cable closeup", "computer"),
    "case": ("phone case wall display", "computer"),
    "screen protector": ("installing screen protector", "computer"),
    "electronics store": ("electronics store interior", "computer"),

    # Post Office
    "parcel": ("sending parcel at post office", "business"),
    "letter": ("writing letter envelope", "business"),
    "stamp": ("postage stamps", "business"),
    "label": ("shipping label closeup", "business"),
    "registered": ("registered mail counter", "business"),
    "express delivery": ("express delivery package", "transportation"),
    "post office interior": ("post office counter interior", "business"),
}

# Phrases to ignore (do not drive the image)
MODIFIER_PHRASES = {
    "with milk","without milk","no milk","milk",
    "with sugar","without sugar","no sugar","sugar",
    "with ice","without ice","no ice","ice",
    "soy milk","oat milk","almond milk","lactose free","gluten free",
    "small size","medium size","large size","extra hot","less hot",
    "with warranty","without contract","more storage","online banking","no monthly fee",
    "with priority","with a tag","without liquids",
    "for children","for adults","sans sucre","ohne zucker","بدون شکر",
    "blue","black","white","large","medium","small"
}

# ------------------------------- #
#      Dynamic Scenario Mining    #
# ------------------------------- #
_SCENARIO_TERMS: Dict[str, Set[str]] = {}
_SCENARIO_LOADED = False

def _scenario_paths() -> List[Path]:
    here = Path(__file__).resolve().parent
    candidates = list(here.glob("A*_*.txt")) + list(here.parent.glob("A*_*.txt"))
    return candidates

def _infer_domain_from_filename(fn: str) -> str:
    f = fn.lower()
    if "pharmacy" in f: return "pharmacy"
    if "airport" in f: return "airport"
    if "bank" in f: return "bank"
    if "clothing" in f or "clothes" in f: return "clothing"
    if "directions" in f: return "directions"
    if "doctor" in f: return "doctor"
    if "hotel" in f: return "hotel"
    if "phone" in f: return "phone"
    if "post" in f: return "post"
    if "cafe" in f: return "food"
    return "generic"

def _load_scenario_terms_once():
    global _SCENARIO_LOADED, _SCENARIO_TERMS
    if _SCENARIO_LOADED:
        return
    _SCENARIO_LOADED = True
    term_freq: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)

    for p in _scenario_paths():
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        lang_guess = guess_lang(text)
        tokens = _tokenize(text, lang_guess)
        domain = _infer_domain_from_filename(p.name)
        for t in tokens:
            if t.isdigit():
                continue
            term_freq[domain][t] += 1

    for domain, counter in term_freq.items():
        keep = set([t for t, c in counter.most_common(200) if c >= NLPConfig.FREQ_MIN_SCENARIO and len(t) > 2])
        _SCENARIO_TERMS[domain] = keep

def _scenario_hints_for(domain: str) -> List[str]:
    _load_scenario_terms_once()
    return sorted(list(_SCENARIO_TERMS.get(domain, set())))[:20]

# ------------------------------- #
#       Core matching logic       #
# ------------------------------- #
def _clean_modifiers(text: str) -> str:
    t = _normalize_text(text)
    for m in MODIFIER_PHRASES:
        t = t.replace(_normalize_text(m), " ")
    for n in NEGATORS:
        t = t.replace(f" {n} ", " ")
    t = re.sub(r"\s+", " ", t).strip()
    return t

def _detect_candidates(text: str, lang: str) -> List[str]:
    tokens = _tokenize(text, lang)
    joined = " ".join(tokens)
    hits: List[str] = []

    for canonical, variants in LEXICON.items():
        vlist = []
        code = lang.split("-")[0][:2]
        vlist.extend(variants.get(code, []))
        if code != "en":
            vlist.extend(variants.get("en", []))
        found = False
        for v in sorted(set(vlist), key=len, reverse=True):
            v_norm = _normalize_text(v)
            if v_norm and re.search(rf"\b{re.escape(v_norm)}\b", joined):
                hits.append(canonical)
                found = True
                break
        if found:
            continue

    if not hits:
        for canonical, variants in LEXICON.items():
            vlist = []
            code = lang.split("-")[0][:2]
            vlist.extend(variants.get(code, []))
            if code != "en":
                vlist.extend(variants.get("en", []))
            best = 0.0
            for v in vlist:
                v_norm = _normalize_text(v)
                for tok in tokens:
                    s = _tri_sim(tok, v_norm)
                    if s > best:
                        best = s
            if best >= NLPConfig.TRIGRAM_MATCH_THRESHOLD:
                hits.append(canonical)

    def first_idx(key):
        phrases = []
        code = lang.split("-")[0][:2]
        phrases.extend(LEXICON[key].get(code, []))
        phrases.extend(LEXICON[key].get("en", []))
        for p in phrases:
            m = re.search(rf"\b{re.escape(_normalize_text(p))}\b", joined)
            if m:
                return m.start()
        return 10**9
    return sorted(set(hits), key=first_idx)

def _classify(canonical: str) -> str:
    if canonical in {"espresso","americano","cappuccino","latte","flat white","black coffee","green tea","herbal tea","hot chocolate","croissant","sandwich"}:
        return "food"
    if canonical in {"passport","boarding pass","check in desk","gate","carry on bag","suitcase","airport interior"}:
        return "airport"
    if canonical in {"cough syrup","pain tablets","nasal spray","thermometer","vitamins","clinic interior","pharmacy interior"}:
        return "pharmacy"
    if canonical in {"bank account","debit card","pin code","bank transfer","online banking","bank interior"}:
        return "bank"
    if canonical in {"jacket","shirt","trousers","dress","shoes","belt","fitting room","cashier","clothing store interior"}:
        return "clothing"
    if canonical in {"bus stop","park","map","street","square","bridge","hotel exterior"}:
        return "directions"
    if canonical in {"fever","cough","sore throat","stomach ache","prescription","clinic interior"}:
        return "doctor"
    if canonical in {"reservation","check in","key card","towel","extra pillow","invoice","hotel reception"}:
        return "hotel"
    if canonical in {"smartphone","sim card","charger","cable","case","screen protector","electronics store"}:
        return "phone"
    if canonical in {"parcel","letter","stamp","label","registered","express delivery","post office interior"}:
        return "post"
    return "generic"

def _domain_anchor(domain: str) -> List[str]:
    mapping = {
        "food": ["cafe interior", "coffee shop interior", "barista making coffee"],
        "pharmacy": ["pharmacy interior shelves", "medicine counter pharmacy"],
        "airport": ["modern airport interior", "airport departure hall"],
        "bank": ["bank counter interior", "atm machine"],
        "clothing": ["clothing store interior", "fashion display"],
        "directions": ["city street view", "town square plaza"],
        "doctor": ["clinic waiting room", "doctor with patient"],
        "hotel": ["hotel reception front desk", "hotel lobby"],
        "phone": ["electronics store interior", "phone shop display"],
        "post": ["post office counter", "mail shipping counter"],
        "generic": ["people indoors", "object closeup"]
    }
    return mapping.get(domain, mapping["generic"])

def sentence_to_query(text: str, lang: str = AUTO_IMAGE_LANG, max_words: int = NLPConfig.MAX_QUERY_TOKENS) -> str:
    auto = lang if lang and lang != "auto" else guess_lang(text)
    text_norm = _clean_modifiers(text)
    tokens = _tokenize(text_norm, auto)

    hits = _detect_candidates(text_norm, auto)
    if hits:
        first = hits[0]
        tmpl, _cat = QUERY_TEMPLATES.get(first, (first, None))
        return tmpl

    informative = tokens[:max_words] if tokens else []
    domain_guess = "generic"
    heur_text = " ".join(tokens)
    for kw, d in [
        ("airport", "airport"), ("gate", "airport"), ("boarding", "airport"), ("passport", "airport"),
        ("bank", "bank"), ("transfer", "bank"), ("pin", "bank"),
        ("parcel", "post"), ("post", "post"), ("stamp", "post"),
        ("pharmacy", "pharmacy"), ("thermometer", "pharmacy"), ("vitamin", "pharmacy"),
        ("hotel", "hotel"), ("towel", "hotel"), ("pillow", "hotel"),
        ("sim", "phone"), ("smartphone", "phone"), ("charger", "phone"),
        ("jacket", "clothing"), ("dress", "clothing"), ("belt", "clothing"), ("shoes", "clothing"),
        ("bridge", "directions"), ("map", "directions"), ("street", "directions"),
        ("fever", "doctor"), ("cough", "doctor"), ("throat", "doctor")
    ]:
        if kw in heur_text:
            domain_guess = d
            break

    anchors = _domain_anchor(domain_guess)
    hints = _scenario_hints_for(domain_guess)
    phrase = " ".join(informative + anchors[:1]).strip()
    if hints:
        phrase = f"{phrase} {hints[0]}" if phrase else hints[0]
    return phrase or anchors[0]

def sentence_to_query_extras(text: str, lang: str) -> Tuple[List[Tuple[str, Optional[str]]], Optional[str]]:
    auto = lang if lang and lang != "auto" else guess_lang(text)
    primary = sentence_to_query(text, auto)

    tokens = _tokenize(_clean_modifiers(text), auto)
    hits = _detect_candidates(" ".join(tokens), auto)
    primary_cat = None
    domain = "generic"
    if hits:
        first = hits[0]
        tmpl, cat = QUERY_TEMPLATES.get(first, (primary, None))
        primary_cat = cat
        domain = _classify(first)
    else:
        if any(w in tokens for w in ["passport","gate","airport","boarding"]): domain = "airport"
        elif any(w in tokens for w in ["pharmacy","thermometer","vitamin"]): domain = "pharmacy"
        elif any(w in tokens for w in ["bank","transfer","pin"]): domain = "bank"
        elif any(w in tokens for w in ["jacket","dress","belt","shoes","fitting"]): domain = "clothing"
        elif any(w in tokens for w in ["bus","bridge","street","map","square"]): domain = "directions"
        elif any(w in tokens for w in ["fever","cough","throat","stomach"]): domain = "doctor"
        elif any(w in tokens for w in ["hotel","towel","pillow","invoice"]): domain = "hotel"
        elif any(w in tokens for w in ["sim","smartphone","charger","cable","case"]): domain = "phone"
        elif any(w in tokens for w in ["parcel","stamp","post"]): domain = "post"

    pairs: List[Tuple[str, Optional[str]]] = []
    pairs.append((primary, primary_cat))

    for c in hits[:3]:
        tmpl, cat = QUERY_TEMPLATES.get(c, (c, primary_cat))
        if (tmpl, cat) not in pairs:
            pairs.append((tmpl, cat))

    eng_like = " ".join([w for w in tokens if re.match(r"[a-z0-9]+$", w)])
    if eng_like and (eng_like, primary_cat) not in pairs:
        pairs.append((eng_like, primary_cat))

    anchors = _domain_anchor(domain)
    hints = _scenario_hints_for(domain)
    for a in anchors:
        q = f"{eng_like} {a}".strip() if eng_like else a
        if (q, primary_cat) not in pairs:
            pairs.append((q, primary_cat))
    for h in hints[:3]:
        q = f"{eng_like} {h}".strip() if eng_like else h
        if (q, primary_cat) not in pairs:
            pairs.append((q, primary_cat))

    generic = _domain_anchor(domain) + _scenario_hints_for(domain)
    for q in generic:
        if (q, primary_cat) not in pairs:
            pairs.append((q, primary_cat))

    seen = set()
    uniq: List[Tuple[str, Optional[str]]] = []
    for q, cat in pairs:
        key = (q.strip().lower(), cat)
        if key not in seen and q.strip():
            uniq.append((q, cat))
            seen.add(key)
        if len(uniq) >= (1 + NLPConfig.MAX_FALLBACKS):
            break
    return uniq, primary_cat

# ------------------------------- #
#        HTTP & Keys              #
# ------------------------------- #
def _requests_session_with_retries() -> requests.Session:
    s = requests.Session()
    retries = Retry(total=IMAGE_RETRIES, backoff_factor=0.6, status_forcelist=[429,500,502,503,504])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.mount("http://", HTTPAdapter(max_retries=retries))
    return s

def read_pixabay_key_from_file() -> Optional[str]:
    for d in (Path.cwd(), Path(__file__).resolve().parent):
        f = d / "pixabay.key"
        if f.exists():
            try:
                key = f.read_text(encoding="utf-8").strip()
                if key:
                    return key
            except Exception:
                pass
    return None

def read_unsplash_key_from_file() -> Optional[str]:
    for d in (Path.cwd(), Path(__file__).resolve().parent):
        f = d / "unsplash.key"
        if f.exists():
            try:
                key = f.read_text(encoding="utf-8").strip()
                if key:
                    return key
            except Exception:
                pass
    return None

# ------------------------------- #
#        Ranking / Scoring        #
# ------------------------------- #
def _score_hit(tags: str, raw_query_tokens: List[str]) -> float:
    ts = _normalize_text(tags).split(",")
    tag_tokens = set()
    for t in ts:
        tag_tokens.update(_normalize_text(t).split())
    rq = set([w for w in raw_query_tokens if len(w) > 2])
    if not tag_tokens:
        return 0.0
    overlap = len(tag_tokens & rq) / max(1, len(rq))
    tri = 0.0
    if rq:
        tri = max(_tri_sim(" ".join(tag_tokens), " ".join(rq)), 0.0)
    boost = 1.0 + (tri if tri >= NLPConfig.TRIGRAM_TAG_THRESHOLD else 0.0)
    return overlap * boost

def _domain_keyword_boost(text: str) -> float:
    text = text.lower()
    if any(k in text for k in ["airport","gate","boarding","passport"]): return 1.10
    if any(k in text for k in ["pharmacy","medicine","clinic"]): return 1.10
    if any(k in text for k in ["bank","atm","finance"]): return 1.08
    if any(k in text for k in ["hotel","reception","lobby","towel"]): return 1.08
    if any(k in text for k in ["clothing","fashion","fitting room","dress","jacket"]): return 1.08
    if any(k in text for k in ["post","mail","shipping","stamp"]): return 1.08
    if any(k in text for k in ["coffee","cafe","tea"]): return 1.05
    if any(k in text for k in ["street","bridge","map","square"]): return 1.05
    return 1.0

# ------------------------------- #
#     Pixabay: rank & download    #
# ------------------------------- #
def _pixabay_ranked(query: str, key: str, category: Optional[str]) -> List[Dict]:
    url = "https://pixabay.com/api/"
    params = {
        "key": key,
        "q": query,
        "image_type": "photo",
        "safesearch": PIXABAY_SAFESEARCH,
        "per_page": ProviderConfig.PIXABAY_PER_PAGE,
        "orientation": "horizontal",
        "lang": "en"
    }
    if category:
        params["category"] = category
    sess = _requests_session_with_retries()
    try:
        r = sess.get(url, params=params, timeout=IMAGE_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        hits = data.get("hits", [])
    except Exception as e:
        print(f"⚠️ Pixabay search failed for '{query}': {e}")
        return []

    raw_tokens = _tokenize(_normalize_text(query), "en")
    ranked = []
    for h in hits:
        src = h.get("largeImageURL") or h.get("webformatURL")
        if not src:
            continue
        tags = h.get("tags","")
        score = _score_hit(tags, raw_tokens)
        score *= _domain_keyword_boost(tags)
        if h.get("type") == "photo":
            score *= 1.05
        ranked.append({
            "provider": "pixabay",
            "score": score,
            "src": src,
            "tags": tags,
            "meta": {"pageURL": h.get("pageURL"), "user": h.get("user"), "user_id": h.get("user_id")}
        })
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked

def _download_image(sess: requests.Session, src: str, query: str, dest_dir: Path, provider: str) -> Optional[Path]:
    try:
        fn = hashlib.md5((provider + "|" + query + "|" + src).encode("utf-8")).hexdigest() + ".jpg"
        dst = dest_dir / fn
        if not dst.exists():
            img = sess.get(src, timeout=IMAGE_TIMEOUT)
            img.raise_for_status()
            dst.write_bytes(img.content)
        return dst
    except Exception:
        return None

def pixabay_search_and_download(query: str, dest_dir: Path, n: int = 1, key_once: Optional[str] = None, category: Optional[str] = None) -> List[Path]:
    """Kept for backward compatibility; prefers internal re-ranking."""
    key = key_once or read_pixabay_key_from_file()
    if not key:
        print("⚠️ No Pixabay API key found (pixabay.key).")
        return []
    dest_dir.mkdir(parents=True, exist_ok=True)
    ranked = _pixabay_ranked(query, key, category)
    out: List[Path] = []
    sess = _requests_session_with_retries()
    seen = set()
    for item in ranked:
        if item["src"] in seen: continue
        seen.add(item["src"])
        p = _download_image(sess, item["src"], query, dest_dir, "pixabay")
        if p:
            out.append(p)
        if len(out) >= n: break
    return out

# ------------------------------- #
#     Unsplash: rank & download   #
# ------------------------------- #
def _unsplash_ranked(query: str, key: str) -> List[Dict]:
    url = "https://api.unsplash.com/search/photos"
    headers = {"Authorization": f"Client-ID {key}"}
    params = {
        "query": query,
        "per_page": ProviderConfig.UNSPLASH_PER_PAGE,
        "orientation": ProviderConfig.ORIENTATION,
        "content_filter": ProviderConfig.CONTENT_FILTER
    }
    sess = _requests_session_with_retries()
    try:
        r = sess.get(url, headers=headers, params=params, timeout=IMAGE_TIMEOUT)
        r.raise_for_status()
        data = r.json()
        results = data.get("results", [])
    except Exception as e:
        print(f"⚠️ Unsplash search failed for '{query}': {e}")
        return []

    raw_tokens = _tokenize(_normalize_text(query), "en")
    ranked = []
    for ph in results:
        urls = ph.get("urls", {}) or {}
        # prefer 'regular' to avoid huge downloads; fallback to full
        src = urls.get("regular") or urls.get("full") or urls.get("raw")
        if not src:
            continue
        tags_list = ph.get("tags", []) or []
        tag_text = ",".join([t.get("title","") for t in tags_list if isinstance(t, dict)])

        alt_desc = ph.get("alt_description") or ""
        desc = ph.get("description") or ""
        tags = ",".join([s for s in [tag_text, alt_desc, desc] if s])

        score = _score_hit(tags, raw_query_tokens=raw_tokens)
        score *= _domain_keyword_boost(tags)
        likes = ph.get("likes", 0)
        score *= 1.0 + min(likes, 200) / 500.0  # gentle popularity boost up to +0.4

        ranked.append({
            "provider": "unsplash",
            "score": score,
            "src": src,
            "tags": tags,
            "meta": {
                "id": ph.get("id"),
                "user": (ph.get("user", {}) or {}).get("name"),
                "user_username": (ph.get("user", {}) or {}).get("username"),
                "user_link": ((ph.get("user", {}) or {}).get("links", {}) or {}).get("html"),
                "photo_link": (ph.get("links", {}) or {}).get("html"),
                "likes": likes
            }
        })
    ranked.sort(key=lambda x: x["score"], reverse=True)
    return ranked

def _write_credit_sidecar(image_path: Path, item: Dict):
    try:
        meta = item.get("meta") or {}
        provider = item.get("provider","")
        credit = None
        if provider == "unsplash" and meta.get("user"):
            user = meta.get("user")
            username = meta.get("user_username") or ""
            credit = f"Photo by {user} (@{username}) on Unsplash"
        elif provider == "pixabay" and meta.get("user"):
            credit = f"Photo by {meta.get('user')} on Pixabay"
        if credit:
            (image_path.with_suffix(".txt")).write_text(credit, encoding="utf-8")
    except Exception:
        pass

def _search_both_ranked(query: str, category: Optional[str]) -> List[Dict]:
    ranked_all: List[Dict] = []
    pix_key = read_pixabay_key_from_file()
    uns_key = read_unsplash_key_from_file()

    if pix_key:
        ranked_all.extend(_pixabay_ranked(query, pix_key, category))
    else:
        print("ℹ️ Pixabay key not found, skipping Pixabay for this query.")
    if uns_key:
        ranked_all.extend(_unsplash_ranked(query, uns_key))
    else:
        print("ℹ️ Unsplash key not found, skipping Unsplash for this query.")

    ranked_all.sort(key=lambda x: x["score"], reverse=True)
    return ranked_all

def search_and_download_best(query: str, dest_dir: Path, n: int = 1, category: Optional[str] = None) -> List[Path]:
    """
    Search both Unsplash & Pixabay, combine & re-rank, download top-n unique images.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    ranked_all = _search_both_ranked(query, category)
    if not ranked_all:
        return []

    out: List[Path] = []
    sess = _requests_session_with_retries()
    seen_src = set()
    for item in ranked_all:
        src = item["src"]
        if src in seen_src:
            continue
        seen_src.add(src)
        p = _download_image(sess, src, query, dest_dir, item["provider"])
        if p:
            _write_credit_sidecar(p, item)
            out.append(p)
        if len(out) >= n:
            break
    return out

# ------------------------------- #
#   High-level: sentence → image  #
# ------------------------------- #
def get_images_for_cues(cues: List[dict]) -> List[Optional[Path]]:
    """
    Return one image per PRIMARY cue (SECONDARY cues reuse last image).
    Now tries:
      1) if cue contains explicit tags -> try searching tags (joined and individual)
      2) fallback to existing sentence_to_query_extras pipeline
    """
    result: List[Optional[Path]] = []
    last_img: Optional[Path] = None

    for c in cues:
        if c.get("is_primary", True):
            lang = c.get("lang", AUTO_IMAGE_LANG)
            # 1) Try explicit tags first
            tags = c.get("tags") or []
            img_path: Optional[Path] = None
            if tags:
                # --- normalize tags (lower, keep [a-z0-9-], drop empties)
                def _norm_tag(t: str) -> str:
                    t = str(t or "").strip().lower()
                    t = "".join(ch for ch in t if (ch.isalnum() or ch == "-"))
                    return t if 2 <= len(t) <= 24 else ""
                tags_norm = [x for x in (_norm_tag(t) for t in tags) if x]

                # derive category from the sentence (helps Pixabay)
                primary_cat = None
                try:
                    q_pairs_for_cat, cat = sentence_to_query_extras(c["text"], lang=lang or "auto")
                    primary_cat = cat
                except Exception:
                    primary_cat = None

                # try joined tags phrase first
                if tags_norm:
                    joined = " ".join(tags_norm)
                    print(f"🔎 Trying explicit tags for image (with category={primary_cat}): {joined}")
                    imgs = search_and_download_best(joined, CACHE_IMG_DIR, n=max(1, IMAGES_PER_SENTENCE), category=primary_cat)
                    if imgs:
                        img_path = imgs[0]
                        print(f"🖼️ Image matched via tags (joined): '{joined}' -> {img_path.name}")
                    else:
                        # try each tag alone as a fallback (with category if any)
                        for t in tags_norm:
                            imgs = search_and_download_best(t, CACHE_IMG_DIR, n=max(1, IMAGES_PER_SENTENCE), category=primary_cat)
                            if imgs:
                                img_path = imgs[0]
                                print(f"🖼️ Image matched via tag: '{t}' -> {img_path.name}")
                                break


            # 2) If no explicit-tag hit, fallback to the previous NLP pipeline
            if not img_path:
                q_pairs, primary_cat = sentence_to_query_extras(c["text"], lang=lang or "auto")
                for q, cat in q_pairs:
                    imgs = search_and_download_best(q, CACHE_IMG_DIR, n=max(1, IMAGES_PER_SENTENCE), category=cat)
                    if imgs:
                        img_path = imgs[0]
                        print(f"🖼️ Image matched: '{q}' (via best-of providers) -> {img_path.name}")
                        break
                    else:
                        print(f"… no hit for '{q}', trying fallback.")

            last_img = img_path
            result.append(img_path)
        else:
            result.append(last_img)
    return result


# ------------------------------- #
#          Video Helpers          #
# ------------------------------- #
def compute_visual_spans(cues: List[dict], total_audio_ms: int) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    n = len(cues)
    for i, c in enumerate(cues):
        start = c["start"]
        if i < n - 1:
            end = max(start, cues[i+1]["start"])
        else:
            end = total_audio_ms
        spans.append((start, end))
    return spans

def build_slideshow_video_cfr(cues, per_sentence_images, total_audio_ms, size=VIDEO_SIZE, fps=VIDEO_FPS) -> Path:
    w, h = map(int, size.split("x"))
    tmp_dir = CACHE_VIDEO_DIR
    tmp_dir.mkdir(exist_ok=True)

    spans = compute_visual_spans(cues, total_audio_ms)
    seg_paths = []
    r_fps = float(fps)

    for i, ((start_ms, end_ms), img) in enumerate(zip(spans, per_sentence_images), start=1):
        dur_ms = max(0, end_ms - start_ms)
        frames = max(1, round(dur_ms * r_fps / 1000.0))
        seg = tmp_dir / f"seg_{i:04}.mp4"

        if img and Path(img).exists():
            vf = (
                "scale="
                f"w='if(gte(a,{w}/{h}),-1,{w})':"
                f"h='if(gte(a,{w}/{h}),{h},-1)',"
                f"crop={w}:{h},fps={fps},format=yuv420p"
            )
            cmd = [
                "ffmpeg","-hide_banner","-y",
                "-loop","1","-i", str(img),
                "-vf", vf,
                "-r", str(fps),
                "-frames:v", str(frames),
                "-c:v","libx264","-pix_fmt","yuv420p",
                str(seg)
            ]
        else:
            cmd = [
                "ffmpeg","-hide_banner","-y",
                "-f","lavfi","-i", f"color=c=black:s={w}x{h}:r={fps}",
                "-r", str(fps),
                "-frames:v", str(frames),
                "-c:v","libx264","-pix_fmt","yuv420p",
                str(seg)
            ]
        subprocess.run(cmd, check=True)
        seg_paths.append(seg)

    list_file = tmp_dir / "list.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in seg_paths:
            f.write(f"file '{p.resolve()}'\n")

    slideshow = tmp_dir / "slideshow.mp4"
    cmd_concat = [
        "ffmpeg","-hide_banner","-y",
        "-f","concat","-safe","0",
        "-i", str(list_file),
        "-c:v","libx264","-pix_fmt","yuv420p",
        "-r", str(fps),
        "-an",
        str(slideshow)
    ]
    subprocess.run(cmd_concat, check=True)
    return slideshow.resolve()

def mux_subs_and_audio_on_video(base_video_path: Path, ass_path: Path, audio_path: Path, out_mp4: str):
    subs = f"subtitles=filename={Path(ass_path).name}:charenc=UTF-8:force_style='Alignment=5,BorderStyle=1,Outline=3,Shadow=2'"
    cmd = [
        "ffmpeg","-hide_banner","-y",
        "-i", str(base_video_path),
        "-i", str(audio_path),
        "-vf", subs,
        "-c:v","libx264","-pix_fmt","yuv420p",
        "-c:a","aac","-b:a","192k","-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
        "-shortest", out_mp4
    ]
    subprocess.run(cmd, check=True, cwd=str(Path(audio_path).parent))

def render_video_single_or_none(audio_path, ass_path, out_mp4, size=VIDEO_SIZE, fps=VIDEO_FPS, bg_image=None):
    audio_p = Path(audio_path).resolve()
    ass_p   = Path(ass_path).resolve()
    out_p   = Path(out_mp4).resolve()
    workdir = audio_p.parent
    w, h = map(int, size.split("x"))

    img_abs = Path(bg_image).resolve() if bg_image else None

    if img_abs and img_abs.exists():
        vf = (
            "scale="
            f"w='if(gte(a,{w}/{h}),-1,{w})':"
            f"h='if(gte(a,{w}/{h}),{h},-1)',"
            f"crop={w}:{h},fps={fps},format=yuv420p"
        )
        cmd = [
            "ffmpeg","-hide_banner","-y",
            "-loop","1","-i", str(img_abs),
            "-i", str(audio_p.name),
            "-vf", vf,
            "-c:v","libx264","-pix_fmt","yuv420p",
            "-c:a","aac","-b:a","192k","-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "-shortest", str(out_p.name)
        ]
    else:
        subs = f"subtitles=filename={ass_p.name}:charenc=UTF-8:force_style='Alignment=5,BorderStyle=1,Outline=3,Shadow=2'"
        cmd = [
            "ffmpeg","-hide_banner","-y",
            "-f","lavfi","-i", f"color=c=black:s={size}:r={fps}",
            "-i", str(audio_p.name),
            "-vf", subs,
            "-c:v","libx264","-pix_fmt","yuv420p",
            "-c:a","aac","-b:a","192k","-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS),
            "-shortest", str(out_p.name)
        ]

    subprocess.run(cmd, check=True, cwd=str(workdir))
    print(f"🎥 Video written: {out_p}")
