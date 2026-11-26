"""Anthem provider-directory search and profiles."""

from __future__ import annotations

from .core import *
from .core import SCRIPT_DIR

# ---------------------------------------------------------------------------
# /anthem -- Anthem/Elevance Provider Directory FHIR search
# ---------------------------------------------------------------------------
#
# The public Provider Directory base below is the shared Elevance endpoint
# documented for CMS-mandated provider-directory access.  A registered app can
# set ANTHEM_FHIR_BASE_URL (or ANTHEM_FHIR_TOKEN / ANTHEM_FHIR_TOKEN_URL) to
# use a brand-specific registered endpoint instead.  The public shared endpoint
# has been returning HTTP 500 for resource searches, so the command keeps a
# CMS NPPES fallback: it can still identify doctors, specialties, and practice
# addresses, but it cannot claim Anthem network participation.

ANTHEM_FHIR_PUBLIC_BASE = os.environ.get(
    "ANTHEM_FHIR_BASE_URL",
    "https://totalview.healthos.elevancehealth.com/resources/unregistered/"
    "api/v1/fhir/cms_mandate/mcd",
).rstrip("/")

ANTHEM_FHIR_BRAND = os.environ.get("ANTHEM_FHIR_BRAND", "AnthemBlueCross")

ANTHEM_FHIR_REGISTERED_BASE = os.environ.get(
    "ANTHEM_FHIR_REGISTERED_BASE_URL",
    "https://totalview.healthos.elevancehealth.com/resources/registered/"
    f"{ANTHEM_FHIR_BRAND}/api/v1/fhir",
).rstrip("/")

ANTHEM_FHIR_TOKEN_URL = os.environ.get("ANTHEM_FHIR_TOKEN_URL", "").strip()

ANTHEM_FHIR_CLIENT_ID = os.environ.get("ANTHEM_FHIR_CLIENT_ID", "").strip()

ANTHEM_FHIR_CLIENT_SECRET = os.environ.get(
    "ANTHEM_FHIR_CLIENT_SECRET", ""
).strip()

ANTHEM_NPPES_URL = "https://npiregistry.cms.hhs.gov/api/"

ANTHEM_MODEL = os.environ.get("OLLAMA_ANTHEM_MODEL", "ornith-1.5:35b")

ANTHEM_MAX_PROVIDERS = 12

ANTHEM_MAX_FACILITY_PROVIDERS = 50

ANTHEM_CONTEXT_CHARS = 20000

# NPPES taxonomy search matches the official taxonomy description, not the
# everyday specialty word a patient uses. Keep a small bridge for the common
# requests so "cardiologist" does not come back empty just because the
# registry calls the specialty "Cardiovascular Disease".
_NPPES_TAXONOMY_ALIASES = {
    "cardiology": "Cardiovascular Disease",
    "cardiologist": "Cardiovascular Disease",
    "heart": "Cardiovascular Disease",
    "pulmonology": "Pulmonary Disease",
    "pulmonologist": "Pulmonary Disease",
    "lung": "Pulmonary Disease",
    "head and neck": "Otolaryngology",
    "head and neck surgery": "Otolaryngology",
    "head neck surgery": "Otolaryngology",
    "neck surgery": "Otolaryngology",
    "ent": "Otolaryngology",
    "ear nose and throat": "Otolaryngology",
    "orthopedics": "Orthopaedic Surgery",
    "orthopedic": "Orthopaedic Surgery",
    "dermatology": "Dermatology",
    "neurology": "Neurology",
    "oncologist": "Medical Oncology",
    "oncology": "Medical Oncology",
    "pediatrician": "Pediatrics",
    "pediatrics": "Pediatrics",
    "urologist": "Urology",
    "urology": "Urology",
    "surgeon": "Surgery",
    "surgery": "Surgery",
}

# NUCC provider-taxonomy codes used by Plan-Net ProviderRole.specialty.
# Trying a code search first is more reliable than sending a free-text word to
# a FHIR token search parameter.
_ANTHEM_FHIR_SPECIALTY_CODES = {
    "cardiology": "207RC0000X",
    "cardiologist": "207RC0000X",
    "heart": "207RC0000X",
    "dermatology": "207N00000X",
    "neurology": "2084N0400X",
    "medical oncology": "207QX0050X",
    "oncologist": "207QX0050X",
    "oncology": "207QX0050X",
    "otolaryngology": "207Y00000X",
    "head and neck": "207Y00000X",
    "orthopaedic surgery": "207X00000X",
    "orthopedics": "207X00000X",
    "pediatrics": "208000000X",
    "pediatrician": "208000000X",
    "pulmonary disease": "207RP1001X",
    "pulmonology": "207RP1001X",
    "lung": "207RP1001X",
    "surgery": "208600000X",
    "surgeon": "208600000X",
    "urology": "208800000X",
    "urologist": "208800000X",
}

# /united_healthcare -- UnitedHealthcare public Plan-Net FHIR directory.
UHC_FHIR_BASE = os.environ.get(
    "UHC_FHIR_BASE", "https://flex.optum.com/fhirpublic/R4"
).rstrip("/")

UHC_MODEL = os.environ.get("OLLAMA_UHC_MODEL", ANTHEM_MODEL)

_UHC_SPECIALTY_CODES = {
    "cardiology": "207RC0000X",
    "cardiologist": "207RC0000X",
    "cardiovascular disease": "207RC0000X",
    "heart": "207RC0000X",
    "dermatology": "207N00000X",
    "dermatologist": "207N00000X",
    "neurology": "2084N0400X",
    "neurologist": "2084N0400X",
    "medical oncology": "207QX0050X",
    "oncologist": "207QX0050X",
    "oncology": "207QX0050X",
    "surgical oncology": "2086X0206X",
    "otolaryngology": "207Y00000X",
    "otorhinolaryngology": "207Y00000X",
    "ent": "207Y00000X",
    "head and neck": "207Y00000X",
    "head and neck surgery": "207Y00000X",
    "orthopaedic surgery": "207X00000X",
    "orthopedic surgery": "207X00000X",
    "orthopedics": "207X00000X",
    "pediatrics": "208000000X",
    "pediatrician": "208000000X",
    "pulmonary disease": "207RP1001X",
    "pulmonology": "207RP1001X",
    "pulmonologist": "207RP1001X",
    "lung": "207RP1001X",
    "surgery": "208600000X",
    "surgeon": "208600000X",
    "thoracic surgery": "208G00000X",
    "cardiothoracic surgery": "208G00000X",
    "vascular surgery": "2086S0129X",
    "urology": "208800000X",
    "urologist": "208800000X",
    "gastroenterology": "207RG0100X",
    "gastroenterologist": "207RG0100X",
    "endocrinology": "207RE0101X",
    "endocrinologist": "207RE0101X",
    "psychiatry": "2084P0800X",
    "psychiatrist": "2084P0800X",
    "ophthalmology": "207W00000X",
    "ophthalmologist": "207W00000X",
    "obstetrics": "207V00000X",
    "gynecology": "207V00000X",
    "obstetrics and gynecology": "207V00000X",
    "anesthesiology": "207L00000X",
    "anesthesiologist": "207L00000X",
    "radiology": "2085R0202X",
    "radiologist": "2085R0202X",
    "pathology": "207ZP0102X",
    "pathologist": "207ZP0102X",
    "family medicine": "207Q00000X",
    "internal medicine": "207R00000X",
    "infectious disease": "207RI0200X",
    "immunology": "207KI0005X",
    "rheumatology": "207RR0500X",
    "nephrology": "207RN0300X",
    "hematology": "207RH0000X",
    "emergency medicine": "207P00000X",
    "physical therapy": "225100000X",
    "dentistry": "122300000X",
    "podiatry": "213E00000X",
}

_UHC_QUESTION_PROMPT = """You classify questions for the UnitedHealthcare provider directory.
Return only one JSON object, with no prose and no markdown fences.

Fields:
- "type": one of "doctor_detail", "specialty_list",
  "specialty_facility_list", or "network_membership".
- "doctor_name": a doctor's full name, or an empty string.
- "npi": a 10-digit NPI if the user provides one, or an empty string.
- "specialty": the medical specialty, or an empty string.
- "facility": the hospital, clinic, or medical group, or an empty string.
- "network": a named UnitedHealthcare network or plan such as "Select Plus",
  "Choice Plus", or "NexusACO RP", or an empty string.
- "location": a city, state, ZIP code, or address, or an empty string.

How to choose the type:
- "doctor_detail": the user asks for details about one specific doctor.
- "specialty_list": the user asks for doctors in a specialty, without
  restricting the search to one named facility.
- "specialty_facility_list": the user asks for doctors in a specialty at a
  named hospital, clinic, or medical group.
- "network_membership": the user asks whether a specific doctor is in a named
  network/plan, optionally in a state. Example: "is Dr X in Select Plus in
  California?" or "double check Dr X is not in Select Plus in California."

Rules:
- Use the earlier conversation to resolve follow-up references such as
  "him", "her", "there", or "that hospital".
- Preserve a named doctor, NPI, specialty, facility, network, and location
  exactly when the user gives them.
- For network_membership, put the network in "network" and the state in
  "location"; do not put the network in "specialty".
- Use specific medical specialty wording such as "Thoracic Surgery",
  "Otolaryngology", "Cardiology", or "Surgical Oncology".
- A question about a neck lump treated with surgery should use
  "Otolaryngology" or "Head and Neck Surgery".
- Do not invent a doctor, NPI, facility, specialty, network, or location.

<conversation>
{{CONVERSATION}}
</conversation>

<question>
{{QUESTION}}
</question>

JSON:
"""

ANTHEM_PLAN_PROMPT = """You extract search parameters for the Anthem Provider Directory.
Return only one JSON object, with no prose and no markdown fences.

Fields:
- "doctor_name": a specific doctor's full name, or an empty string.
- "specialty": a medical specialty, such as "cardiology", "head and neck", or "lung", or an empty string.
- "facility": a hospital, clinic, lab, medical group, or medical school name, or an empty string.
- "location": a city, state, ZIP code, or street address, or an empty string.
- "plan": an Anthem plan or network name if the user names one, or an empty string.
- "keywords": any remaining useful search words, or an empty string.

Rules:
- Use the earlier conversation to resolve follow-up references such as "him", "her", "there", or "that hospital".
- If the user asks for a doctor by name, put the full name in doctor_name.
- If the user asks for doctors in a specialty, put the specialty in specialty.
- Preserve the user's exact specialty wording. For example, "pediatric otolaryngology" must stay "pediatric otolaryngology"; do not replace it with a broader category such as "head and neck".
- If the user asks for doctors at a hospital or clinic, put its name in facility. Do not also copy that facility into location unless the user separately gave a city or state.
- Put qualifiers such as "pediatric", "adult", or "interventional" in specialty when they describe the specialty, not in keywords.
- Do not invent a doctor, facility, specialty, or location that the user did not mention or imply.
- A question such as "find cardiologists in San Francisco" should produce specialty=cardiology and location=San Francisco.

<conversation>
{{CONVERSATION}}
</conversation>

<question>
{{QUESTION}}
</question>

JSON:
"""

ANTHEM_SPECIALTY_PROMPT = """You are an Anthem BlueCross provider-directory search specialist.
Choose the best provider specialty for the patient's question.

Return only one JSON object, with no prose and no markdown fences:
{
  "specialty": "...",
  "keywords": "..."
}

Rules:
- If the question names a medical specialty, preserve that specialty.
- If the question describes a body area, condition, or treatment, choose the
  specialty that actually treats it. Be specific, not generic.
- Prefer NPPES taxonomy wording such as "Otolaryngology", "Thoracic Surgery",
  "Surgical Oncology", "Cardiology", "Neurology", "Dermatology", or
  "Orthopaedic Surgery".
- Examples:
  - neck lump, neck bump, thyroid, throat, tonsil, sinus, ear, hearing,
    voice, or head and neck surgery -> "Otolaryngology"
  - lung tumor surgery, chest surgery, or esophageal surgery ->
    "Thoracic Surgery"
  - cancer surgery -> "Surgical Oncology"
  - heart or cardiac surgery -> "Thoracic Surgery"
  - brain or spine surgery -> "Neurological Surgery"
- "specialty" must be a medical specialty, not a hospital, clinic, city, or
  doctor name.
- "keywords" may contain extra anatomy or condition words, or be empty.
- Do not invent a doctor, facility, or location.

<current_search_plan>
{{CURRENT_PLAN}}
</current_search_plan>

<question>
{{QUESTION}}
</question>

JSON:
"""

ANTHEM_PROVIDER_PROFILE_PROMPT = """You are preparing a concise provider profile for a patient.

<verified_directory_facts>
{{FACTS}}
</verified_directory_facts>

<publications>
{{PUBLICATIONS}}
</publications>

<institution_profile>
{{PROFILE_PAGE}}
</institution_profile>

Return only one JSON object with these keys:
- "background": a 2-4 sentence introduction to the provider.
- "research_interests": the provider's research interests, based on the publication list and institution profile when available.
- "education": education and training.
- "awards": awards, board certifications, and recognition.
- "clinical_focus": the provider's clinical focus.

Rules:
- The verified directory facts are authoritative for name, NPI, specialty, address, phone, status, and publication list.
- If the institution profile is provided, prefer it for background, research interests, education, awards, and clinical focus. Summarize it accurately; do not copy it verbatim.
- If the verified directory facts include an education/training list or provider title, use them in "education" and "background".
- If the institution profile or directory facts list board certifications, licenses, awards, or honors, include them in "awards".
- Use the publication titles to describe research interests when they are available.
- For education and awards, use only details found in the institution profile or details you are confident are public. If you are not confident, write exactly "Not found in the retrieved public records."
- Do not invent publications, degrees, dates, awards, or affiliations.
- Do not add a sources section; the application adds one.
- Keep each section to 1-3 sentences.

JSON:
"""

_ANTHEM_TOKEN_LOCK = threading.Lock()

_ANTHEM_TOKEN_CACHE: dict[str, object] = {
    "value": "",
    "expires_at": 0.0,
}

_ANTHEM_PROVIDER_DETAIL_LOCK = threading.Lock()

_ANTHEM_PROVIDER_DETAIL_CACHE: dict[str, dict] = {}

_PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

_US_STATE_NAMES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
}

class AnthemFHIRError(RuntimeError):
    """A provider-directory request failed before usable records arrived."""

    def __init__(
        self, message: str, status: int = 0, url: str = ""
    ) -> None:
        super().__init__(message)
        self.status = status
        self.url = url

def _anthem_access_token() -> str:
    """Return an OAuth bearer token for a registered Anthem FHIR app."""
    token = os.environ.get("ANTHEM_FHIR_TOKEN", "").strip()
    if token:
        return token
    if not (
        ANTHEM_FHIR_TOKEN_URL
        and ANTHEM_FHIR_CLIENT_ID
        and ANTHEM_FHIR_CLIENT_SECRET
    ):
        return ""
    with _ANTHEM_TOKEN_LOCK:
        cached = str(_ANTHEM_TOKEN_CACHE.get("value") or "")
        expires_at = float(_ANTHEM_TOKEN_CACHE.get("expires_at") or 0.0)
        if cached and expires_at > time.time() + 60:
            return cached
        try:
            response = requests.post(
                ANTHEM_FHIR_TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(
                    ANTHEM_FHIR_CLIENT_ID,
                    ANTHEM_FHIR_CLIENT_SECRET,
                ),
                headers={"Accept": "application/json"},
                timeout=(10, 30),
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
            return ""
        value = str(data.get("access_token") or "").strip()
        if not value:
            return ""
        try:
            expires_in = int(data.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        _ANTHEM_TOKEN_CACHE["value"] = value
        _ANTHEM_TOKEN_CACHE["expires_at"] = time.time() + expires_in
        return value

def _anthem_fhir_bases() -> list[tuple[str, str]]:
    """Return candidate FHIR base URLs, most specific first."""
    override = os.environ.get("ANTHEM_FHIR_BASE_URL", "").strip()
    if override:
        return [("configured", override.rstrip("/"))]
    bases: list[tuple[str, str]] = []
    if _anthem_access_token():
        bases.append(
            (
                "registered " + ANTHEM_FHIR_BRAND,
                ANTHEM_FHIR_REGISTERED_BASE,
            )
        )
    bases.append(("public", ANTHEM_FHIR_PUBLIC_BASE))
    return bases

def _anthem_json_object(raw: str) -> dict:
    """Parse a JSON object from a model reply, tolerating fences/prose."""
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    candidates = [text]
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return {}

def _anthem_split_name(name: str) -> tuple[str, str]:
    """Split a person name into given and family parts for NPPES."""
    value = str(name or "")
    value = re.sub(
        r"\b(?:M\.?\s?D\.?|D\.?\s?O\.?|P\.?\s?h\.?\s?D\.?|"
        r"M\.?\s?P\.?\s?H\.?|M\.?\s?S\.?|M\.?\s?A\.?|"
        r"R\.?\s?N\.?|N\.?\s?P\.?|P\.?\s?A\.?-?C\.?|"
        r"F\.?\s?A\.?\s?C\.?\s?P\.?|F\.?\s?A\.?\s?C\.?\s?S\.?|"
        r"Inc\.?|LLC|Ltd\.?)\b\.?",
        " ",
        value,
        flags=re.IGNORECASE,
    )
    value = re.sub(
        r"^\s*(?:dr\.?|doctor)\s+", "", value, flags=re.IGNORECASE
    )
    value = re.sub(r"^[\s,]+", "", value)
    value = re.sub(r"[\s,]+$", "", value)
    value = " ".join(value.replace(",", " , ").split())
    if not value:
        return "", ""
    if " , " in value:
        family, given = value.split(" , ", 1)
        return given.strip(" ,"), family.strip(" ,")
    parts = [part for part in value.split() if part]
    if len(parts) == 1:
        return "", parts[0]
    return " ".join(parts[:-1]), parts[-1]

def _anthem_parse_location(location: str) -> tuple[str, str, str]:
    """Return (city, state, postal code) from a free-form location string."""
    text = " ".join(str(location or "").split())
    if not text:
        return "", "", ""
    postal = ""
    match = re.search(r"\b(\d{5}(?:-\d{4})?)\b", text)
    if match:
        postal = match.group(1)
        text = (text[: match.start()] + " " + text[match.end() :]).strip()
    state = ""
    lower = text.casefold()
    for full, abbreviation in _US_STATE_NAMES.items():
        if re.search(rf"\b{re.escape(full)}\b", lower):
            state = abbreviation
            text = re.sub(
                rf"\b{re.escape(full)}\b",
                " ",
                text,
                flags=re.IGNORECASE,
            ).strip()
            break
    if not state:
        match = re.search(r"(?:,\s*|\b)([A-Z]{2})\s*$", text)
        if match:
            candidate = match.group(1).upper()
            if candidate in set(_US_STATE_NAMES.values()):
                state = candidate
                text = text[: match.start()].strip()
    city = text.strip(" ,")
    return city, state, postal

def _anthem_clean_plan(data: dict, question: str) -> dict[str, str]:
    """Normalize the model's search plan and fill harmless defaults."""
    keys = (
        "doctor_name",
        "specialty",
        "facility",
        "location",
        "plan",
        "keywords",
    )
    plan = {
        key: " ".join(str(data.get(key, "") or "").split()) for key in keys
    }
    plan["doctor_name"] = re.sub(
        r"^\s*(?:dr\.?|doctor)\s+",
        "",
        plan["doctor_name"],
        flags=re.IGNORECASE,
    )
    if not any(
        plan[key]
        for key in ("doctor_name", "specialty", "facility", "location")
    ):
        plan["keywords"] = plan["keywords"] or question
    return plan

def _anthem_plan_fallback(question: str) -> dict[str, str]:
    """A small deterministic plan used when the planning model is unavailable."""
    plan = {key: "" for key in (
        "doctor_name",
        "specialty",
        "facility",
        "location",
        "plan",
        "keywords",
    )}
    match = re.search(
        r"\b(?:dr\.?|doctor)\s+([A-Z][A-Za-z'.-]+"
        r"(?:\s+[A-Z][A-Za-z'.-]+){0,3})",
        question,
    )
    if match:
        plan["doctor_name"] = match.group(1)
    specialties = {
        "cardiologist": "Cardiology",
        "cardiology": "Cardiology",
        "dermatologist": "Dermatology",
        "dermatology": "Dermatology",
        "neurologist": "Neurology",
        "neurology": "Neurology",
        "oncologist": "Oncology",
        "oncology": "Oncology",
        "pediatrician": "Pediatrics",
        "pediatrics": "Pediatrics",
        "surgeon": "Surgery",
        "surgery": "Surgery",
        "urologist": "Urology",
        "urology": "Urology",
        "head and neck": "Head and Neck",
        "lung": "Pulmonology",
        "heart": "Cardiology",
    }
    lower = question.casefold()
    for term, canonical in specialties.items():
        if term in lower:
            plan["specialty"] = canonical
            break
    match = re.search(
        r"\b(?:in|near|around)\s+([A-Z][A-Za-z .'-]+"
        r"(?:,\s*[A-Z]{2})?)",
        question,
    )
    if match:
        plan["location"] = match.group(1).strip(" ,.")
    if not any(
        plan[key]
        for key in ("doctor_name", "specialty", "facility", "location")
    ):
        plan["keywords"] = question
    return plan

_ANTHEM_SPECIALTY_PHRASES = [
    ("pediatric otolaryngology", "pediatric otolaryngology"),
    ("paediatric otolaryngology", "pediatric otolaryngology"),
    ("pediatric cardiology", "pediatric cardiology"),
    ("interventional cardiology", "Interventional Cardiology"),
    (
        "advanced heart failure",
        "Advanced Heart Failure and Transplant Cardiology",
    ),
    ("head and neck", "head and neck"),
    ("ear nose and throat", "Otolaryngology"),
    ("otolaryngology", "Otolaryngology"),
    ("otorhinolaryngology", "Otolaryngology"),
    ("ent", "Otolaryngology"),
    ("cardiology", "Cardiology"),
    ("cardiologist", "Cardiology"),
    ("heart", "Cardiology"),
    ("pulmonology", "Pulmonology"),
    ("pulmonologist", "Pulmonology"),
    ("lung", "Pulmonology"),
    ("dermatology", "Dermatology"),
    ("dermatologist", "Dermatology"),
    ("neurology", "Neurology"),
    ("neurologist", "Neurology"),
    ("oncology", "Oncology"),
    ("oncologist", "Oncology"),
    ("pediatrics", "Pediatrics"),
    ("pediatrician", "Pediatrics"),
    ("pediatric", "Pediatrics"),
    ("urology", "Urology"),
    ("urologist", "Urology"),
    ("orthopedics", "Orthopedics"),
    ("orthopedic", "Orthopedics"),
    ("gastroenterology", "Gastroenterology"),
    ("gastroenterologist", "Gastroenterology"),
    ("endocrinology", "Endocrinology"),
    ("endocrinologist", "Endocrinology"),
    ("psychiatry", "Psychiatry"),
    ("psychiatrist", "Psychiatry"),
    ("ophthalmology", "Ophthalmology"),
    ("ophthalmologist", "Ophthalmology"),
    ("obstetrics", "Obstetrics"),
    ("gynecology", "Gynecology"),
    ("anesthesiology", "Anesthesiology"),
    ("radiology", "Radiology"),
    ("pathology", "Pathology"),
    ("family medicine", "Family Medicine"),
    ("internal medicine", "Internal Medicine"),
    ("infectious disease", "Infectious Disease"),
    ("immunology", "Immunology"),
    ("rheumatology", "Rheumatology"),
    ("nephrology", "Nephrology"),
    ("hematology", "Hematology"),
    ("emergency medicine", "Emergency Medicine"),
    ("physical therapy", "Physical Therapy"),
    ("chiropractic", "Chiropractic"),
    ("dentistry", "Dentistry"),
    ("podiatry", "Podiatry"),
    ("surgery", "Surgery"),
    ("surgeon", "Surgery"),
    ("vascular", "Vascular Surgery"),
]

_ANTHEM_SPECIALTY_PHRASES.sort(
    key=lambda item: len(item[0]), reverse=True
)

_ANTHEM_FACILITY_NAMES = [
    ("ucsf benioff children's", "UCSF Benioff Children's"),
    ("ucsf benioff childrens", "UCSF Benioff Children's"),
    ("university of california san francisco", "UCSF"),
    ("stanford health care", "Stanford Health Care"),
    ("massachusetts general", "Massachusetts General"),
    ("brigham and women's", "Brigham and Women's"),
    ("brigham and womens", "Brigham and Women's"),
    ("memorial sloan kettering", "Memorial Sloan Kettering"),
    ("johns hopkins", "Johns Hopkins"),
    ("cleveland clinic", "Cleveland Clinic"),
    ("mayo clinic", "Mayo Clinic"),
    ("md anderson", "MD Anderson"),
    ("kaiser permanente", "Kaiser Permanente"),
    ("cedars-sinai", "Cedars-Sinai"),
    ("cedars sinai", "Cedars-Sinai"),
    ("mount sinai", "Mount Sinai"),
    ("nyu langone", "NYU Langone"),
    ("dana-farber", "Dana-Farber"),
    ("dana farber", "Dana-Farber"),
    ("city of hope", "City of Hope"),
    ("sutter health", "Sutter Health"),
    ("dignity health", "Dignity Health"),
    ("children's hospital", "Children's Hospital"),
    ("childrens hospital", "Children's Hospital"),
    ("ucsf", "UCSF"),
    ("stanford health care", "Stanford"),
    ("stanford hospital", "Stanford"),
    ("stanford", "Stanford"),
    ("providence", "Providence"),
    ("scripps", "Scripps"),
    ("sharp", "Sharp"),
]

_ANTHEM_FACILITY_NAMES.sort(
    key=lambda item: len(item[0]), reverse=True
)

_ANTHEM_QUERY_CORRECTIONS = (
    (re.compile(r"\blunch\s+cancer\b", re.IGNORECASE), "lung cancer"),
    (re.compile(r"\blong\s+cancer\b", re.IGNORECASE), "lung cancer"),
    (re.compile(r"\blung\s+caner\b", re.IGNORECASE), "lung cancer"),
    (re.compile(r"\bstandford\b", re.IGNORECASE), "stanford"),
)

def _anthem_normalize_question(question: str) -> str:
    """Correct common spelling mistakes in provider search questions."""
    text = " ".join(str(question or "").split())
    for pattern, replacement in _ANTHEM_QUERY_CORRECTIONS:
        text = pattern.sub(replacement, text)
    return text

_ANTHEM_ADDRESS_SUFFIXES = {
    "AV": "AVE",
    "AVENUE": "AVE",
    "BLVD": "BLVD",
    "BOULEVARD": "BLVD",
    "CT": "CT",
    "COURT": "CT",
    "DR": "DR",
    "DRIVE": "DR",
    "HWY": "HWY",
    "HIGHWAY": "HWY",
    "LANE": "LN",
    "LN": "LN",
    "PKWY": "PKWY",
    "PARKWAY": "PKWY",
    "PL": "PL",
    "PLACE": "PL",
    "RD": "RD",
    "ROAD": "RD",
    "ST": "ST",
    "STREET": "ST",
    "TER": "TER",
    "TERRACE": "TER",
}

_ANTHEM_ADDRESS_UNIT_RE = re.compile(
    r"\s+(?:#|APT\b|BLDG\b|BUILDING\b|DEPT\b|FL\b|FLOOR\b|MC\b|"
    r"RM\b|ROOM\b|STE\b|SUITE\b|UNIT\b|BOX\b).*$",
    re.IGNORECASE,
)

def _anthem_normalize_street(value: str) -> str:
    """Return a comparable street line without unit details."""
    text = " ".join(str(value or "").upper().replace(",", " ").split())
    text = _ANTHEM_ADDRESS_UNIT_RE.sub("", text).strip()
    words = text.split()
    if words:
        words[-1] = _ANTHEM_ADDRESS_SUFFIXES.get(words[-1], words[-1])
    return " ".join(words)

def _anthem_address_keys(addresses) -> set[tuple[str, str]]:
    """Return ``(normalized street, ZIP5)`` keys for practice addresses."""
    keys: set[tuple[str, str]] = set()
    for address in addresses or []:
        if not isinstance(address, dict):
            continue
        purpose = str(address.get("address_purpose") or "").upper()
        if purpose and purpose != "LOCATION":
            continue
        street = _anthem_normalize_street(address.get("address_1"))
        postal = str(address.get("postal_code") or "").strip()[:5]
        if street:
            keys.add((street, postal))
    return keys

def _anthem_facility_aliases(facility: str) -> set[str]:
    """Return the names a facility may use in a directory record."""
    name = " ".join(str(facility or "").casefold().split())
    aliases = {name} if name else set()
    if not name:
        return aliases
    for phrase, display in _ANTHEM_FACILITY_NAMES:
        phrase_key = phrase.casefold()
        display_key = display.casefold()
        if (
            phrase_key in name
            or name in phrase_key
            or display_key in name
            or name in display_key
        ):
            aliases.update({phrase_key, display_key})
    return aliases

def _anthem_name_matches_facility(name: str, facility: str) -> bool:
    """Return whether a directory name refers to the requested facility."""
    text = " ".join(str(name or "").casefold().split())
    if not text:
        return False
    return any(
        alias and alias in text
        for alias in _anthem_facility_aliases(facility)
    )

def _anthem_facility_profile_supported(facility: str) -> bool:
    """Return whether we can verify affiliation through profile pages."""
    key = " ".join(str(facility or "").casefold().split())
    return "ucsf" in key or "stanford" in key

_ANTHEM_CITY_STATES = {
    "san francisco": "San Francisco, CA",
    "los angeles": "Los Angeles, CA",
    "san diego": "San Diego, CA",
    "san jose": "San Jose, CA",
    "palo alto": "Palo Alto, CA",
    "mountain view": "Mountain View, CA",
    "redwood city": "Redwood City, CA",
    "san mateo": "San Mateo, CA",
    "long beach": "Long Beach, CA",
    "santa monica": "Santa Monica, CA",
    "new york": "New York, NY",
    "washington dc": "Washington, DC",
    "salt lake city": "Salt Lake City, UT",
    "las vegas": "Las Vegas, NV",
    "little rock": "Little Rock, AR",
    "des moines": "Des Moines, IA",
    "oklahoma city": "Oklahoma City, OK",
    "new orleans": "New Orleans, LA",
    "virginia beach": "Virginia Beach, VA",
    "colorado springs": "Colorado Springs, CO",
    "fort worth": "Fort Worth, TX",
    "san antonio": "San Antonio, TX",
    "charlotte": "Charlotte, NC",
    "raleigh": "Raleigh, NC",
    "nashville": "Nashville, TN",
    "minneapolis": "Minneapolis, MN",
    "detroit": "Detroit, MI",
    "philadelphia": "Philadelphia, PA",
    "baltimore": "Baltimore, MD",
    "pittsburgh": "Pittsburgh, PA",
    "boston": "Boston, MA",
    "chicago": "Chicago, IL",
    "houston": "Houston, TX",
    "dallas": "Dallas, TX",
    "austin": "Austin, TX",
    "seattle": "Seattle, WA",
    "portland": "Portland, OR",
    "phoenix": "Phoenix, AZ",
    "denver": "Denver, CO",
    "atlanta": "Atlanta, GA",
    "miami": "Miami, FL",
    "orlando": "Orlando, FL",
    "tampa": "Tampa, FL",
    "sacramento": "Sacramento, CA",
    "oakland": "Oakland, CA",
    "berkeley": "Berkeley, CA",
    "fremont": "Fremont, CA",
    "sunnyvale": "Sunnyvale, CA",
    "irvine": "Irvine, CA",
    "pasadena": "Pasadena, CA",
    "torrance": "Torrance, CA",
    "greenbrae": "Greenbrae, CA",
    "honolulu": "Honolulu, HI",
    "anchorage": "Anchorage, AK",
}

_ANTHEM_CITY_NAMES = sorted(
    _ANTHEM_CITY_STATES, key=len, reverse=True
)

_ANTHEM_QUALIFIER_WORDS = [
    "pediatric",
    "paediatric",
    "adult",
    "interventional",
    "transplant",
    "sports",
    "spine",
    "head",
    "neck",
    "heart",
    "lung",
    "breast",
    "cardiac",
    "vascular",
    "robotic",
    "minimally invasive",
]

def _anthem_fast_facility(question: str) -> str:
    """Return a named facility from a question without calling a model."""
    text = " ".join(str(question or "").split())
    lower = text.casefold()
    for phrase, display in _ANTHEM_FACILITY_NAMES:
        if re.search(rf"\b{re.escape(phrase)}\b", lower):
            return display
    match = re.search(
        r"\b((?:[A-Z][A-Za-z'&.-]+\s+){0,4}"
        r"(?:Hospital|Medical Center|Health System|Clinic|"
        r"Cancer Center|Children's|Childrens))\b",
        text,
    )
    if match:
        value = match.group(1).strip()
        value = re.sub(
            r"^(?:At|In|Near|The)\s+", "", value
        ).strip()
        return value
    return ""

def _anthem_fast_specialty(question: str) -> str:
    """Return a known specialty phrase without calling a model."""
    lower = " ".join(str(question or "").casefold().split())
    for phrase, canonical in _ANTHEM_SPECIALTY_PHRASES:
        if re.search(
            rf"\b{re.escape(phrase)}(?:s|es)?\b", lower
        ):
            return canonical
    return ""

def _anthem_fast_location(
    question: str,
    facility: str = "",
    specialty: str = "",
) -> str:
    """Return a city/state from a question without calling a model."""
    text = " ".join(str(question or "").split())
    lower = text.casefold()
    for city in _ANTHEM_CITY_NAMES:
        if re.search(rf"\b{re.escape(city)}\b", lower):
            return _ANTHEM_CITY_STATES[city]
    for full_name, abbreviation in _US_STATE_NAMES.items():
        if re.search(rf"\b{re.escape(full_name)}\b", lower):
            return abbreviation
    match = re.search(r"(?:,\s*|\bin\s+)([A-Z]{2})\b", text)
    if match:
        candidate = match.group(1).upper()
        if candidate in set(_US_STATE_NAMES.values()):
            return candidate
    match = re.search(
        r"\b(?:in|near|around)\s+"
        r"([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,3})",
        text,
    )
    if not match:
        return ""
    value = match.group(1).strip(" ,.")
    value = re.sub(
        r"\s+(?:at|near|with)$", "", value, flags=re.IGNORECASE
    ).strip()
    lowered = value.casefold()
    if lowered in {"the", "a", "an", "my", "area", "me", "network"}:
        return ""
    if facility and (
        lowered in facility.casefold()
        or facility.casefold() in lowered
    ):
        return ""
    if specialty and lowered in specialty.casefold():
        return ""
    if any(
        lowered == phrase for phrase, _canonical in _ANTHEM_SPECIALTY_PHRASES
    ):
        return ""
    return value

def _anthem_fast_plan(question: str) -> dict[str, str]:
    """Build a provider search plan with fast local rules.

    This is the primary path for the result list: it avoids a model call so
    the API results can appear immediately. The slower question model is only
    used as a fallback when these rules find no search fields.
    """
    text = _anthem_normalize_question(question)
    text = " ".join(text.split())
    lower = text.casefold()
    plan = {
        "doctor_name": "",
        "specialty": "",
        "facility": "",
        "location": "",
        "plan": "",
        "keywords": "",
    }
    doctor_match = re.search(
        r"\b(?i:dr\.?|doctor)\s+"
        r"([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){0,3})",
        text,
    )
    if not doctor_match:
        doctor_match = re.search(
            r"\b(?i:named|called)\s+"
            r"([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){0,3})",
            text,
        )
    if not doctor_match:
        doctor_match = re.search(
            r"\b([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){1,3})"
            r"\s*,?\s*(?:M\.?D\.?|D\.?O\.?|Ph\.?D\.?|FACS)\b",
            text,
        )
    if not doctor_match:
        # Detail questions often omit credentials: "tell me about Jane Doe".
        # Only accept a multi-word capitalized name and reject phrases that
        # are clearly specialties, facilities, or care settings.
        detail_match = re.search(
            r"\b(?i:tell me about|who is|describe|profile of|"
            r"background of)\s+"
            r"([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){1,3})",
            text,
        )
        if detail_match:
            candidate = detail_match.group(1).strip()
            candidate_key = candidate.casefold()
            generic_terms = (
                "cancer",
                "center",
                "hospital",
                "clinic",
                "health",
                "medical",
                "medicine",
                "surgery",
                "specialist",
                "specialists",
                "doctor",
                "doctors",
                "department",
                "institute",
                "care",
                "insurance",
                "network",
            )
            if not any(
                term in candidate_key for term in generic_terms
            ):
                doctor_match = detail_match
    if not doctor_match:
        # A bare "Name from Facility" question is common and may omit
        # "doctor", credentials, or a detail phrase. Accept a capitalized
        # name before from/at, but reject specialties, body areas, and
        # locations so "Head and Neck from UCSF" stays a specialty search.
        name_from_match = re.search(
            r"\b([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){0,3})\s+"
            r"(?:from|at)\s+"
            r"([A-Z][A-Za-z0-9'&., -]{1,60})",
            text,
        )
        if name_from_match:
            candidate = name_from_match.group(1).strip()
            candidate_key = candidate.casefold()
            generic_terms = (
                "cancer",
                "center",
                "hospital",
                "clinic",
                "health",
                "medical",
                "medicine",
                "surgery",
                "specialist",
                "specialists",
                "doctor",
                "doctors",
                "department",
                "institute",
                "care",
                "insurance",
                "network",
            )
            body_terms = {
                "head",
                "neck",
                "throat",
                "thyroid",
                "lung",
                "heart",
                "cardiac",
                "brain",
                "spine",
                "back",
                "knee",
                "hip",
                "skin",
                "breast",
                "stomach",
                "abdomen",
                "liver",
                "kidney",
                "eye",
                "prostate",
                "bone",
            }
            if (
                not any(
                    term in candidate_key for term in generic_terms
                )
                and not any(
                    re.search(rf"\b{re.escape(term)}\b", candidate_key)
                    for term in body_terms
                )
                and candidate_key not in _US_STATE_NAMES
                and candidate_key not in _ANTHEM_CITY_NAMES
            ):
                doctor_match = name_from_match
    if not doctor_match:
        candidate = text.strip(" .,")
        words = candidate.split()
        credentials = {
            "md",
            "m.d.",
            "do",
            "d.o.",
            "phd",
            "ph.d.",
            "facs",
            "facc",
            "mbbs",
            "mph",
            "mba",
            "ms",
            "msc",
            "rn",
            "np",
            "pa",
        }
        while words and words[-1].casefold().strip(".,") in credentials:
            words.pop()
        candidate = " ".join(words)
        candidate_key = candidate.casefold()
        generic_terms = (
            "cancer",
            "center",
            "hospital",
            "clinic",
            "health",
            "medical",
            "medicine",
            "surgery",
            "specialist",
            "specialists",
            "doctor",
            "doctors",
            "department",
            "institute",
            "care",
            "insurance",
            "network",
        )
        body_terms = {
            "head",
            "neck",
            "throat",
            "thyroid",
            "lung",
            "heart",
            "cardiac",
            "brain",
            "spine",
            "back",
            "knee",
            "hip",
            "skin",
            "breast",
            "stomach",
            "abdomen",
            "liver",
            "kidney",
            "eye",
            "prostate",
            "bone",
        }
        if (
            2 <= len(words) <= 4
            and all(
                re.fullmatch(r"[A-Z][A-Za-z'.-]*", word)
                for word in words
            )
            and not any(
                term in candidate_key for term in generic_terms
            )
            and not any(
                re.search(rf"\b{re.escape(term)}\b", candidate_key)
                for term in body_terms
            )
            and candidate_key not in _US_STATE_NAMES
            and candidate_key not in _ANTHEM_CITY_NAMES
        ):
            doctor_match = re.match(r"(.*)", candidate)
    if doctor_match:
        plan["doctor_name"] = doctor_match.group(1)
    plan["facility"] = _anthem_fast_facility(text)
    plan["specialty"] = _anthem_fast_specialty(text)
    if plan["doctor_name"]:
        doctor_key = plan["doctor_name"].casefold()
        if (
            plan["facility"]
            and doctor_key in plan["facility"].casefold()
        ) or (
            plan["specialty"]
            and doctor_key in plan["specialty"].casefold()
        ):
            plan["doctor_name"] = ""
    plan["location"] = _anthem_fast_location(
        text, plan["facility"], plan["specialty"]
    )
    if plan["doctor_name"] and re.search(
        r",\s*(?:M\.?D\.?|D\.?O\.?|Ph\.?D\.?|FACS)\b",
        text,
        re.IGNORECASE,
    ):
        # "Kratz, MD" is a credential, not Maryland.
        if plan["location"].upper() in {"MD", "DO"}:
            plan["location"] = ""
    qualifiers = []
    for qualifier in _ANTHEM_QUALIFIER_WORDS:
        if (
            re.search(rf"\b{re.escape(qualifier)}\b", lower)
            and qualifier not in plan["specialty"].casefold()
        ):
            qualifiers.append(qualifier)
    plan["keywords"] = " ".join(dict.fromkeys(qualifiers))
    return _anthem_clean_plan(plan, question)

_ANTHEM_GENERIC_SPECIALTIES = {
    "surgery",
    "surgeon",
    "medicine",
    "internal medicine",
    "family medicine",
    "general practice",
    "primary care",
}

_ANTHEM_BODY_AREA_TERMS = {
    "head",
    "neck",
    "throat",
    "thyroid",
    "voice",
    "ear",
    "nose",
    "sinus",
    "tonsil",
    "lung",
    "chest",
    "thoracic",
    "heart",
    "cardiac",
    "brain",
    "spine",
    "back",
    "knee",
    "hip",
    "shoulder",
    "skin",
    "breast",
    "stomach",
    "abdomen",
    "liver",
    "kidney",
    "eye",
    "prostate",
    "bone",
    "tumor",
    "tumour",
    "bump",
    "lump",
    "mass",
}

_ANTHEM_SPECIALTY_CACHE: dict[str, dict[str, str]] = {}

_ANTHEM_SPECIALTY_LOCK = threading.Lock()

def _anthem_needs_smart_specialty(
    question: str,
    plan: dict[str, str],
) -> bool:
    """Return whether the fast plan needs a model-detected specialty."""
    if plan.get("doctor_name"):
        return False
    specialty = " ".join(
        str(plan.get("specialty") or "").casefold().split()
    )
    text = " ".join(
        [
            str(question or ""),
            str(plan.get("keywords") or ""),
        ]
    ).casefold()
    if not specialty:
        return bool(plan.get("keywords"))
    if specialty not in _ANTHEM_GENERIC_SPECIALTIES:
        return False
    return any(
        re.search(rf"\b{re.escape(term)}\b", text)
        for term in _ANTHEM_BODY_AREA_TERMS
    )

def _anthem_smart_specialty(
    question: str,
    plan: dict[str, str],
) -> dict[str, str]:
    """Use the question model to choose a specific provider specialty.

    The fast parser often sees only the treatment word (for example
    "surgery") and misses the body area (for example a neck lump). The model
    is asked for a short NPPES-style specialty so the directory search uses a
    clinically appropriate taxonomy instead of a broad one.
    """
    question = " ".join(str(question or "").split())
    if not question:
        return {}
    key = question.casefold()
    with _ANTHEM_SPECIALTY_LOCK:
        cached = _ANTHEM_SPECIALTY_CACHE.get(key)
    if cached is not None:
        return dict(cached)
    current_plan = json.dumps(
        {
            key: str(plan.get(key) or "")
            for key in (
                "specialty",
                "keywords",
                "facility",
                "location",
            )
        },
        ensure_ascii=False,
    )
    prompt = ANTHEM_SPECIALTY_PROMPT.replace(
        "{{CURRENT_PLAN}}", current_plan
    ).replace("{{QUESTION}}", question)
    try:
        raw = chat.generate_text(
            prompt,
            model=ANTHEM_MODEL,
            temperature=0.0,
            num_predict=200,
            num_ctx=chat.ANSWER_NUM_CTX,
        )
    except RuntimeError:
        return {}
    parsed = _anthem_json_object(raw)
    if not isinstance(parsed, dict):
        return {}
    specialty = " ".join(
        str(parsed.get("specialty") or "").split()
    )
    specialty = re.sub(
        r"\s*\(.*\)\s*$", "", specialty
    ).strip(" .,;")
    keywords = " ".join(str(parsed.get("keywords") or "").split())
    if specialty.casefold() in {
        "",
        "none",
        "unknown",
        "n/a",
        "not applicable",
    }:
        return {}
    result = {"specialty": specialty, "keywords": keywords}
    with _ANTHEM_SPECIALTY_LOCK:
        _ANTHEM_SPECIALTY_CACHE[key] = dict(result)
    return dict(result)

def _anthem_select_provider(
    question: str,
    providers: list[dict],
) -> dict | None:
    """Pick the provider a follow-up refers to, when possible."""
    if not providers:
        return None
    lower = " ".join(str(question or "").casefold().split())
    for word, index in (
        ("first", 0),
        ("1st", 0),
        ("second", 1),
        ("2nd", 1),
        ("third", 2),
        ("3rd", 2),
        ("last", len(providers) - 1),
    ):
        if re.search(rf"\b{word}\b", lower) and index < len(providers):
            return providers[index]
    for provider in providers:
        name = str(
            provider.get("name")
            or provider.get("title")
            or ""
        ).casefold()
        parts = [
            part
            for part in re.findall(r"[a-z]+", name)
            if len(part) > 2 and part not in {"md", "do", "dr"}
        ]
        if not parts:
            continue
        if parts[-1] in lower:
            return provider
        if all(part in lower for part in parts[:2]):
            return provider
    if len(providers) == 1:
        return providers[0]
    return None

_ANTHEM_DETAIL_REQUEST_RE = re.compile(
    r"\b(?:"
    r"tell me more|tell me about|more about|details?|profile|background|"
    r"biography|bio|education|training|awards?|recognition|"
    r"publications?|research|who is|experience|qualifications?|describe|"
    r"overview"
    r")\b",
    re.IGNORECASE,
)

def _anthem_is_detail_request(question: str) -> bool:
    """Return whether a follow-up asks for a provider's full profile."""
    return bool(_ANTHEM_DETAIL_REQUEST_RE.search(str(question or "")))

_ANTHEM_LIST_INTENT_RE = re.compile(
    r"\b(?:list|doctors|physicians|specialists|surgeons|providers|"
    r"options|candidates)\b",
    re.IGNORECASE,
)

def _anthem_should_show_provider_detail(
    question: str,
    plan: dict[str, str],
) -> bool:
    """Return whether a named doctor should load the full detail directly.

    Questions such as "Johannes Kratz", "Johannes Kratz from UCSF", and
    "doctor Johannes Kratz from UCSF" identify one doctor even without a
    detail phrase. Only explicit list/plural-provider wording stays a search.
    """
    if not plan.get("doctor_name"):
        return False
    if _anthem_is_detail_request(question):
        return True
    return not _ANTHEM_LIST_INTENT_RE.search(str(question or ""))

_ANTHEM_NUMBER_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
}

_ANTHEM_SIMILAR_NUMBER_PATTERN = "|".join(
    sorted(_ANTHEM_NUMBER_WORDS, key=len, reverse=True)
)

_ANTHEM_SIMILAR_RE = re.compile(
    rf"(?i:\b(?:show|find|give|list|get)?\s*(?:me\s+)?"
    rf"({_ANTHEM_SIMILAR_NUMBER_PATTERN}|\d+)?\s*"
    r"(?:similar\s+(?:doctor|doctors|provider|providers)|"
    r"(?:doctor|doctors|provider|providers)\s+similar)\s+to\s+"
    r"(?:doctor|dr\.?|doctor\s+)?\s*)"
    r"([A-Z][A-Za-z'.-]+(?:\s+[A-Z][A-Za-z'.-]+){0,3})"
)

def _anthem_similar_request(question: str) -> dict | None:
    """Parse "show me N doctors similar to Dr X" requests."""
    text = _anthem_normalize_question(question)
    match = _ANTHEM_SIMILAR_RE.search(text)
    if not match:
        return None
    doctor_name = " ".join(match.group(2).split())
    if not doctor_name or len(doctor_name.split()) < 2:
        return None
    doctor_key = doctor_name.casefold()
    if (
        _anthem_fast_specialty(doctor_name)
        or _anthem_fast_facility(doctor_name)
        or doctor_key in _US_STATE_NAMES
        or doctor_key in _ANTHEM_CITY_NAMES
    ):
        return None
    raw_limit = str(match.group(1) or "").strip().casefold()
    if raw_limit.isdigit():
        limit = int(raw_limit)
    else:
        limit = _ANTHEM_NUMBER_WORDS.get(raw_limit, 5)
    limit = max(1, min(limit, 50))
    return {
        "limit": limit,
        "doctor_name": doctor_name,
        "facility": _anthem_fast_facility(text),
    }

def _anthem_infer_target_specialty(detail: dict) -> str:
    """Infer a searchable specialty from a provider's detail text."""
    section = str(detail.get("section") or "").strip()
    text = " ".join(
        [section]
        + [
            str(paragraph or "")
            for paragraph in detail.get("paragraphs") or []
        ]
    ).casefold()
    rules = (
        (
            (
                "thoracic",
                "esophageal",
                "esophagus",
                "lung cancer",
                "lung tumor",
                "chest wall",
                "mediastin",
            ),
            "Thoracic Surgery",
        ),
        (
            (
                "head and neck",
                "otolaryng",
                "larynx",
                "thyroid",
                "salivary",
                "tonsil",
                "sinus",
            ),
            "Otolaryngology",
        ),
        (
            ("surgical oncology", "cancer surgery"),
            "Surgical Oncology",
        ),
        (("cardiac surgery", "heart surgery"), "Thoracic Surgery"),
        (("cardio", "heart"), "Cardiology"),
        (("neuro", "brain", "spine"), "Neurology"),
        (("dermat", "skin"), "Dermatology"),
        (("orthop", "bone", "joint"), "Orthopaedic Surgery"),
        (("gastro", "colon", "liver"), "Gastroenterology"),
        (("urolog", "prostate", "bladder", "kidney"), "Urology"),
    )
    for terms, specialty in rules:
        if any(term in text for term in terms):
            return specialty
    return section

def _anthem_detail_section_text(item: dict, heading: str) -> str:
    """Return the paragraph text under one detail-modal heading."""
    target = " ".join(str(heading or "").casefold().split())
    collecting = False
    texts: list[str] = []
    for block in item.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind == "heading":
            collecting = (
                " ".join(str(block.get("text") or "").casefold().split())
                == target
            )
        elif collecting and kind == "paragraph":
            text = " ".join(str(block.get("text") or "").split())
            if text:
                texts.append(text)
    return " ".join(texts)

def _anthem_detail_npi(item: dict) -> str:
    """Extract the NPI line from a provider detail modal payload."""
    for block in item.get("blocks") or []:
        if not isinstance(block, dict) or block.get("type") != "list":
            continue
        for value in block.get("items") or []:
            text = str(value or "").strip()
            if text.upper().startswith("NPI:"):
                return text.split(":", 1)[1].strip()
    return ""

def _anthem_similarity_terms(detail: dict) -> set[str]:
    """Return research-interest terms used to rank similar doctors."""
    text = _anthem_detail_section_text(detail, "Research Interests")
    if not text:
        text = " ".join(
            str(paragraph or "")
            for paragraph in detail.get("paragraphs") or []
        )
    stop = {
        "about",
        "after",
        "also",
        "among",
        "based",
        "clinical",
        "current",
        "department",
        "director",
        "during",
        "education",
        "faculty",
        "health",
        "hospital",
        "including",
        "medical",
        "medicine",
        "patients",
        "professor",
        "provider",
        "published",
        "research",
        "school",
        "specializes",
        "specializing",
        "their",
        "there",
        "these",
        "through",
        "trained",
        "treating",
        "treatment",
        "university",
        "which",
        "while",
        "with",
    }
    return {
        word
        for word in re.findall(r"[a-z]{5,}", text.casefold())
        if word not in stop
    }

_ANTHEM_PROVIDER_INTENT_RE = re.compile(
    r"\b(?:doctor|doctors|physician|physicians|specialist|specialists|"
    r"provider|providers|surgeon|surgeons)\b",
    re.IGNORECASE,
)

_ANTHEM_PROVIDER_SEARCH_RE = re.compile(
    r"\b(?:find|list|show me|give me|search|look for|looking for|"
    r"recommend|which|who|tell me about|i want)\b",
    re.IGNORECASE,
)

def _anthem_online_tool_matches(question: str) -> bool:
    """Return whether an Online-mode question should use the Anthem tool.

    The provider directory is invoked for the two clear cases:
    a named doctor with a detail request, or a provider-search request that
    mentions a doctor, specialty, or facility. Ordinary news questions that
    merely mention doctors are left to the Online research flow.
    """
    text = " ".join(str(question or "").split())
    if not text:
        return False
    plan = _anthem_fast_plan(text)
    if plan.get("doctor_name") and _anthem_is_detail_request(text):
        return True
    if not _ANTHEM_PROVIDER_SEARCH_RE.search(text):
        return False
    if plan.get("specialty") and (
        plan.get("facility") or plan.get("location")
    ):
        return True
    if not _ANTHEM_PROVIDER_INTENT_RE.search(text):
        return False
    return bool(
        plan.get("doctor_name")
        or plan.get("specialty")
        or plan.get("facility")
    )

_ANTHEM_LIMIT_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
}

_ANTHEM_LIMIT_RE = re.compile(
    r"\b(?:show|list|give|find|display|recommend)\b.{0,40}?"
    r"\b(\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
    r"eighteen|nineteen|twenty|thirty|forty|fifty)\b",
    re.IGNORECASE,
)

def _anthem_requested_limit(question: str) -> int:
    """Return the requested doctor count from a provider question.

    Examples: ``show me 10 head and neck doctors in UCSF`` or
    ``find five specialists at MD Anderson``. A missing count returns 0.
    """
    text = " ".join(str(question or "").casefold().split())
    match = _ANTHEM_LIMIT_RE.search(text)
    if not match:
        return 0
    value = match.group(1)
    number = int(value) if value.isdigit() else _ANTHEM_LIMIT_WORDS.get(value, 0)
    if number <= 0:
        return 0
    return min(number, 100)

def _anthem_provider_detail_text(item: dict) -> str:
    """Render provider-detail blocks as readable chat text."""
    lines: list[str] = []
    for block in item.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        kind = str(block.get("type") or "")
        if kind == "heading":
            text = str(block.get("text") or "").strip()
            if text:
                if lines:
                    lines.append("")
                lines.append(text)
        elif kind == "paragraph":
            text = " ".join(str(block.get("text") or "").split())
            if text:
                lines.append(text)
        elif kind == "list":
            for value in block.get("items") or []:
                text = " ".join(str(value or "").split())
                if text:
                    lines.append(f"- {text}")
    return "\n".join(lines).strip()

def _anthem_follow_up_answer(
    question: str,
    providers: list[dict],
) -> str | None:
    """Answer a simple provider follow-up directly from stored API records."""
    if not providers:
        return None
    lower = " ".join(str(question or "").casefold().split())
    target = _anthem_select_provider(question, providers)

    def label(provider: dict) -> str:
        return str(
            provider.get("name")
            or provider.get("title")
            or "Provider"
        )

    def values(field: str, empty: str) -> str:
        if target is not None:
            value = str(target.get(field) or "").strip()
            return f"{label(target)}: {value or empty}"
        lines = []
        for provider in providers:
            value = str(provider.get(field) or "").strip()
            lines.append(f"{label(provider)}: {value or empty}")
        return "\n".join(lines)

    if any(
        word in lower
        for word in ("phone", "telephone", "call", "fax", "contact")
    ):
        if "fax" in lower:
            return values("fax", "fax not listed")
        return values("phone", "phone not listed")
    if any(
        word in lower
        for word in ("address", "where", "location", "located")
    ):
        return values("address", "address not listed")
    if "specialty" in lower or "special" in lower:
        return values("specialty", "specialty not listed")
    if "network" in lower or "accepting" in lower or "accept" in lower:
        return (
            "The current provider records do not confirm Anthem network "
            "participation or whether a provider is accepting new patients."
        )
    if "how many" in lower or "count" in lower:
        return (
            f"{len(providers)} provider(s) are in the current result set."
        )
    if "list" in lower and any(
        word in lower for word in ("name", "doctor", "provider")
    ):
        return "\n".join(
            f"{index}. {label(provider)}"
            for index, provider in enumerate(providers, start=1)
        )
    if target is not None:
        summary = str(
            target.get("summary")
            or target.get("address")
            or ""
        ).strip()
        return f"{label(target)} — {summary}" if summary else label(target)
    return None

def _anthem_search_plan(question: str, conversation: str) -> dict[str, str]:
    """Ask the question model to turn a provider question into search fields."""
    prompt = ANTHEM_PLAN_PROMPT.replace(
        "{{CONVERSATION}}", conversation or "(none)"
    ).replace("{{QUESTION}}", question)
    try:
        raw = chat.generate_text(
            prompt,
            model=ANTHEM_MODEL,
            temperature=0.0,
            num_predict=320,
            num_ctx=chat.ANSWER_NUM_CTX,
        )
    except RuntimeError:
        return _anthem_plan_fallback(question)
    parsed = _anthem_json_object(raw)
    if not parsed:
        return _anthem_plan_fallback(question)
    return _anthem_clean_plan(parsed, question)

def _fhir_bundle_resources(bundle) -> list[dict]:
    """Return resources from a FHIR Bundle or a single resource."""
    if not isinstance(bundle, dict):
        return []
    if bundle.get("resourceType") == "Bundle":
        items = []
        for entry in bundle.get("entry") or []:
            if isinstance(entry, dict):
                resource = entry.get("resource")
                if isinstance(resource, dict):
                    items.append(resource)
        return items
    if bundle.get("resourceType"):
        return [bundle]
    return []

def _fhir_human_name(resource: dict) -> str:
    """Render a FHIR HumanName, preferring the official name."""
    direct = resource.get("name")
    if isinstance(direct, str) and direct.strip():
        # Organization, Location, and HealthcareService use a plain string
        # name, while Practitioner uses a list of HumanName objects.
        return direct.strip()
    names = resource.get("name") or []
    if isinstance(names, dict):
        names = [names]
    names = [name for name in names if isinstance(name, dict)]
    names.sort(key=lambda name: 0 if name.get("use") == "official" else 1)
    for name in names:
        text = str(name.get("text") or "").strip()
        if text:
            return text
        parts: list[str] = []
        for key in ("prefix", "given"):
            value = name.get(key) or []
            if isinstance(value, str):
                value = [value]
            parts.extend(str(item).strip() for item in value if str(item).strip())
        family = str(name.get("family") or "").strip()
        if family:
            parts.append(family)
        value = name.get("suffix") or []
        if isinstance(value, str):
            value = [value]
        parts.extend(str(item).strip() for item in value if str(item).strip())
        label = " ".join(parts)
        if label:
            return label
    return ""

def _fhir_address(address) -> str:
    """Render a FHIR Address as one comma-separated line."""
    if not isinstance(address, dict):
        return ""
    parts: list[str] = []
    lines = address.get("line") or []
    if isinstance(lines, str):
        lines = [lines]
    parts.extend(str(line).strip() for line in lines if str(line).strip())
    for key in ("city", "state", "postalCode"):
        value = str(address.get(key) or "").strip()
        if value:
            parts.append(value)
    country = str(address.get("country") or "").strip()
    if country and country.upper() not in {"US", "USA"}:
        parts.append(country)
    return ", ".join(parts)

def _fhir_telecom(telecom) -> str:
    """Render phone/fax/email contact points."""
    if isinstance(telecom, dict):
        telecom = [telecom]
    values: list[str] = []
    for item in telecom or []:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        system = str(item.get("system") or "").strip().lower()
        if system == "phone":
            values.append(value)
        elif system in {"fax", "email", "url"}:
            values.append(f"{system}: {value}")
    return "; ".join(dict.fromkeys(values))

def _fhir_codeable(value) -> str:
    """Render one FHIR CodeableConcept."""
    if not isinstance(value, dict):
        return ""
    text = str(value.get("text") or "").strip()
    if text:
        return text
    for coding in value.get("coding") or []:
        if not isinstance(coding, dict):
            continue
        display = str(
            coding.get("display") or coding.get("code") or ""
        ).strip()
        if display:
            return display
    return ""

def _fhir_codeables(values) -> list[str]:
    """Render a list of CodeableConcepts without duplicates."""
    if isinstance(values, dict):
        values = [values]
    result: list[str] = []
    for value in values or []:
        label = _fhir_codeable(value)
        if label and label not in result:
            result.append(label)
    return result

def _fhir_npi(resource: dict) -> str:
    """Return the resource's US NPI, when present."""
    for identifier in resource.get("identifier") or []:
        if not isinstance(identifier, dict):
            continue
        system = str(identifier.get("system") or "").lower()
        if "us-npi" in system or "npi" in system:
            value = str(identifier.get("value") or "").strip()
            if value:
                return value
    return ""

def _fhir_reference_id(reference) -> str:
    """Return a stable ``ResourceType/id`` key from a FHIR reference."""
    if isinstance(reference, list):
        for item in reference:
            key = _fhir_reference_id(item)
            if key:
                return key
        return ""
    if isinstance(reference, dict):
        reference = reference.get("reference") or ""
    text = str(reference or "").strip()
    if not text:
        return ""
    parsed = urlparse(text)
    path = parsed.path if parsed.path else text
    parts = [part for part in path.rstrip("/").split("/") if part]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return parts[-1] if parts else ""

def _fhir_reference_display(reference) -> str:
    if isinstance(reference, list):
        for item in reference:
            label = _fhir_reference_display(item)
            if label:
                return label
        return ""
    if isinstance(reference, dict):
        return str(reference.get("display") or "").strip()
    return ""

def _anthem_newpatients(role: dict) -> str:
    """Read the Da Vinci Plan-Net new-patient extension."""
    for extension in role.get("extension") or []:
        if not isinstance(extension, dict):
            continue
        if not str(extension.get("url", "")).endswith("/newpatients"):
            continue
        for item in extension.get("extension") or []:
            if not isinstance(item, dict):
                continue
            concept = item.get("valueCodeableConcept") or {}
            for coding in concept.get("coding") or []:
                code = str(coding.get("code") or "").lower()
                if code == "newpt":
                    return "yes"
                if code == "existptonly":
                    return "no"
    return ""

def _anthem_network(role: dict) -> str:
    """Read a Plan-Net network reference."""
    for extension in role.get("extension") or []:
        if not isinstance(extension, dict):
            continue
        if "network-reference" not in str(extension.get("url", "")):
            continue
        reference = extension.get("valueReference") or {}
        label = str(reference.get("display") or "").strip()
        if label:
            return label
        return _fhir_reference_id(reference)
    networks = role.get("network") or []
    if isinstance(networks, dict):
        networks = [networks]
    labels = []
    for reference in networks:
        label = (
            _fhir_reference_display(reference)
            or _fhir_reference_id(reference)
        )
        if label:
            labels.append(label)
    return ", ".join(dict.fromkeys(labels))

def _anthem_fhir_request(
    base: str,
    resource: str,
    params,
    token: str,
) -> dict:
    """Fetch one FHIR resource path and return the decoded body."""
    url = f"{base.rstrip('/')}/{resource.lstrip('/')}"
    headers = {
        "Accept": "application/fhir+json, application/json",
        "User-Agent": "local-llm-chat-anthem/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = requests.get(
            url,
            params=params or [],
            headers=headers,
            timeout=(5, 30),
        )
    except requests.RequestException as exc:
        raise AnthemFHIRError(f"request failed: {exc}", url=url) from exc
    if response.status_code >= 400:
        detail = " ".join(response.text.split())[:240]
        raise AnthemFHIRError(
            f"HTTP {response.status_code}: {detail}",
            status=response.status_code,
            url=response.url,
        )
    try:
        return response.json()
    except ValueError as exc:
        raise AnthemFHIRError(
            "the FHIR server returned invalid JSON",
            status=response.status_code,
            url=response.url,
        ) from exc

def _anthem_resolve_references(
    base: str,
    token: str,
    resources: dict[tuple[str, str], dict],
    log: Callable[[str], None],
) -> None:
    """Fetch referenced Practitioner/Organization/Location resources."""
    wanted = {"Practitioner", "Organization", "Location", "HealthcareService"}
    fields = ("practitioner", "organization", "location", "healthcareService")
    for _ in range(2):
        missing: list[tuple[str, str]] = []
        for resource in list(resources.values()):
            for field in fields:
                references = resource.get(field) or []
                if isinstance(references, dict):
                    references = [references]
                for reference in references:
                    key = _fhir_reference_id(reference)
                    if "/" not in key:
                        continue
                    resource_type, resource_id = key.split("/", 1)
                    if resource_type not in wanted or not resource_id:
                        continue
                    if (resource_type, resource_id) in resources:
                        continue
                    if (resource_type, resource_id) not in missing:
                        missing.append((resource_type, resource_id))
        if not missing:
            return
        for resource_type, resource_id in missing[:40]:
            try:
                bundle = _anthem_fhir_request(
                    base,
                    f"{resource_type}/{resource_id}",
                    [],
                    token,
                )
            except AnthemFHIRError as exc:
                log(
                    "[anthem: could not read referenced "
                    f"{resource_type}/{resource_id}: {exc}]"
                )
                continue
            for item in _fhir_bundle_resources(bundle):
                key = (
                    str(item.get("resourceType") or ""),
                    str(item.get("id") or ""),
                )
                if key[0] and key[1]:
                    resources.setdefault(key, item)

def _anthem_fhir_search(
    base: str,
    token: str,
    plan: dict[str, str],
    log: Callable[[str], None],
) -> dict[tuple[str, str], dict]:
    """Search the FHIR directory for the providers described by ``plan``."""
    resources: dict[tuple[str, str], dict] = {}
    errors: list[str] = []
    role_includes = [
        ("_include", "PractitionerRole:practitioner"),
        ("_include", "PractitionerRole:organization"),
        ("_include", "PractitionerRole:location"),
        ("_include", "PractitionerRole:healthcare-service"),
    ]

    def add(resource) -> None:
        if not isinstance(resource, dict):
            return
        resource_type = str(resource.get("resourceType") or "").strip()
        if not resource_type:
            return
        resource_id = str(resource.get("id") or "").strip()
        key = (
            resource_type,
            resource_id or f"anonymous-{len(resources)}",
        )
        resources.setdefault(key, resource)

    def fetch(resource: str, params) -> list[dict]:
        try:
            bundle = _anthem_fhir_request(base, resource, params, token)
        except AnthemFHIRError as exc:
            errors.append(f"{resource}: {exc}")
            return []
        items = _fhir_bundle_resources(bundle)
        for item in items:
            add(item)
        return items

    doctor_name = plan.get("doctor_name", "")
    specialty = plan.get("specialty", "")
    facility = plan.get("facility", "")
    location = plan.get("location", "")
    practitioners: list[dict] = []
    organizations: list[dict] = []
    locations: list[dict] = []
    facility_org_ids: set[str] = set()
    facility_location_ids: set[str] = set()

    if doctor_name:
        practitioners = [
            item
            for item in fetch(
                "Practitioner",
                [("name", doctor_name), ("_count", str(ANTHEM_MAX_PROVIDERS))],
            )
            if item.get("resourceType") == "Practitioner"
        ]
        for practitioner in practitioners[:ANTHEM_MAX_PROVIDERS]:
            practitioner_id = str(practitioner.get("id") or "").strip()
            if practitioner_id:
                fetch(
                    "PractitionerRole",
                    [
                        (
                            "practitioner",
                            f"Practitioner/{practitioner_id}",
                        ),
                        ("_count", "50"),
                        *role_includes,
                    ],
                )

    if facility:
        organizations = [
            item
            for item in fetch(
                "Organization",
                [("name", facility), ("_count", "8")],
            )
            if item.get("resourceType") == "Organization"
        ]
        for organization in organizations[:8]:
            organization_id = str(organization.get("id") or "").strip()
            if organization_id:
                facility_org_ids.add(organization_id)
                fetch(
                    "PractitionerRole",
                    [
                        (
                            "organization",
                            f"Organization/{organization_id}",
                        ),
                        ("_count", "50"),
                        *role_includes,
                    ],
                )

    if location:
        locations = [
            item
            for item in fetch(
                "Location",
                [("address", location), ("_count", "10")],
            )
            if item.get("resourceType") == "Location"
        ]
        if not locations:
            city, state, postal = _anthem_parse_location(location)
            params = [("_count", "10")]
            if city:
                params.append(("address-city", city))
            if state:
                params.append(("address-state", state))
            if postal:
                params.append(("address-postalcode", postal))
            if len(params) > 1:
                locations = [
                    item
                    for item in fetch("Location", params)
                    if item.get("resourceType") == "Location"
                ]
        for location_resource in locations[:8]:
            location_id = str(
                location_resource.get("id") or ""
            ).strip()
            if location_id:
                if facility:
                    location_name = (
                        _fhir_human_name(location_resource)
                        or _fhir_reference_display(location_resource)
                    )
                    if _anthem_name_matches_facility(
                        location_name, facility
                    ):
                        facility_location_ids.add(location_id)
                fetch(
                    "PractitionerRole",
                    [
                        ("location", f"Location/{location_id}"),
                        ("_count", "50"),
                        *role_includes,
                    ],
                )

    if specialty:
        specialty_queries = []
        code = _ANTHEM_FHIR_SPECIALTY_CODES.get(specialty.casefold())
        if code:
            specialty_queries.append(
                f"http://nucc.org/provider-taxonomy|{code}"
            )
        specialty_queries.append(specialty)
        roles: list[dict] = []
        for query in specialty_queries:
            roles = fetch(
                "PractitionerRole",
                [
                    ("specialty", query),
                    ("_count", "50"),
                    *role_includes,
                ],
            )
            if roles:
                break
        if not roles:
            services = fetch(
                "HealthcareService",
                [("specialty", specialty), ("_count", "20")],
            )
            if not services:
                services = fetch(
                    "HealthcareService",
                    [("name", specialty), ("_count", "20")],
                )
            for service in services[:10]:
                service_id = str(service.get("id") or "").strip()
                if service_id:
                    fetch(
                        "PractitionerRole",
                        [
                            (
                                "service",
                                f"HealthcareService/{service_id}",
                            ),
                            ("_count", "50"),
                            *role_includes,
                        ],
                    )

    if not resources:
        if not any((doctor_name, specialty, facility, location)):
            fetch(
                "PractitionerRole",
                [("_count", "20"), *role_includes],
            )

    if not resources:
        detail = "; ".join(errors[-3:]) or "no matching providers"
        raise AnthemFHIRError(detail)
    if errors:
        log(
            "[anthem: some directory queries failed: "
            + "; ".join(errors[-3:])
            + "]"
        )
    _anthem_resolve_references(base, token, resources, log)
    if facility:
        # A specialty search can return roles from every organization in the
        # directory. Keep only roles whose linked organization or location is
        # the requested facility; otherwise a broad specialty query would
        # leak unrelated practices into a facility-constrained answer.
        kept: dict[tuple[str, str], dict] = {}
        kept_roles = 0
        for key, resource in resources.items():
            if key[0] != "PractitionerRole":
                continue
            org_id = _fhir_reference_id(
                resource.get("organization")
            ).split("/", 1)[-1]
            loc_id = _fhir_reference_id(
                resource.get("location")
            ).split("/", 1)[-1]
            org_resource = resources.get(("Organization", org_id), {})
            loc_resource = resources.get(("Location", loc_id), {})
            org_name = (
                _fhir_human_name(org_resource)
                or _fhir_reference_display(resource.get("organization"))
            )
            loc_name = (
                _fhir_human_name(loc_resource)
                or _fhir_reference_display(resource.get("location"))
            )
            loc_address = _fhir_address(loc_resource.get("address"))
            if not (
                org_id in facility_org_ids
                or loc_id in facility_location_ids
                or _anthem_name_matches_facility(org_name, facility)
                or _anthem_name_matches_facility(loc_name, facility)
                or _anthem_name_matches_facility(loc_address, facility)
            ):
                continue
            kept[key] = resource
            kept_roles += 1
            for resource_type, reference in (
                ("Practitioner", resource.get("practitioner")),
                ("Organization", resource.get("organization")),
                ("Location", resource.get("location")),
                (
                    "HealthcareService",
                    resource.get("healthcareService"),
                ),
            ):
                reference_id = _fhir_reference_id(reference).split(
                    "/", 1
                )[-1]
                if not reference_id:
                    continue
                reference_key = (resource_type, reference_id)
                if reference_key in resources:
                    kept.setdefault(reference_key, resources[reference_key])
        if kept_roles:
            resources = kept
        else:
            log(
                "[anthem: FHIR returned no PractitionerRole linked to "
                f"{facility!r}; trying the NPPES fallback]"
            )
            resources = {}
    return resources

def _anthem_format_fhir(
    resources: dict[tuple[str, str], dict],
    plan: dict[str, str],
) -> str:
    """Turn FHIR resources into a compact provider-directory context."""
    by_type: dict[str, list[dict]] = {}
    for resource in resources.values():
        resource_type = str(resource.get("resourceType") or "")
        by_type.setdefault(resource_type, []).append(resource)

    practitioners = {
        str(item.get("id")): item
        for item in by_type.get("Practitioner", [])
        if item.get("id")
    }
    organizations = {
        str(item.get("id")): item
        for item in by_type.get("Organization", [])
        if item.get("id")
    }
    locations = {
        str(item.get("id")): item
        for item in by_type.get("Location", [])
        if item.get("id")
    }
    roles = by_type.get("PractitionerRole", [])
    role_practitioner_ids: set[str] = set()
    blocks: list[str] = []

    def add_block(lines: list[str]) -> None:
        text = "\n".join(line for line in lines if line)
        if text and text not in blocks:
            blocks.append(text)

    for role in roles:
        practitioner_ref = _fhir_reference_id(role.get("practitioner"))
        practitioner_id = practitioner_ref.split("/", 1)[-1]
        practitioner = practitioners.get(practitioner_id, {})
        if practitioner_id:
            role_practitioner_ids.add(practitioner_id)
        organization_ref = _fhir_reference_id(role.get("organization"))
        organization = organizations.get(
            organization_ref.split("/", 1)[-1], {}
        )
        location_ref = _fhir_reference_id(role.get("location"))
        location = locations.get(location_ref.split("/", 1)[-1], {})

        name = (
            _fhir_human_name(practitioner)
            or _fhir_reference_display(role.get("practitioner"))
            or "Name not listed"
        )
        specialties = _fhir_codeables(role.get("specialty"))
        if not specialties:
            specialties = _fhir_codeables(
                practitioner.get("qualification")
            )
        organization_name = (
            _fhir_human_name(organization)
            or _fhir_reference_display(role.get("organization"))
        )
        location_name = (
            _fhir_human_name(location)
            or _fhir_reference_display(role.get("location"))
        )
        address = _fhir_address(location.get("address"))
        phone = (
            _fhir_telecom(role.get("telecom"))
            or _fhir_telecom(location.get("telecom"))
            or _fhir_telecom(practitioner.get("telecom"))
        )
        npi = _fhir_npi(practitioner)
        newpatients = _anthem_newpatients(role)
        network = _anthem_network(role)
        lines = [f"Provider: {name}"]
        if npi:
            lines.append(f"NPI: {npi}")
        if specialties:
            lines.append("Specialty: " + "; ".join(specialties))
        if organization_name:
            lines.append(f"Organization: {organization_name}")
        if location_name:
            lines.append(f"Location: {location_name}")
        if address:
            lines.append(f"Address: {address}")
        if phone:
            lines.append(f"Phone/contact: {phone}")
        if network:
            lines.append(f"Network: {network}")
        if newpatients:
            lines.append(f"Accepting new patients: {newpatients}")
        add_block(lines)

    for practitioner_id, practitioner in practitioners.items():
        if practitioner_id in role_practitioner_ids:
            continue
        name = _fhir_human_name(practitioner) or "Name not listed"
        lines = [f"Provider: {name}"]
        npi = _fhir_npi(practitioner)
        if npi:
            lines.append(f"NPI: {npi}")
        specialties = _fhir_codeables(
            [
                qualification.get("code")
                for qualification in practitioner.get("qualification")
                or []
                if isinstance(qualification, dict)
            ]
        )
        if specialties:
            lines.append("Specialty: " + "; ".join(specialties))
        phone = _fhir_telecom(practitioner.get("telecom"))
        if phone:
            lines.append(f"Phone/contact: {phone}")
        lines.append("No practice location was returned for this provider.")
        add_block(lines)

    if not blocks:
        for resource_type, items in by_type.items():
            for item in items:
                name = _fhir_human_name(item) or str(item.get("id") or "")
                lines = [f"{resource_type}: {name}"]
                address = _fhir_address(item.get("address"))
                if address:
                    lines.append(f"Address: {address}")
                phone = _fhir_telecom(item.get("telecom"))
                if phone:
                    lines.append(f"Phone/contact: {phone}")
                add_block(lines)

    text = "\n\n".join(blocks)
    return text[:ANTHEM_CONTEXT_CHARS]

def _anthem_format_nppes(
    results: list[dict],
    preferred_city: str = "",
    preferred_state: str = "",
    preferred_postal: str = "",
    preferred_addresses: set[tuple[str, str]] | None = None,
) -> str:
    """Turn NPPES API results into a compact provider context."""
    blocks: list[str] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        basic = result.get("basic") or {}
        if result.get("enumeration_type") == "NPI-2":
            name = str(basic.get("organization_name") or "").strip()
        else:
            name = " ".join(
                part
                for part in (
                    str(basic.get(key) or "").strip()
                    for key in ("first_name", "middle_name", "last_name")
                )
                if part
            ).strip()
        if not name:
            name = "Name not listed"
        credential = str(basic.get("credential") or "").strip()
        if credential:
            name = f"{name}, {credential}"
        lines = [f"Provider: {name}"]
        npi = str(result.get("number") or "").strip()
        if npi:
            lines.append(f"NPI: {npi}")
        gender = str(basic.get("gender") or "").strip()
        if gender:
            lines.append(f"Gender: {gender}")
        status = str(basic.get("status") or "").strip()
        if status:
            lines.append(f"Status: {status}")

        specialties: list[str] = []
        for taxonomy in result.get("taxonomies") or []:
            if not isinstance(taxonomy, dict):
                continue
            desc = str(taxonomy.get("desc") or "").strip()
            if not desc:
                continue
            if taxonomy.get("primary"):
                desc += " (primary)"
            license_number = str(taxonomy.get("license") or "").strip()
            state = str(taxonomy.get("state") or "").strip()
            if license_number:
                suffix = f" license {license_number}"
                if state:
                    suffix += f" ({state})"
                desc += suffix
            if desc not in specialties:
                specialties.append(desc)
        if specialties:
            lines.append("Specialty: " + "; ".join(specialties))

        addresses = [
            item
            for item in result.get("addresses") or []
            if isinstance(item, dict)
        ]

        def address_rank(item: dict) -> tuple[int, int]:
            city_value = str(item.get("city") or "").strip().casefold()
            state_value = str(item.get("state") or "").strip().upper()
            postal_value = str(item.get("postal_code") or "").strip()
            location_rank = 0
            if preferred_state and state_value == preferred_state.upper():
                location_rank -= 2
            if preferred_city and city_value == preferred_city.casefold():
                location_rank -= 4
            if preferred_postal and postal_value.startswith(
                preferred_postal[:5]
            ):
                location_rank -= 8
            if preferred_addresses and (
                _anthem_normalize_street(item.get("address_1")),
                postal_value[:5],
            ) in preferred_addresses:
                location_rank -= 16
            purpose_rank = (
                0 if item.get("address_purpose") == "LOCATION" else 1
            )
            return (location_rank, purpose_rank)

        addresses.sort(key=address_rank)
        if addresses:
            address = addresses[0]
            street = ", ".join(
                part
                for part in (
                    str(address.get("address_1") or "").strip(),
                    str(address.get("address_2") or "").strip(),
                )
                if part
            )
            locality = " ".join(
                part
                for part in (
                    str(address.get("state") or "").strip(),
                    str(address.get("postal_code") or "").strip(),
                )
                if part
            )
            label = ", ".join(
                part
                for part in (
                    street,
                    str(address.get("city") or "").strip(),
                    locality,
                )
                if part
            )
            if label:
                lines.append(f"Address: {label}")
            phone = str(address.get("telephone_number") or "").strip()
            if phone:
                lines.append(f"Phone: {phone}")
            fax = str(address.get("fax_number") or "").strip()
            if fax:
                lines.append(f"Fax: {fax}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)[:ANTHEM_CONTEXT_CHARS]

def _anthem_nppes_request(
    params: list[tuple[str, str]],
    log: Callable[[str], None],
    paginate: bool = False,
) -> list[dict]:
    """Run an NPPES API request, optionally paging through all results."""
    base_params = list(params)
    try:
        page_limit = int(dict(base_params).get("limit", "0"))
    except (TypeError, ValueError):
        page_limit = 0
    pages = 5 if paginate and page_limit >= 200 else 1
    results: list[dict] = []
    for page in range(pages):
        page_params = list(base_params)
        if page:
            page_params.append(("skip", str(page * page_limit)))
        try:
            response = requests.get(
                ANTHEM_NPPES_URL,
                params=page_params,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "local-llm-chat-anthem/1.0",
                },
                timeout=(5, 30),
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            log(f"[anthem: NPPES fallback failed: {exc}]")
            break
        batch = data.get("results") or []
        if not isinstance(batch, list):
            break
        batch = [item for item in batch if isinstance(item, dict)]
        results.extend(batch)
        if len(batch) < page_limit:
            break
    return results

_ANTHEM_GENERIC_KEYWORDS = {
    "phone",
    "number",
    "address",
    "location",
    "insurance",
    "network",
    "accepting",
    "patient",
    "patients",
    "doctor",
    "doctors",
    "physician",
    "physicians",
    "provider",
    "providers",
    "list",
    "find",
    "show",
    "me",
    "the",
    "a",
    "an",
    "in",
    "at",
    "near",
    "for",
    "with",
    "and",
    "or",
    "of",
    "to",
    "is",
    "are",
    "who",
    "what",
    "where",
    "how",
    "good",
    "best",
    "new",
    "current",
    "please",
    "can",
    "you",
    "give",
    "tell",
    "about",
    "contact",
    "office",
    "clinic",
    "hospital",
    "medical",
    "center",
    "department",
    "division",
    "specialist",
    "specialists",
    "practice",
    "practices",
    "care",
}

def _anthem_nppes_taxonomy_candidates(
    specialty: str,
    keywords: str,
) -> list[str]:
    """Return NPPES taxonomy search terms, most specific first."""
    candidates: list[str] = []

    def add(value: str) -> None:
        value = " ".join(str(value or "").split())
        if not value:
            return
        if value.casefold() in {
            candidate.casefold() for candidate in candidates
        }:
            return
        candidates.append(value)

    mapped = _NPPES_TAXONOMY_ALIASES.get(
        specialty.casefold(), specialty
    )
    qualifier_words = [
        word
        for word in re.split(r"[^A-Za-z]+", str(keywords or ""))
        if word and word.casefold() not in _ANTHEM_GENERIC_KEYWORDS
    ]
    mapped_key = mapped.casefold() if mapped else ""
    if "surgery" in mapped_key and qualifier_words:
        qualifier_set = {word.casefold() for word in qualifier_words}
        if qualifier_set & {
            "neck",
            "head",
            "throat",
            "thyroid",
            "voice",
            "tonsil",
            "sinus",
        }:
            add("Otolaryngology")
        if qualifier_set & {
            "lung",
            "thoracic",
            "heart",
            "cardiac",
            "esophageal",
            "esophagus",
        }:
            add("Thoracic Surgery")
        if qualifier_set & {
            "cancer",
            "tumor",
            "tumour",
            "oncology",
            "breast",
            "melanoma",
        }:
            add("Surgical Oncology")
    if mapped and qualifier_words:
        # "pediatric" plus "Otolaryngology" is more specific than the generic
        # taxonomy and matches "Otolaryngology, Pediatric Otolaryngology".
        add(" ".join(qualifier_words + [mapped]))
    add(mapped)
    add(specialty)
    if not specialty and qualifier_words:
        add(" ".join(qualifier_words))
    return candidates

def _anthem_nppes_facility_location(
    facility: str,
    log: Callable[[str], None],
) -> tuple[str, str, str, set[tuple[str, str]]]:
    """Find a named facility's location and its NPPES address anchors.

    Organization records give us the addresses that actually belong to the
    facility. Those anchors let a practitioner search reject providers who
    happen to practise in the same city but not at the facility.
    """
    facility = " ".join(str(facility or "").split())
    names = [facility]
    names.extend(
        alias
        for alias in sorted(
            _anthem_facility_aliases(facility),
            key=len,
            reverse=True,
        )
        if alias and alias.casefold() != facility.casefold()
    )
    simplified = re.sub(
        r"\b(?:medical|center|centre|hospital|clinic|health|healthcare|"
        r"system|university|institute|group|inc|llc)\b",
        " ",
        facility,
        flags=re.IGNORECASE,
    )
    simplified = " ".join(simplified.split())
    if simplified and simplified.casefold() != facility.casefold():
        names.append(simplified)
    anchors: set[tuple[str, str]] = set()
    primary_city = ""
    primary_state = ""
    primary_postal = ""
    for name in dict.fromkeys(names):
        if not name:
            continue
        results = _anthem_nppes_request(
            [
                ("version", "2.1"),
                ("limit", "50"),
                ("organization_name", name),
            ],
            log,
        )
        for result in results:
            addresses = [
                address
                for address in result.get("addresses") or []
                if isinstance(address, dict)
            ]
            addresses.sort(
                key=lambda address: 0
                if address.get("address_purpose") == "LOCATION"
                else 1
            )
            anchors.update(_anthem_address_keys(addresses))
            if primary_city and primary_state:
                continue
            for address in addresses:
                city = str(address.get("city") or "").strip()
                state = str(address.get("state") or "").strip().upper()
                postal = str(address.get("postal_code") or "").strip()
                if city and state:
                    primary_city, primary_state = city, state
                    primary_postal = postal
                    break
    if anchors:
        log(
            "[anthem: NPPES mapped facility "
            f"{facility!r} to {len(anchors)} address(es)]"
        )
    if primary_city and primary_state:
        log(
            "[anthem: NPPES located facility "
            f"{facility!r} in {primary_city}, {primary_state}]"
        )
    return primary_city, primary_state, primary_postal, anchors

def _anthem_nppes_facility_filter(
    results: list[dict],
    facility: str,
    anchors: set[tuple[str, str]],
    limit: int,
    log: Callable[[str], None],
    on_batch: Callable[[list[dict]], None] | None = None,
    batch_size: int = 10,
) -> list[dict]:
    """Keep only NPPES providers confirmed to be at the requested facility.

    Address anchors come from the facility's own NPPES organization records.
    When there are not enough anchor matches, UCSF/Stanford profile pages are
    used as a second source of affiliation evidence. This prevents a city-wide
    specialty search from returning a nearby private practice as if it were
    part of the hospital.

    When ``on_batch`` is supplied, confirmed individual providers are emitted
    in batches as soon as they are verified, so the browser can render the
    first page without waiting for every affiliation check.
    """
    if not results:
        return []
    anchor_flags = [
        bool(_anthem_address_keys(result.get("addresses")) & anchors)
        for result in results
    ]
    anchor_hits = sum(anchor_flags)

    stream_order: list[int] = []
    streamed: set[int] = set()
    stream_batch: list[dict] = []

    def emit(index: int) -> None:
        if on_batch is None or len(stream_order) >= limit:
            return
        if index in streamed:
            return
        if results[index].get("enumeration_type") != "NPI-1":
            return
        streamed.add(index)
        stream_order.append(index)
        stream_batch.append(results[index])
        if len(stream_batch) >= batch_size:
            on_batch(list(stream_batch))
            stream_batch.clear()

    def flush() -> None:
        if on_batch is not None and stream_batch:
            on_batch(list(stream_batch))
            stream_batch.clear()

    if anchor_hits >= limit:
        if on_batch is not None:
            for index, hit in enumerate(anchor_flags):
                if hit:
                    emit(index)
            flush()
            if stream_order:
                return [results[i] for i in stream_order][:limit]
        selected = [
            result
            for result, hit in zip(results, anchor_flags)
            if hit
        ]
        individuals = [
            result
            for result in selected
            if result.get("enumeration_type") == "NPI-1"
        ]
        return (individuals or selected)[:limit]

    supported = _anthem_facility_profile_supported(facility)
    if not anchors and not supported:
        log(
            "[anthem: no facility address anchors were found; keeping "
            "location matches without facility verification]"
        )
        if on_batch is not None:
            for index in range(len(results)):
                emit(index)
            flush()
            if stream_order:
                return [results[i] for i in stream_order][:limit]
        return results

    affiliated = list(anchor_flags)
    if on_batch is not None:
        for index, hit in enumerate(anchor_flags):
            if hit:
                emit(index)
    if supported:
        needed = max(1, limit - anchor_hits)
        pending = [
            index
            for index, hit in enumerate(anchor_flags)
            if not hit
            and results[index].get("enumeration_type") == "NPI-1"
        ][: max(12, needed * 3)]

        def check(index: int) -> tuple[int, bool]:
            record = results[index]
            name = _anthem_provider_record_name(record)
            npi = str(record.get("number") or "").strip()
            return index, _anthem_provider_affiliation(
                name, facility, npi=npi
            )

        if pending:
            # Affiliation checks are network-bound but independent. Use a
            # wider pool so a UCSF/Stanford facility filter does not add
            # tens of seconds before the provider list can be streamed.
            with ThreadPoolExecutor(max_workers=32) as pool:
                futures = {
                    pool.submit(check, index): index
                    for index in pending
                }
                for future in as_completed(futures):
                    try:
                        index, confirmed = future.result()
                    except Exception:
                        continue
                    if confirmed:
                        affiliated[index] = True
                        if on_batch is not None:
                            emit(index)
    flush()
    if on_batch is not None and stream_order:
        selected = [results[i] for i in stream_order]
        if anchors:
            log(
                f"[anthem: confirmed {len(selected)} provider(s) at "
                f"{facility!r}]"
            )
        return selected[:limit]

    selected = [
        result
        for result, hit in zip(results, affiliated)
        if hit
    ]
    if not selected:
        return []
    individuals = [
        result
        for result in selected
        if result.get("enumeration_type") == "NPI-1"
    ]
    selected = individuals or selected
    if anchors:
        log(
            f"[anthem: confirmed {len(selected)} provider(s) at "
            f"{facility!r}]"
        )
    return selected[:limit]

def _anthem_nppes_search(
    plan: dict[str, str],
    log: Callable[[str], None],
    on_batch: Callable[[str], None] | None = None,
    condition_tokens: list[str] | None = None,
    modalities: list[tuple[str, ...]] | None = None,
    max_results: int = 0,
) -> tuple[str, int]:
    """Search CMS NPPES for the provider list.

    When ``on_batch`` is supplied, formatted result batches are emitted as
    soon as they are verified so the browser can render the first page before
    the full search and facility verification finish.
    """
    first_name, last_name = _anthem_split_name(
        plan.get("doctor_name", "")
    )
    facility = plan.get("facility", "")
    specialty = plan.get("specialty", "")
    keywords = plan.get("keywords", "")
    city, state, postal = _anthem_parse_location(plan.get("location", ""))
    facility_only = bool(
        facility and not (first_name or last_name or specialty)
    )
    facility_anchors: set[tuple[str, str]] = set()
    if facility and not facility_only:
        # NPPES organization names are not reliable in a practitioner query.
        # Resolve the facility to a city/state and to the addresses that
        # belong to its organization records, then search providers around
        # that location and keep only providers at the facility.
        (
            inferred_city,
            inferred_state,
            _inferred_postal,
            inferred_anchors,
        ) = _anthem_nppes_facility_location(facility, log)
        facility_anchors = inferred_anchors
        if not (city or state or postal) and (
            inferred_city or inferred_state
        ):
            # Use the facility's city/state to search nearby providers, but do
            # not keep its ZIP: providers at the same hospital often list a
            # different practice ZIP code.
            city, state, postal = inferred_city, inferred_state, ""
    taxonomies = _anthem_nppes_taxonomy_candidates(
        specialty, keywords
    )
    result_limit = (
        ANTHEM_MAX_FACILITY_PROVIDERS if facility
        else ANTHEM_MAX_PROVIDERS
    )
    if max_results > 0:
        result_limit = min(result_limit, max_results)
    paginate = bool(
        facility and taxonomies and not (first_name or last_name)
    )

    def build(
        taxonomy: str = "",
        include_location: bool = True,
        include_facility: bool = False,
    ) -> list[tuple[str, str]]:
        broad = bool(
            taxonomy and not (first_name or last_name or facility_only)
        )
        params: list[tuple[str, str]] = [
            ("version", "2.1"),
            (
                "limit",
                str(200 if broad else ANTHEM_MAX_PROVIDERS),
            ),
        ]
        if first_name:
            # NPPES matches the first-name field literally; a middle initial
            # such as "Maria R." would return no rows, so search the first
            # token.
            params.append(("first_name", first_name.split()[0]))
        if last_name:
            params.append(("last_name", last_name))
        if include_facility and facility:
            params.append(("organization_name", facility))
        if include_location:
            if city:
                params.append(("city", city))
            if state:
                params.append(("state", state))
            if postal:
                params.append(("postal_code", postal))
        if taxonomy:
            params.append(("taxonomy_description", taxonomy))
        return params

    attempts: list[list[tuple[str, str]]] = []

    def add_attempt(params: list[tuple[str, str]]) -> None:
        search_terms = [
            name
            for name, _value in params
            if name not in {"version", "limit"}
        ]
        if not search_terms:
            return
        if params not in attempts:
            attempts.append(params)

    if facility_only:
        add_attempt(build(include_facility=True, include_location=True))
        add_attempt(build(include_facility=True, include_location=False))
    for taxonomy in taxonomies:
        add_attempt(build(taxonomy, include_location=True))
    if taxonomies:
        # Last resort: keep the name/location filters and filter the taxonomy
        # client-side, then try the specialty without a location constraint.
        add_attempt(build("", include_location=True))
        add_attempt(build(taxonomies[0], include_location=False))
    else:
        add_attempt(build("", include_location=True))
    if (first_name or last_name) and (city or state or postal or facility):
        # A named doctor can be registered under an older practice address.
        # Do not lose the doctor just because the requested facility/location
        # constraint returns no NPPES rows.
        add_attempt(build("", include_location=False))

    results: list[dict] = []
    for params in attempts:
        results = _anthem_nppes_request(
            params, log, paginate=paginate
        )
        if results:
            used_taxonomy = dict(params).get("taxonomy_description", "")
            if used_taxonomy:
                log(
                    "[anthem: NPPES matched taxonomy "
                    f"{used_taxonomy!r}]"
                )
            break
    if not results:
        return "", 0

    if specialty:
        tokens = [
            token
            for token in re.split(
                r"[^a-z0-9]+",
                " ".join([specialty, *taxonomies]).casefold(),
            )
            if len(token) > 2
            and token not in _ANTHEM_GENERIC_KEYWORDS
        ]

        def matches_specialty(result: dict) -> bool:
            text = " ".join(
                str(taxonomy.get("desc") or "")
                for taxonomy in result.get("taxonomies") or []
                if isinstance(taxonomy, dict)
            ).casefold()
            return any(token in text for token in tokens)

        filtered = [
            result for result in results if matches_specialty(result)
        ]
        if filtered:
            results = filtered
        else:
            log(
                "[anthem: NPPES taxonomy did not match the requested "
                "specialty; showing the name/location matches instead]"
            )

    if city or state or postal:
        def matches_location(result: dict) -> bool:
            for address in result.get("addresses") or []:
                if not isinstance(address, dict):
                    continue
                state_value = str(address.get("state") or "").upper()
                city_value = (
                    str(address.get("city") or "").strip().casefold()
                )
                postal_value = str(
                    address.get("postal_code") or ""
                ).strip()
                if state and state_value != state.upper():
                    continue
                if city and city_value != city.casefold():
                    continue
                if postal and not postal_value.startswith(postal[:5]):
                    continue
                return True
            return False

        located = [
            result for result in results if matches_location(result)
        ]
        if located:
            results = located
        else:
            log(
                "[anthem: NPPES returned records, but none listed an "
                "address matching the requested location]"
            )

    if condition_tokens and modalities:
        def result_matches_condition(result: dict) -> bool:
            text = " ".join(
                str(taxonomy.get("desc") or "")
                for taxonomy in result.get("taxonomies") or []
                if isinstance(taxonomy, dict)
            )
            return _anthem_provider_matches_condition(
                {"specialty": text, "section": text},
                condition_tokens,
                modalities,
            )

        relevant = [
            result
            for result in results
            if result_matches_condition(result)
        ]
        if relevant:
            results = relevant
            log(
                "[anthem: NPPES kept "
                f"{len(results)} provider(s) matching the condition "
                "and requested treatment]"
            )
        else:
            log(
                "[anthem: NPPES had no provider matching the condition "
                "and requested treatment; keeping the wider list]"
            )

    if facility and not facility_only:
        def stream_facility_batch(batch: list[dict]) -> None:
            if on_batch is None or not batch:
                return
            batch_context = _anthem_format_nppes(
                batch, city, state, postal, facility_anchors
            )
            if batch_context:
                on_batch(batch_context)

        pre_facility_results = list(results)
        results = _anthem_nppes_facility_filter(
            results,
            facility,
            facility_anchors,
            result_limit,
            log,
            on_batch=stream_facility_batch if on_batch else None,
        )
        if not results and (first_name or last_name):
            results = pre_facility_results
            log(
                "[anthem: could not confirm the facility affiliation; "
                "returning the named doctor's NPPES record]"
            )
        if not results:
            log(
                "[anthem: no NPPES record confirmed an affiliation with "
                f"{facility!r}; refusing to return unrelated providers]"
            )
            return "", 0

    if first_name or last_name or specialty:
        # A provider-name or specialty answer should lead with individual
        # practitioners; organizations are kept only when no person remains.
        sort_key = lambda result: 0 if result.get("enumeration_type") == "NPI-1" else 1
        if not (on_batch is not None and facility):
            results.sort(key=sort_key)
    results = results[:result_limit]
    if not results:
        return "", 0
    if on_batch is not None and not facility:
        for start in range(0, len(results), 10):
            batch_context = _anthem_format_nppes(
                results[start : start + 10],
                city,
                state,
                postal,
                facility_anchors,
            )
            if batch_context:
                on_batch(batch_context)
    log(
        f"[anthem: NPPES fallback returned {len(results)} record(s)]"
    )
    return (
        _anthem_format_nppes(
            results, city, state, postal, facility_anchors
        ),
        len(results),
    )

def _anthem_provider_record_name(
    record: dict,
    fallback: str = "",
) -> str:
    """Return a display name from one NPPES result."""
    basic = record.get("basic") or {}
    if record.get("enumeration_type") == "NPI-2":
        name = str(basic.get("organization_name") or "").strip()
    else:
        name = " ".join(
            part
            for part in (
                str(basic.get(key) or "").strip()
                for key in ("first_name", "middle_name", "last_name")
            )
            if part
        ).strip()
    credential = str(basic.get("credential") or "").strip()
    if credential:
        name = f"{name}, {credential}" if name else credential
    return name or " ".join(str(fallback or "").split())

def _anthem_nppes_record_for_provider(
    npi: str,
    name: str,
) -> dict | None:
    """Look up one provider in NPPES by NPI, then by name."""
    npi = str(npi or "").strip()
    name = " ".join(str(name or "").split())
    silent = lambda _text: None
    if npi:
        results = _anthem_nppes_request(
            [
                ("version", "2.1"),
                ("number", npi),
                ("limit", "1"),
            ],
            silent,
        )
        for result in results:
            if str(result.get("number") or "") == npi:
                return result
        if results:
            return results[0]
    first_name, last_name = _anthem_split_name(name)
    if not (first_name or last_name):
        return None
    params: list[tuple[str, str]] = [
        ("version", "2.1"),
        ("limit", "5"),
    ]
    if first_name:
        params.append(("first_name", first_name.split()[0]))
    if last_name:
        params.append(("last_name", last_name))
    results = _anthem_nppes_request(params, silent)
    return results[0] if results else None

def _anthem_pubmed_get(resource: str, params: dict[str, str]) -> dict:
    """Fetch one PubMed E-utilities JSON response."""
    try:
        response = requests.get(
            f"{_PUBMED_BASE}{resource}",
            params=params,
            headers={
                "Accept": "application/json",
                "User-Agent": "local-llm-chat-anthem/1.0",
            },
            timeout=(5, 20),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

_PUBMED_SPECIALTY_TERMS = {
    "otolaryng": [
        "otolaryngology",
        "laryngology",
        "otology",
        "rhinology",
        "airway",
        "tonsil",
        "sinus",
        "neck",
    ],
    "head and neck": [
        "head and neck",
        "otolaryngology",
        "laryngology",
        "airway",
        "neck",
    ],
    "cardio": ["cardiology", "cardiovascular", "cardiac", "heart"],
    "pulmon": ["pulmonology", "pulmonary", "lung", "respiratory"],
    "oncol": ["oncology", "cancer", "tumor", "carcinoma", "neoplasm"],
    "neurol": ["neurology", "neurologic", "brain", "stroke", "epilepsy"],
    "dermat": ["dermatology", "skin"],
    "pediatr": ["pediatric", "paediatric", "children", "child"],
    "surg": ["surgery", "surgical", "operative"],
    "urol": ["urology", "urologic", "prostate", "bladder", "kidney"],
    "orthop": ["orthopedic", "orthopaedic", "bone", "joint", "spine"],
    "gastro": [
        "gastroenterology",
        "gastrointestinal",
        "liver",
        "colon",
    ],
    "endocr": ["endocrinology", "diabetes", "thyroid", "hormone"],
    "psych": ["psychiatry", "psychiatric", "mental", "depression"],
    "ophthalm": ["ophthalmology", "eye", "retina", "glaucoma"],
    "obstet": ["obstetrics", "gynecology", "pregnancy", "women"],
    "gyne": ["gynecology", "obstetrics", "pregnancy", "women"],
    "anesthes": ["anesthesiology", "anesthesia"],
    "radiol": ["radiology", "imaging", "radiologic"],
    "pathol": ["pathology", "pathologic"],
    "family medicine": ["family medicine", "primary care"],
    "internal medicine": ["internal medicine"],
    "infect": ["infectious", "infection", "virology", "microbiology"],
    "immun": ["immunology", "immune"],
    "rheum": ["rheumatology", "arthritis", "autoimmune"],
    "nephro": ["nephrology", "kidney", "renal"],
    "hemat": ["hematology", "blood", "leukemia", "lymphoma"],
    "emergency": ["emergency medicine", "emergency"],
}

def _anthem_pubmed_topic_terms(specialty: str) -> list[str]:
    """Return PubMed title/abstract terms implied by a specialty."""
    text = " ".join(str(specialty or "").casefold().split())
    terms: set[str] = set()
    for key, values in _PUBMED_SPECIALTY_TERMS.items():
        if key in text:
            terms.update(values)
    generic = {
        "internal",
        "medicine",
        "primary",
        "license",
        "medical",
        "health",
        "care",
        "center",
        "clinic",
        "department",
        "division",
        "specialist",
        "specialists",
        "practice",
        "practices",
        "student",
        "program",
        "training",
        "certified",
    }
    for word in re.findall(r"[a-z]{4,}", text):
        if word not in generic:
            terms.add(word)
    return sorted(terms, key=lambda term: (len(term), term))[:10]

def _anthem_pubmed_publications(
    name: str,
    specialty: str = "",
    institution: str = "",
    limit: int = 8,
) -> list[dict]:
    """Return PubMed publications that match the provider's identity.

    Author-only PubMed searches are highly ambiguous: many people share the
    same last name and initials. The query therefore combines the author with
    specialty-derived title/abstract terms, or with the institution when no
    specialty is known. Results are then checked against the returned author
    list so a same-name publication is not attributed to this provider.
    """
    first_name, last_name = _anthem_split_name(name)
    if not last_name:
        return []
    initials = "".join(
        part[0]
        for part in re.findall(r"[A-Za-z]+", first_name)
        if part
    ).upper()
    author = f"{last_name} {initials}".strip()
    topic_terms = _anthem_pubmed_topic_terms(specialty)
    queries: list[str] = []
    if topic_terms:
        topic_query = " OR ".join(
            f"{term}[Title/Abstract]" for term in topic_terms
        )
        queries.append(f"{author}[Author] AND ({topic_query})")
    if institution:
        queries.append(f"{author}[Author] AND {institution}[Affiliation]")
    if not queries:
        queries.append(f"{author}[Author]")

    ids: list[str] = []
    for term in queries:
        data = _anthem_pubmed_get(
            "esearch.fcgi",
            {
                "db": "pubmed",
                "term": term,
                "retmode": "json",
                "retmax": str(limit),
                "sort": "date",
            },
        )
        ids = list(
            (data.get("esearchresult") or {}).get("idlist") or []
        )
        if ids:
            break
    if not ids:
        return []
    summary = _anthem_pubmed_get(
        "esummary.fcgi",
        {
            "db": "pubmed",
            "id": ",".join(ids),
            "retmode": "json",
        },
    )
    result = summary.get("result") or {}
    target = f"{last_name} {initials}".casefold().strip()

    def author_matches(item: dict) -> bool:
        if not target:
            return False
        for author_entry in item.get("authors") or []:
            if not isinstance(author_entry, dict):
                continue
            name_value = str(
                author_entry.get("name") or ""
            ).casefold().strip()
            if not name_value:
                continue
            if name_value == target or name_value.startswith(
                target + " "
            ):
                return True
        return False

    publications: list[dict] = []
    for pmid in ids:
        item = result.get(pmid) or {}
        if not isinstance(item, dict) or not author_matches(item):
            continue
        title = re.sub(
            r"<[^>]+>", "", str(item.get("title") or "")
        )
        title = " ".join(html.unescape(title).split())
        if not title:
            continue
        publications.append(
            {
                "pmid": pmid,
                "title": title,
                "journal": str(item.get("source") or "").strip(),
                "date": str(item.get("pubdate") or "").strip(),
                "url": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
            }
        )
    return publications[:limit]

def _anthem_provider_profile_urls(
    name: str,
    institution: str,
) -> list[str]:
    """Return predictable institution profile URLs for a provider."""
    first_name, last_name = _anthem_split_name(name)
    if not last_name:
        return []

    def slug(value: str) -> str:
        return re.sub(
            r"[^a-z0-9]+", "-", str(value or "").casefold()
        ).strip("-")

    first_slug_variants: list[str] = []

    def add_given(value: str) -> None:
        value_slug = slug(value)
        if value_slug and value_slug not in first_slug_variants:
            first_slug_variants.append(value_slug)

    if first_name:
        add_given(first_name.split()[0])
        add_given(first_name)
        add_given("".join(re.findall(r"[A-Za-z0-9]+", first_name)))
    last_slug = slug(last_name)
    if not last_slug:
        return []
    institution_key = " ".join(str(institution or "").casefold().split())
    candidates: list[str] = []
    if "ucsf" in institution_key:
        for given_slug in first_slug_variants:
            candidates.append(
                f"https://profiles.ucsf.edu/{given_slug}.{last_slug}"
            )
        for given_slug in first_slug_variants:
            candidates.append(
                "https://www.ucsfhealth.org/providers/"
                f"{given_slug}-{last_slug}"
            )
        for given_slug in first_slug_variants:
            candidates.append(
                "https://www.ucsfbenioffchildrens.org/providers/"
                f"{given_slug}-{last_slug}"
            )
    elif "stanford" in institution_key:
        for given_slug in first_slug_variants:
            candidates.append(
                f"https://profiles.stanford.edu/{given_slug}-{last_slug}"
            )
            candidates.append(
                "https://med.stanford.edu/profiles/"
                f"{given_slug}-{last_slug}"
            )
    return list(dict.fromkeys(candidates))

def _anthem_provider_title_urls(
    name: str,
    institution: str,
) -> list[str]:
    """Return the profile URLs most likely to list an academic title."""
    urls = _anthem_provider_profile_urls(name, institution)

    def rank(url: str) -> int:
        if "ucsfhealth.org/providers" in url:
            return 0
        if "ucsfbenioffchildrens.org/providers" in url:
            return 1
        if (
            "profiles.stanford.edu" in url
            or "med.stanford.edu/profiles" in url
        ):
            return 0
        return 2

    return sorted(urls, key=lambda url: (rank(url), urls.index(url)))

def _anthem_extract_academic_title(text: str) -> str:
    """Extract an academic rank from an institution profile page."""
    if not text:
        return ""
    lines = [
        " ".join(line.split())
        for line in str(text).splitlines()
        if line.strip()
    ]
    labels = {
        "academic title",
        "academic titles",
        "academic appointment",
        "academic appointments",
    }
    skip = {
        "on this page",
        "locations",
        "my research",
        "education & training",
        "board certification",
    }
    for index, line in enumerate(lines):
        stripped = line.casefold().rstrip(":")
        value = ""
        if stripped in labels:
            for following in lines[index + 1 : index + 5]:
                candidate = following.lstrip("-–—•: ").strip()
                if (
                    candidate
                    and candidate.casefold() not in skip
                    and len(candidate) <= 120
                ):
                    value = candidate
                    break
        else:
            match = re.match(
                r"(?i)^academic titles?\s*[:\-–—]\s*(.+)$", line
            )
            if match:
                value = match.group(1).strip()[:120]
        if value:
            return value
    flat = " ".join(lines)
    match = re.search(
        r"\b((?:Clinical |Associate |Assistant |Adjunct |Volunteer )*"
        r"(?:Professor|Instructor|Lecturer)(?: of [A-Za-z&'’,. -]{2,90})?)\b",
        flat,
    )
    if match:
        value = re.split(
            r"\s+(?:at|in the|and)\s+", match.group(1)
        )[0]
        return value.strip(" ,.-")[:120]
    return ""

_ANTHEM_TITLE_CACHE: dict[tuple[str, str], str] = {}

_ANTHEM_TITLE_LOCK = threading.Lock()

def _anthem_academic_title(
    name: str,
    institution: str,
    deadline: float | None = None,
) -> str:
    """Return a provider's academic title from an institution profile.

    ``deadline`` is an optional ``time.monotonic()`` cutoff. When it is
    reached, remaining profile URLs are skipped and the empty result is not
    cached, so a later request can try again.
    """
    name = " ".join(str(name or "").split())
    institution = " ".join(str(institution or "").split())
    if not name or not institution:
        return ""
    key = (institution.casefold(), name.casefold())
    with _ANTHEM_TITLE_LOCK:
        if key in _ANTHEM_TITLE_CACHE:
            return _ANTHEM_TITLE_CACHE[key]
    _first_name, last_name = _anthem_split_name(name)
    last_key = last_name.casefold()
    title = ""
    aborted = False
    for url in _anthem_provider_title_urls(name, institution):
        if deadline is not None and time.monotonic() >= deadline:
            aborted = True
            break
        try:
            response = requests.get(
                url,
                headers={
                    **chat.HTTP_HEADERS,
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=(4, 10),
                allow_redirects=True,
            )
        except requests.RequestException:
            continue
        if response.status_code != 200:
            continue
        content_type = str(
            response.headers.get("Content-Type") or ""
        ).split(";", 1)[0].strip().lower()
        if content_type and "html" not in content_type:
            continue
        if trafilatura is None:
            text = response.text
        else:
            try:
                text = trafilatura.extract(
                    response.text,
                    url=str(response.url or url),
                    include_comments=False,
                    include_tables=False,
                    favor_precision=True,
                ) or ""
            except Exception:
                text = ""
        if not text:
            continue
        if last_key and last_key not in text.casefold():
            continue
        candidate = _anthem_extract_academic_title(text)
        if candidate:
            title = candidate
            break
    if not aborted:
        with _ANTHEM_TITLE_LOCK:
            _ANTHEM_TITLE_CACHE[key] = title
    return title

_ANTHEM_AFFILIATION_CACHE: dict[tuple[str, str, str], bool] = {}

_ANTHEM_AFFILIATION_LOCK = threading.Lock()

def _anthem_ucsf_directory_affiliation(
    name: str,
    npi: str = "",
) -> bool:
    """Return whether the UCSF directory lists this provider.

    A last-name search is one fast API call and is more reliable than
    fetching several profile-page URL variants. It is also the same public
    directory used by the specialist supplement, so the two paths agree.
    """
    _first_name, last_name = _anthem_split_name(name)
    if not last_name:
        return False
    results = _anthem_ucsf_directory_query(last_name, limit=50)
    target_npi = str(npi or "").strip()
    if target_npi:
        return any(
            str(result.get("npi") or "") == target_npi
            for result in results
        )
    return any(
        _anthem_names_match(
            name,
            str(
                result.get("preferredNameDegrees")
                or result.get("fullName")
                or ""
            ),
        )
        for result in results
    )

def _anthem_provider_affiliation(
    name: str,
    institution: str,
    npi: str = "",
) -> bool:
    """Return whether an institution directory confirms a provider.

    This is a lightweight existence/name check for list filtering; the detail
    modal separately extracts the full profile text. Results are cached so a
    follow-up question does not refetch every candidate.
    """
    name = " ".join(str(name or "").split())
    institution = " ".join(str(institution or "").split())
    if not name or not institution:
        return False
    target_npi = str(npi or "").strip()
    key = (institution.casefold(), name.casefold(), target_npi)
    with _ANTHEM_AFFILIATION_LOCK:
        cached = _ANTHEM_AFFILIATION_CACHE.get(key)
        if cached is not None:
            return cached
    if "ucsf" in institution.casefold():
        confirmed = _anthem_ucsf_directory_affiliation(
            name, target_npi
        )
        with _ANTHEM_AFFILIATION_LOCK:
            _ANTHEM_AFFILIATION_CACHE[key] = confirmed
        return confirmed
    urls = _anthem_provider_title_urls(name, institution)
    words = [
        word.casefold()
        for word in re.findall(r"[A-Za-z]{2,}", name)
        if word.casefold() not in {"md", "do", "dr", "phd"}
    ]
    first_word = words[0] if words else ""
    last_word = words[-1] if words else ""
    confirmed = False
    for url in urls:
        try:
            response = requests.get(
                url,
                headers={
                    **chat.HTTP_HEADERS,
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=(3, 8),
                allow_redirects=True,
            )
        except requests.RequestException:
            continue
        if response.status_code != 200:
            continue
        content_type = str(
            response.headers.get("Content-Type") or ""
        ).split(";", 1)[0].strip().lower()
        if content_type and "html" not in content_type:
            continue
        text = response.text.casefold()
        if len(text) < 5000:
            continue
        if first_word and last_word and (
            first_word in text and last_word in text
        ):
            confirmed = True
            break
    with _ANTHEM_AFFILIATION_LOCK:
        _ANTHEM_AFFILIATION_CACHE[key] = confirmed
    return confirmed

def _anthem_provider_profile_page(
    name: str,
    institution: str,
    specialty: str = "",
) -> dict | None:
    """Try to fetch an institution profile page for a provider.

    Only institution families with predictable public profile URLs are
    attempted. The result is passed to the profile model as additional
    evidence, especially for education, awards, and research interests.
    """
    candidates = _anthem_provider_profile_urls(name, institution)
    if not candidates or trafilatura is None:
        return None

    name_words = [
        word.casefold()
        for word in re.findall(r"[A-Za-z]{3,}", name)
        if word.casefold() not in {"md", "do", "dr"}
    ]
    for url in candidates:
        try:
            response = requests.get(
                url,
                headers={
                    **chat.HTTP_HEADERS,
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "text/html,application/xhtml+xml",
                },
                timeout=(5, 20),
                allow_redirects=True,
            )
        except requests.RequestException:
            continue
        if response.status_code != 200:
            continue
        content_type = str(
            response.headers.get("Content-Type") or ""
        ).split(";", 1)[0].strip().lower()
        if content_type and "html" not in content_type:
            continue
        try:
            text = trafilatura.extract(
                response.text,
                url=str(response.url or url),
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )
        except Exception:
            continue
        if not text or len(text) < 300:
            continue
        lowered = text.casefold()
        if name_words and not any(
            word in lowered for word in name_words
        ):
            continue
        return {
            "url": str(response.url or url),
            "text": " ".join(text.split())[:8000],
        }
    return None

def _anthem_profile_sentences(text: str) -> list[str]:
    """Split retrieved public text into compact sentences."""
    text = " ".join(str(text or "").split())
    if not text:
        return []
    return [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", text)
        if sentence.strip()
    ]

def _anthem_join_profile_lines(
    items: list[str],
    limit: int = 4,
) -> str:
    """Deduplicate and join profile lines without inventing new text."""
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        text = " ".join(str(item or "").strip(" -•:;,").split())
        if not text:
            continue
        if text.casefold() in seen:
            continue
        seen.add(text.casefold())
        if text[-1] not in ".!?":
            text += "."
        result.append(text)
        if len(result) >= limit:
            break
    return " ".join(result)

def _anthem_build_provider_profile(
    name: str,
    facts: str,
    publications: list[dict],
    profile_text: str = "",
) -> dict[str, str]:
    """Build profile sections from retrieved public records without an LLM.

    Provider detail used to ask the local question model to write these
    sections, which made the first modal open wait for a 35B generation. The
    sections now use only facts already retrieved from the directory, the
    institution profile page, and the publication list.
    """
    lines = [
        line.strip()
        for line in str(facts or "").splitlines()
        if line.strip()
    ]
    values: dict[str, list[str]] = {}
    for line in lines:
        if line.startswith("-") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = " ".join(value.split())
        if value:
            values.setdefault(key.strip().casefold(), []).append(value)

    def first_value(*keys: str) -> str:
        for key in keys:
            for value in values.get(key.casefold()) or []:
                if value:
                    return value
        return ""

    display_name = first_value("provider", "name") or name or "Provider"
    _first_name, last_name = _anthem_split_name(display_name)
    specialty = first_value("specialty")
    organization = first_value("organization")
    academic_title = first_value("academic title")
    provider_title = first_value("provider title")
    affiliations = first_value("affiliations")
    profile_sentences = _anthem_profile_sentences(profile_text)

    background: list[str] = []
    intro = f"{display_name} is listed as "
    intro += specialty or "a health care provider"
    if organization:
        intro += f" at {organization}"
    if academic_title:
        intro += f" and holds the academic title {academic_title}"
    background.append(intro + ".")
    if provider_title and provider_title.casefold() not in intro.casefold():
        background.append(f"The public directory lists the provider title as {provider_title}.")
    if affiliations and affiliations.casefold() not in intro.casefold():
        background.append(f"Affiliations include {affiliations}.")
    if last_name:
        for sentence in profile_sentences:
            if (
                last_name.casefold() in sentence.casefold()
                and len(sentence) <= 500
            ):
                background.append(sentence)
                if len(background) >= 3:
                    break

    research: list[str] = []
    for sentence in profile_sentences:
        if len(sentence) > 500:
            continue
        if re.search(
            r"\b(?:research|investigat|clinical trial|studies|study)\b",
            sentence,
            re.IGNORECASE,
        ):
            research.append(sentence)
            if len(research) >= 2:
                break
    if publications:
        titles = [
            str(publication.get("title") or "").strip()
            for publication in publications[:3]
            if str(publication.get("title") or "").strip()
        ]
        if titles:
            research.append(
                "Published work includes " + "; ".join(titles) + "."
            )

    education: list[str] = []
    for line in lines:
        candidate = line.strip(" -•:;,")
        if candidate.casefold() in {
            "education",
            "education & training",
            "education and training",
        }:
            continue
        if re.match(
            r"^(?:education|training|fellowship|residency|internship|"
            r"medical school|medical degree|degree|board certificat)",
            candidate,
            re.IGNORECASE,
        ):
            education.append(candidate)
    for sentence in profile_sentences:
        if len(sentence) > 500:
            continue
        if re.search(
            r"\b(?:fellowship|residency|internship|medical school|"
            r"medical degree|board certified)\b",
            sentence,
            re.IGNORECASE,
        ):
            education.append(sentence)

    awards: list[str] = []
    for line in lines:
        if re.search(
            r"\b(?:award|honor|recognition|recognized|"
            r"board certified|board certification|distinguished)\b",
            line,
            re.IGNORECASE,
        ):
            awards.append(line)
    for sentence in profile_sentences:
        if len(sentence) > 500:
            continue
        if re.search(
            r"\b(?:award|honor|recognition|recognized|"
            r"board certified|board certification|distinguished)\b",
            sentence,
            re.IGNORECASE,
        ):
            awards.append(sentence)

    clinical: list[str] = []
    if specialty:
        clinical.append(f"Specializes in {specialty}.")
    if provider_title:
        clinical.append(f"Clinical role: {provider_title}.")
    for line in lines:
        if specialty and re.match(
            r"^specialty\b", line, re.IGNORECASE
        ):
            continue
        if re.match(
            r"^(?:clinical focus|areas? of expertise|specialty)",
            line,
            re.IGNORECASE,
        ):
            clinical.append(line)
    for sentence in profile_sentences:
        if len(sentence) > 500:
            continue
        if re.search(
            r"\b(?:clinical focus|treats|specializes|specializing|"
            r"focuses on|care for)\b",
            sentence,
            re.IGNORECASE,
        ):
            clinical.append(sentence)

    return {
        "background": _anthem_join_profile_lines(background, limit=3),
        "research_interests": _anthem_join_profile_lines(
            research, limit=3
        ),
        "education": _anthem_join_profile_lines(education, limit=5),
        "awards": _anthem_join_profile_lines(awards, limit=4),
        "clinical_focus": _anthem_join_profile_lines(
            clinical, limit=3
        ),
    }

def _anthem_detail_with_summary(
    item: dict,
    summary: str,
    network_labels: str,
) -> dict:
    """Prepend the result-card summary to a provider detail payload."""
    text = " ".join(str(summary or "").split())
    labels = [
        label.strip()
        for label in str(network_labels or "").split(" · ")
        if label.strip()
    ]
    for label in labels:
        if label not in text:
            text = f"{text} · {label}" if text else label
    if not text:
        return item
    result = dict(item)
    blocks = list(item.get("blocks") or [])
    blocks.insert(0, {"type": "paragraph", "text": text})
    result["blocks"] = blocks
    return result

def _anthem_parallel_calls(
    calls: dict[str, Callable[[], object]],
) -> dict[str, object]:
    """Run independent provider-detail network calls concurrently."""
    if not calls:
        return {}
    results: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(calls))) as pool:
        futures = {
            pool.submit(call): key for key, call in calls.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception:
                results[key] = None
    return results

def _anthem_provider_detail(
    npi: str,
    name: str,
    specialty: str = "",
    institution: str = "",
    source_label: str = "",
    summary: str = "",
    network_labels: str = "",
    anthem_provider_id: str = "",
    anthem_address_id: str = "",
) -> dict:
    """Build one provider's detail modal payload.

    Directory facts come from NPPES, publications from PubMed, and the
    narrative sections from retrieved public records. Independent network
    calls run in parallel so the first modal open is limited by the slowest
    source rather than the sum of every source. The modal shows a source note
    so education/award details are not mistaken for verified directory data.
    """
    npi = str(npi or "").strip()
    name = " ".join(str(name or "").split())
    institution = " ".join(str(institution or "").split())
    anthem_provider_id = str(anthem_provider_id or "").strip()
    anthem_address_id = str(anthem_address_id or "").strip()
    cache_key = (
        f"{npi or name.casefold()}|{institution.casefold()}"
        f"|{anthem_provider_id}"
    )
    with _ANTHEM_PROVIDER_DETAIL_LOCK:
        cached = _ANTHEM_PROVIDER_DETAIL_CACHE.get(cache_key)
        if cached:
            return _anthem_detail_with_summary(
                dict(cached), summary, network_labels
            )

    findcare_detail: dict = {}
    record: dict = {}
    record_specialty = ""
    parallel_results: dict[str, object] = {}
    if name:
        tasks: dict[str, Callable[[], object]] = {
            "record": lambda: _anthem_nppes_record_for_provider(
                npi, name
            ),
            "title": lambda: _anthem_academic_title(
                name, institution
            ),
            "profile": lambda: _anthem_provider_profile_page(
                name, institution, specialty
            ),
            "pubmed": lambda: _anthem_pubmed_publications(
                name,
                specialty=specialty,
                institution=institution,
            ),
        }
        if anthem_provider_id:
            tasks["detail"] = lambda: _anthem_findcare_provider_detail(
                anthem_provider_id, anthem_address_id
            )
        if _anthem_is_ucsf_facility(institution):
            tasks["ucsf"] = lambda: _anthem_ucsf_directory_evidence(
                npi, name
            )
        parallel_results = _anthem_parallel_calls(tasks)
        findcare_detail = parallel_results.get("detail") or {}
        record = parallel_results.get("record") or {}
    else:
        # Without a usable name, resolve identity first, then enrich in
        # parallel using the name returned by the directory.
        bootstrap_tasks: dict[str, Callable[[], object]] = {}
        if anthem_provider_id:
            bootstrap_tasks["detail"] = (
                lambda: _anthem_findcare_provider_detail(
                    anthem_provider_id, anthem_address_id
                )
            )
        if npi:
            bootstrap_tasks["record"] = (
                lambda: _anthem_nppes_record_for_provider(npi, "")
            )
        bootstrap = _anthem_parallel_calls(bootstrap_tasks)
        findcare_detail = bootstrap.get("detail") or {}
        record = bootstrap.get("record") or {}
        if findcare_detail:
            name = _anthem_findcare_detail_facts(findcare_detail)[0]
        elif record:
            name = _anthem_provider_record_name(record, "")
        if name:
            tasks = {
                "title": lambda: _anthem_academic_title(
                    name, institution
                ),
                "profile": lambda: _anthem_provider_profile_page(
                    name, institution, specialty
                ),
                "pubmed": lambda: _anthem_pubmed_publications(
                    name,
                    specialty=specialty,
                    institution=institution,
                ),
            }
            if _anthem_is_ucsf_facility(institution):
                tasks["ucsf"] = (
                    lambda: _anthem_ucsf_directory_evidence(npi, name)
                )
            parallel_results = _anthem_parallel_calls(tasks)
    if findcare_detail:
        display_name, findcare_npi, facts = (
            _anthem_findcare_detail_facts(findcare_detail)
        )
        npi = findcare_npi or npi
        record_specialty = " ".join(
            str(taxonomy.get("name") or taxonomy.get("desc") or "")
            for taxonomy in findcare_detail.get("specialty") or []
            if isinstance(taxonomy, dict)
        )
        if not source_label:
            source_label = (
                "Anthem Find Care public provider detail API"
            )
    elif record:
        npi = str(record.get("number") or npi)
        display_name = _anthem_provider_record_name(record, name)
        facts = _anthem_format_nppes([record])
        record_specialty = " ".join(
            str(taxonomy.get("desc") or "")
            for taxonomy in record.get("taxonomies") or []
            if isinstance(taxonomy, dict)
        )
    else:
        display_name = name or "Provider"
        facts = f"Provider: {display_name}\n"
        if specialty:
            facts += f"Specialty: {specialty}\n"
    academic_title = str(parallel_results.get("title") or "")
    if academic_title:
        facts = (
            facts.rstrip()
            + f"\nAcademic title: {academic_title}\n"
        )
    directory_profile_url = ""
    directory_evidence = ""
    ucsf_result = parallel_results.get("ucsf")
    if isinstance(ucsf_result, tuple) and len(ucsf_result) == 2:
        directory_evidence = str(ucsf_result[0] or "")
        directory_profile_url = str(ucsf_result[1] or "")
    if directory_evidence:
        facts = facts.rstrip() + "\n" + directory_evidence + "\n"
    lookup_specialty = specialty or record_specialty
    profile_page = parallel_results.get("profile")
    if not isinstance(profile_page, dict):
        profile_page = None
    publications = parallel_results.get("pubmed") or []
    if not isinstance(publications, list):
        publications = []
    profile = _anthem_build_provider_profile(
        display_name,
        facts,
        publications,
        profile_page["text"] if profile_page else "",
    )

    blocks: list[dict] = []
    directory_items = [
        line.strip() for line in facts.splitlines() if line.strip()
    ]
    if directory_items:
        blocks.append(
            {"type": "heading", "text": "Directory Record", "level": 2}
        )
        blocks.append({"type": "list", "items": directory_items})

    sections = (
        ("Background", "background"),
        ("Research Interests", "research_interests"),
        ("Education & Training", "education"),
        ("Awards & Recognition", "awards"),
        ("Clinical Focus", "clinical_focus"),
    )
    for heading, key in sections:
        text = str(profile.get(key) or "").strip()
        if not text:
            continue
        blocks.append(
            {"type": "heading", "text": heading, "level": 2}
        )
        for paragraph in re.split(r"\n\s*\n", text):
            paragraph = " ".join(paragraph.split())
            if paragraph:
                blocks.append({"type": "paragraph", "text": paragraph})

    if publications:
        blocks.append(
            {
                "type": "heading",
                "text": "Selected Publications",
                "level": 2,
            }
        )
        blocks.append(
            {
                "type": "list",
                "items": [
                    f"{publication['title']} — "
                    f"{publication['journal']} ({publication['date']})"
                    for publication in publications
                ],
            }
        )

    first_name, last_name = _anthem_split_name(display_name)
    nppes_link = (
        f"https://npiregistry.cms.hhs.gov/provider-view/{npi}"
        if npi
        else "https://npiregistry.cms.hhs.gov/search?q="
        + quote_plus(display_name)
    )
    notes = []
    if findcare_detail:
        notes.append(
            "Provider profile facts come from Anthem's public Find Care "
            "provider-detail API."
        )
    if npi:
        notes.append(f"CMS NPPES record: {nppes_link}")
    if profile_page:
        notes.append(
            f"Institution profile: {profile_page['url']}"
        )
    if (
        directory_profile_url
        and (
            not profile_page
            or directory_profile_url != profile_page["url"]
        )
    ):
        notes.append(
            "UCSF Health provider directory: "
            f"{directory_profile_url}"
        )
    elif directory_evidence:
        notes.append(
            "UCSF Health provider directory record matched by NPI/name."
        )
    if publications:
        query = quote_plus(f"{last_name} {first_name}".strip())
        notes.append(
            "PubMed author search: "
            f"https://pubmed.ncbi.nlm.nih.gov/?term={query}"
        )
    notes.append(
        "Directory facts come from CMS NPPES. Institution profile and "
        "publication data come from the linked public pages. Verify "
        "education, awards, and network participation with the provider "
        "or institution."
    )
    blocks.append(
        {"type": "heading", "text": "Sources & Notes", "level": 2}
    )
    blocks.append({"type": "list", "items": notes})

    # The article/news Learn and Chat controls store readable paragraphs, not
    # the structured blocks. Flatten the modal content once here so provider
    # profiles can be learned and discussed through the same pipeline.
    learn_paragraphs: list[str] = []
    if summary:
        learn_paragraphs.append(summary)
    for block in blocks:
        if block.get("type") == "paragraph":
            text = " ".join(str(block.get("text") or "").split())
            if text:
                learn_paragraphs.append(text)
        elif block.get("type") == "list":
            items = [
                " ".join(str(item).split())
                for item in block.get("items") or []
                if str(item).strip()
            ]
            if items:
                learn_paragraphs.append(" ".join(items))
    learn_paragraphs = list(dict.fromkeys(learn_paragraphs))

    item = {
        "title": display_name,
        "source": (
            "Anthem Find Care provider detail API"
            if findcare_detail
            else "Anthem Provider Directory · CMS NPPES"
        ),
        "link": nppes_link,
        "section": specialty or record_specialty,
        "docs": True,
        "docs_label": "Anthem" if findcare_detail else "NPPES",
        "learnable": True,
        "learn_noun": "provider",
        "findable": True,
        "article_api": "/api/anthem_provider",
        "complete": True,
        "blocks": blocks,
        "paragraphs": learn_paragraphs,
        "images": [],
        "profile_source": source_label,
        "institution": institution,
    }
    with _ANTHEM_PROVIDER_DETAIL_LOCK:
        _ANTHEM_PROVIDER_DETAIL_CACHE[cache_key] = item
    return _anthem_detail_with_summary(
        dict(item), summary, network_labels
    )

def _anthem_add_academic_titles(
    items: list[dict],
    institution: str,
) -> None:
    """Fill ``academic_title`` on provider items in parallel."""
    if not items or not _anthem_facility_profile_supported(institution):
        return

    def load_title(item: dict) -> tuple[dict, str]:
        return item, _anthem_academic_title(item["name"], institution)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(load_title, item) for item in items]
        for future in as_completed(futures):
            try:
                item, title = future.result()
            except Exception:
                continue
            if not title:
                continue
            item["academic_title"] = title
            base = str(item.get("summary") or "")
            item["summary"] = (
                f"Academic title: {title}"
                + (f" · {base}" if base else "")
            )
            item["paragraphs"] = [item["summary"]]

def _anthem_provider_items_from_context(
    context: str,
    source_label: str = "",
    institution: str = "",
) -> list[dict]:
    """Turn formatted provider blocks into clickable reader cards."""
    items: list[dict] = []
    seen: set[tuple[str, str]] = set()
    institution = " ".join(str(institution or "").split())
    for block in str(context or "").split("\n\n"):
        record: dict[str, str] = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            record[key.strip().casefold()] = value.strip()
        name = record.get("provider", "")
        if not name:
            continue
        npi = record.get("npi", "")
        specialty = record.get("specialty", "")
        organization = record.get("organization", "")
        address = record.get("address", "")
        phone = record.get("phone/contact", "") or record.get("phone", "")
        fax = record.get("fax", "")
        network = record.get("network", "")
        anthem_provider_id = record.get("anthem provider id", "")
        anthem_address_id = record.get("anthem address id", "")
        contact = phone
        if fax and "fax" not in contact.casefold():
            contact = (
                f"{contact} · fax {fax}" if contact else f"fax {fax}"
            )
        identity = (npi or name.casefold(), address.casefold())
        if identity in seen:
            continue
        seen.add(identity)
        summary = " · ".join(
            part
            for part in (specialty, organization, address, contact)
            if part
        )
        link_specialty = specialty.split(";", 1)[0]
        link_specialty = re.sub(
            r"\s+license\b.*$", "", link_specialty, flags=re.IGNORECASE
        ).strip(" ;,")
        if link_specialty.endswith("(primary)"):
            link_specialty = link_specialty[: -len("(primary)")].strip()
        query_parts = []
        if link_specialty:
            query_parts.append(
                "specialty=" + quote_plus(link_specialty)
            )
        if institution:
            query_parts.append(
                "institution=" + quote_plus(institution)
            )
        if anthem_provider_id:
            query_parts.append(
                "anthem_provider_id=" + quote_plus(anthem_provider_id)
            )
        if anthem_address_id:
            query_parts.append(
                "anthem_address_id=" + quote_plus(anthem_address_id)
            )
        query = "&".join(query_parts)
        if npi:
            link = (
                "https://npiregistry.cms.hhs.gov/provider-view/"
                f"{npi}"
            )
        else:
            link = (
                "https://npiregistry.cms.hhs.gov/search?q="
                + quote_plus(name)
            )
        if query:
            separator = "?" if npi else "&"
            link += separator + query
        items.append(
            {
                "title": name,
                "name": name,
                "npi": npi,
                "anthem_provider_id": anthem_provider_id,
                "anthem_address_id": anthem_address_id,
                "organization": organization,
                "specialty": specialty,
                "academic_title": "",
                "address": address,
                "phone": phone,
                "fax": fax,
                "network": network,
                "anthem_status_pending": True,
                "link": link,
                "source": "Anthem Provider Directory",
                "section": specialty,
                "summary": summary,
                "paragraphs": [summary] if summary else [],
                "docs": True,
                "docs_label": "NPPES",
                "learnable": True,
                "findable": True,
                "learn_noun": "provider",
                "article_api": "/api/anthem_provider",
                "complete": True,
                "profile_source": source_label,
                "institution": institution,
            }
        )
    return items

_ANTHEM_UCSF_DIRECTORY_CACHE: dict[str, list[dict]] = {}

_ANTHEM_UCSF_DIRECTORY_LOCK = threading.Lock()

def _anthem_ucsf_directory_query(term: str, limit: int = 50) -> list[dict]:
    """Search the UCSF Health public provider directory."""
    term = " ".join(str(term or "").split())
    if not term:
        return []
    key = f"{term.casefold()}|{limit}"
    with _ANTHEM_UCSF_DIRECTORY_LOCK:
        cached = _ANTHEM_UCSF_DIRECTORY_CACHE.get(key)
        if cached is not None:
            return [dict(item) for item in cached]
    try:
        response = requests.post(
            "https://www.ucsfhealth.org/api/backend/providers",
            params={"unified": term, "page": "1"},
            json={
                "pageContext": ["delivery:ucsf:provider"],
                "options": {"brand": ["UCSF Health"]},
                "per_page": str(limit),
            },
            headers={
                **chat.HTTP_HEADERS,
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
            timeout=(5, 20),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError):
        return []
    results = [
        item
        for item in data.get("results") or []
        if isinstance(item, dict) and item.get("npi")
    ]
    with _ANTHEM_UCSF_DIRECTORY_LOCK:
        _ANTHEM_UCSF_DIRECTORY_CACHE[key] = results
    return [dict(item) for item in results]

def _anthem_ucsf_directory_items(
    results: list[dict],
    facility: str,
    source_label: str,
) -> list[dict]:
    """Convert UCSF directory records into provider cards."""
    items: list[dict] = []
    seen: set[str] = set()
    for result in results:
        npi = str(result.get("npi") or "").strip()
        name = str(
            result.get("preferredNameDegrees")
            or result.get("fullName")
            or ""
        ).strip()
        if not name:
            continue
        identity = npi or name.casefold()
        if identity in seen:
            continue
        seen.add(identity)
        specialty = "; ".join(
            str(value).strip()
            for value in result.get("specialties") or []
            if str(value).strip()
        )
        locations = [
            value
            for value in result.get("markatableLocations") or []
            if isinstance(value, dict)
        ]
        locations.sort(
            key=lambda value: int(value.get("rank") or 999)
        )
        organization = ""
        address = ""
        phone = ""
        if locations:
            location = locations[0]
            organization = str(location.get("name") or "").strip()
            locality = " ".join(
                part
                for part in (
                    str(location.get("state") or "").strip(),
                    str(location.get("zipCode") or "").strip(),
                )
                if part
            )
            address = ", ".join(
                part
                for part in (
                    str(location.get("streetAddress") or "").strip(),
                    str(location.get("city") or "").strip(),
                    locality,
                )
                if part
            )
            contact = location.get("contactPrimary") or {}
            phone = str(contact.get("value") or "").strip()
        summary = " · ".join(
            part
            for part in (specialty, organization, address, phone)
            if part
        )
        if npi:
            link = (
                "https://npiregistry.cms.hhs.gov/provider-view/"
                f"{npi}"
            )
            query_parts = []
            if specialty:
                query_parts.append(
                    "specialty=" + quote_plus(specialty)
                )
            if facility:
                query_parts.append(
                    "institution=" + quote_plus(facility)
                )
            if query_parts:
                link += "?" + "&".join(query_parts)
        else:
            link = "https://www.ucsfhealth.org" + str(
                result.get("slug") or ""
            )
        items.append(
            {
                "title": name,
                "name": name,
                "npi": npi,
                "organization": organization,
                "specialty": specialty,
                "academic_title": str(
                    result.get("providerTitle") or ""
                ).strip(),
                "address": address,
                "phone": phone,
                "fax": "",
                "link": link,
                "source": "UCSF Health Provider Directory",
                "section": specialty,
                "summary": summary,
                "paragraphs": [summary] if summary else [],
                "docs": True,
                "docs_label": "UCSF",
                "learnable": True,
                "findable": True,
                "learn_noun": "provider",
                "article_api": "/api/anthem_provider",
                "complete": True,
                "profile_source": source_label,
                "institution": facility,
            }
        )
    return items

def _anthem_ucsf_directory_record(
    npi: str,
    name: str,
) -> dict | None:
    """Find a UCSF directory record by NPI or full name."""
    name = " ".join(str(name or "").split())
    if not name and not npi:
        return None
    clean_name = re.sub(
        r",?\s*(?:M\.?D\.?|D\.?O\.?|Ph\.?D\.?|MBBS|MPH|MBA|"
        r"MS|M\.S\.|BSN|NP|PA-C|RN)\b.*$",
        "",
        name,
        flags=re.IGNORECASE,
    ).strip(" ,")
    results = _anthem_ucsf_directory_query(
        clean_name or name, limit=20
    )
    target_npi = str(npi or "").strip()
    if target_npi:
        for result in results:
            if str(result.get("npi") or "") == target_npi:
                return result

    def normalize(value: str) -> str:
        value = re.sub(
            r",?\s*(?:M\.?D\.?|D\.?O\.?|Ph\.?D\.?|MBBS|MPH|MBA|"
            r"MS|M\.S\.|BSN|NP|PA-C|RN)\b.*$",
            "",
            str(value or ""),
            flags=re.IGNORECASE,
        )
        return " ".join(value.casefold().split())

    wanted = normalize(name)
    if not wanted:
        return None
    for result in results:
        display = str(
            result.get("preferredNameDegrees")
            or result.get("fullName")
            or ""
        )
        if normalize(display) == wanted:
            return result
    return None

def _anthem_ucsf_directory_evidence(
    npi: str,
    name: str,
) -> tuple[str, str]:
    """Return structured UCSF directory facts and a profile URL.

    The UCSF directory exposes training history and provider titles that may
    not survive HTML extraction. Passing them to the profile model gives the
    Education & Training section a reliable public source even when the
    institution page is unavailable.
    """
    record = _anthem_ucsf_directory_record(npi, name)
    if not record:
        return "", ""
    lines = ["UCSF Health public provider directory:"]
    title = str(record.get("providerTitle") or "").strip()
    if title:
        lines.append(f"Provider title: {title}")
    affiliations = [
        str(value).strip()
        for value in record.get("affiliations") or []
        if str(value).strip()
    ]
    if affiliations:
        lines.append(
            "Affiliations: " + "; ".join(affiliations)
        )
    training = [
        item
        for item in record.get("training") or []
        if isinstance(item, dict)
    ]
    if training:
        lines.append("Education & training:")
        for entry in training:
            name_value = str(entry.get("name") or "").strip()
            type_value = str(entry.get("type") or "").strip()
            end_value = str(entry.get("endDate") or "").strip()
            label = ": ".join(
                part for part in (type_value, name_value) if part
            )
            if label:
                lines.append(
                    "- " + label
                    + (f" ({end_value})" if end_value else "")
                )
    languages = [
        str(value).strip()
        for value in record.get("languages") or []
        if str(value).strip()
    ]
    if languages:
        lines.append("Languages: " + "; ".join(languages))
    accepting = record.get("acceptingNewPatients")
    if accepting is not None:
        lines.append(
            "Accepting new patients: "
            + ("yes" if accepting else "no")
        )
    if len(lines) == 1:
        return "", ""
    slug = str(record.get("slug") or "").strip()
    url = (
        "https://www.ucsfhealth.org" + slug if slug else ""
    )
    return "\n".join(lines), url

_ANTHEM_CONDITION_ANCHORS = {
    "alzheimer",
    "aneurysm",
    "arrhythmia",
    "arthritis",
    "asthma",
    "cancer",
    "carcinoma",
    "copd",
    "deficiency",
    "dementia",
    "diabetes",
    "disease",
    "disorder",
    "epilepsy",
    "failure",
    "fibrosis",
    "hypertension",
    "infection",
    "leukemia",
    "lymphoma",
    "melanoma",
    "neoplasm",
    "parkinson",
    "sarcoma",
    "sclerosis",
    "syndrome",
    "transplant",
    "tumor",
    "tumour",
}

_ANTHEM_DIRECTORY_STOPWORDS = _ANTHEM_GENERIC_KEYWORDS | {
    "all",
    "also",
    "and",
    "another",
    "any",
    "around",
    "at",
    "available",
    "board",
    "both",
    "can",
    "certified",
    "each",
    "every",
    "few",
    "find",
    "get",
    "good",
    "have",
    "i",
    "in",
    "list",
    "looking",
    "many",
    "me",
    "most",
    "my",
    "near",
    "nearby",
    "need",
    "on",
    "only",
    "operation",
    "operations",
    "other",
    "please",
    "procedure",
    "recommend",
    "recommendation",
    "search",
    "several",
    "show",
    "some",
    "specialising",
    "specialist",
    "specialists",
    "specialize",
    "specialized",
    "specializes",
    "specializing",
    "surgeon",
    "surgeons",
    "surgery",
    "surgical",
    "top",
    "treat",
    "treating",
    "treats",
    "want",
    "who",
    "with",
    "would",
}

_ANTHEM_CONDITION_PATTERN = re.compile(
    r"((?:[a-z0-9'-]+\s+){0,4}(?:"
    + "|".join(
        sorted(_ANTHEM_CONDITION_ANCHORS, key=len, reverse=True)
    )
    + r"))\b"
)

def _anthem_is_ucsf_facility(facility: str) -> bool:
    """Return whether a facility is covered by the UCSF directory API."""
    key = " ".join(str(facility or "").casefold().split())
    return "ucsf" in key or "university of california san francisco" in key

_ANTHEM_FINDCARE_BASE = "https://findcare.anthem.com"

_ANTHEM_PPO_PREFIX_FALLBACK = ("2402", "2471")

_ANTHEM_PPO_LABELS = {
    "listed": "In Network (Anthem PPO)",
    "not_listed": "Out of Network (Anthem PPO)",
    "unknown": "Network Unknown (Anthem PPO)",
}

_ANTHEM_PPO_FLAGS = {
    "listed": "In Network",
    "not_listed": "Out of Network",
    "unknown": "Network Unknown",
}

_ANTHEM_BLUECARD_PREFIX_FALLBACK = ("901", "904")

_ANTHEM_BLUECARD_LABELS = {
    "listed": "In Network (Anthem BlueCard National PPO)",
    "not_listed": "Out of Network (Anthem BlueCard National PPO)",
    "unknown": "Network Unknown (Anthem BlueCard National PPO)",
}

_ANTHEM_BLUECARD_FLAGS = {
    "listed": "In Network",
    "not_listed": "Out of Network",
    "unknown": "Network Unknown",
}

_ANTHEM_STATE_COORDS = {
    "AL": ("32.806671", "-86.791130"),
    "AK": ("61.370716", "-152.404419"),
    "AZ": ("33.729759", "-111.431221"),
    "AR": ("34.969704", "-92.373123"),
    "CA": ("37.7749", "-122.4194"),
    "CO": ("39.059811", "-105.311104"),
    "CT": ("41.597782", "-72.755371"),
    "DE": ("39.318523", "-75.507141"),
    "DC": ("38.897438", "-77.026817"),
    "FL": ("27.766279", "-81.686783"),
    "GA": ("33.040619", "-83.643074"),
    "GU": ("13.4443", "144.7937"),
    "HI": ("21.094318", "-157.498337"),
    "ID": ("44.240459", "-114.478828"),
    "IL": ("40.349457", "-88.986137"),
    "IN": ("39.849426", "-86.258278"),
    "IA": ("42.011539", "-93.210526"),
    "KS": ("38.526600", "-96.726486"),
    "KY": ("37.668140", "-84.670067"),
    "LA": ("31.169546", "-91.867805"),
    "ME": ("44.693947", "-69.381927"),
    "MD": ("39.063946", "-76.802101"),
    "MA": ("42.230171", "-71.530106"),
    "MI": ("43.326618", "-84.536095"),
    "MN": ("45.694454", "-93.900192"),
    "MS": ("32.741646", "-89.678696"),
    "MO": ("38.456085", "-92.288368"),
    "MT": ("46.921925", "-110.454353"),
    "NE": ("41.125370", "-98.268082"),
    "NV": ("38.313515", "-117.055374"),
    "NH": ("43.452492", "-71.563896"),
    "NJ": ("40.298904", "-74.521011"),
    "NM": ("34.840515", "-106.248482"),
    "NY": ("42.165726", "-74.948051"),
    "NC": ("35.630066", "-79.806419"),
    "ND": ("47.528912", "-99.784012"),
    "MP": ("15.0979", "145.6739"),
    "OH": ("40.388783", "-82.764915"),
    "OK": ("35.565342", "-96.928917"),
    "OR": ("44.572021", "-122.070938"),
    "PA": ("40.590752", "-77.209755"),
    "PR": ("18.220833", "-66.590149"),
    "RI": ("41.680893", "-71.511780"),
    "SC": ("33.856892", "-80.945007"),
    "SD": ("44.299782", "-99.438828"),
    "TN": ("35.747845", "-86.692345"),
    "TX": ("31.054487", "-97.563461"),
    "UT": ("40.150032", "-111.862434"),
    "VT": ("44.045876", "-72.710686"),
    "VI": ("18.3358", "-64.8963"),
    "VA": ("37.769337", "-78.169968"),
    "WA": ("47.400902", "-121.490494"),
    "WV": ("38.491226", "-80.954453"),
    "WI": ("44.268543", "-89.616508"),
    "WY": ("42.755966", "-107.302490"),
}

_ANTHEM_FINDCARE_TOKEN_LOCK = threading.Lock()

_ANTHEM_FINDCARE_TOKEN_CACHE: dict[str, object] = {
    "value": "",
    "expires_at": 0.0,
}

_ANTHEM_PLAN_LOCK = threading.Lock()

_ANTHEM_PLAN_CACHE: dict[str, list[dict]] = {}

_ANTHEM_PPO_PREFIX_LOCK = threading.Lock()

_ANTHEM_PPO_PREFIX_CACHE: dict[str, tuple[str, ...]] = {}

_ANTHEM_PPO_LOCK = threading.Lock()

_ANTHEM_PPO_CACHE: dict[tuple[str, str], str] = {}

_ANTHEM_BLUECARD_PREFIX_LOCK = threading.Lock()

_ANTHEM_BLUECARD_PREFIX_CACHE: dict[str, tuple[str, ...]] = {}

_ANTHEM_BLUECARD_LOCK = threading.Lock()

_ANTHEM_BLUECARD_CACHE: dict[tuple[str, str], str] = {}

_ANTHEM_MEMBER_PLAN_LOCK = threading.Lock()

_ANTHEM_MEMBER_PLAN_CACHE: dict[str, dict] = {}

_ANTHEM_MEMBER_STATUS_LOCK = threading.Lock()

_ANTHEM_MEMBER_STATUS_CACHE: dict[tuple[str, str], str] = {}

_ANTHEM_MEMBER_ID_FILES = (
    SCRIPT_DIR / ".chat" / "anthem" / "member_id.txt",
    SCRIPT_DIR / "prompts" / ".chat" / "anthem" / "member_id.txt",
)

# Network status is keyed by NPI (or name), state, and plan prefix. Unlike the
# per-plan in-memory caches below, this cache survives server restarts so a
# repeat search does not have to call Anthem's Find Care API again.
_ANTHEM_STATUS_CACHE_PATH = (
    SCRIPT_DIR / ".chat" / "anthem" / "network_status_cache.json"
)

_ANTHEM_STATUS_CACHE_TTL = 7 * 24 * 60 * 60

_ANTHEM_PREFIX_STATUS_LOCK = threading.Lock()

_ANTHEM_PREFIX_STATUS_CACHE: dict[str, dict] = {}

_ANTHEM_PREFIX_STATUS_LOADED = False

def _anthem_status_cache_key(
    npi: str,
    name: str,
    state: str,
    prefix: str,
) -> str:
    """Return a stable cache key for one provider/state/plan prefix."""
    identity = " ".join(str(npi or "").split()) or " ".join(
        str(name or "").casefold().split()
    )
    return (
        f"{identity}|{str(state or '').strip().upper()}|"
        f"{str(prefix or '').strip()}"
    )

def _anthem_status_cache_load_locked() -> None:
    """Load the persisted prefix cache once while holding the lock."""
    global _ANTHEM_PREFIX_STATUS_LOADED
    if _ANTHEM_PREFIX_STATUS_LOADED:
        return
    _ANTHEM_PREFIX_STATUS_LOADED = True
    try:
        data = json.loads(
            _ANTHEM_STATUS_CACHE_PATH.read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return
    entries = data.get("entries") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return
    now = time.time()
    for key, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "")
        checked_at = float(entry.get("checked_at") or 0.0)
        if status not in {"listed", "not_listed"} or not checked_at:
            continue
        if now - checked_at > _ANTHEM_STATUS_CACHE_TTL:
            continue
        _ANTHEM_PREFIX_STATUS_CACHE[str(key)] = {
            "status": status,
            "checked_at": checked_at,
        }

def _anthem_status_cache_get(key: str) -> str | None:
    """Return a cached prefix status, or ``None`` when it is stale."""
    with _ANTHEM_PREFIX_STATUS_LOCK:
        _anthem_status_cache_load_locked()
        entry = _ANTHEM_PREFIX_STATUS_CACHE.get(key)
        if not entry:
            return None
        checked_at = float(entry.get("checked_at") or 0.0)
        if time.time() - checked_at > _ANTHEM_STATUS_CACHE_TTL:
            _ANTHEM_PREFIX_STATUS_CACHE.pop(key, None)
            return None
        status = str(entry.get("status") or "")
        return status or None

def _anthem_status_cache_set(key: str, status: str) -> None:
    """Persist one definitive prefix status for future searches."""
    if status not in {"listed", "not_listed"}:
        return
    with _ANTHEM_PREFIX_STATUS_LOCK:
        _anthem_status_cache_load_locked()
        _ANTHEM_PREFIX_STATUS_CACHE[key] = {
            "status": status,
            "checked_at": time.time(),
        }
        # Keep the file bounded even after many months of searches.
        if len(_ANTHEM_PREFIX_STATUS_CACHE) > 20000:
            newest = sorted(
                _ANTHEM_PREFIX_STATUS_CACHE.items(),
                key=lambda item: float(item[1].get("checked_at") or 0.0),
                reverse=True,
            )[:20000]
            _ANTHEM_PREFIX_STATUS_CACHE.clear()
            _ANTHEM_PREFIX_STATUS_CACHE.update(dict(newest))
        payload = {
            "version": 1,
            "entries": dict(_ANTHEM_PREFIX_STATUS_CACHE),
        }
        try:
            _ANTHEM_STATUS_CACHE_PATH.parent.mkdir(
                parents=True, exist_ok=True
            )
            temporary = _ANTHEM_STATUS_CACHE_PATH.with_name(
                _ANTHEM_STATUS_CACHE_PATH.name
                + f".{os.getpid()}.{threading.get_ident()}.tmp"
            )
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=0),
                encoding="utf-8",
            )
            os.replace(temporary, _ANTHEM_STATUS_CACHE_PATH)
        except OSError:
            pass

def _anthem_findcare_headers(token: str = "") -> dict[str, str]:
    """Return headers for Anthem's public Find Care API."""
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Origin": _ANTHEM_FINDCARE_BASE,
        "Referer": _ANTHEM_FINDCARE_BASE + "/",
        "meta-brandcd": "ABCBS",
        "meta-locale": "en_US",
        "meta-consumerapp": "FINDCARE",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

def _anthem_findcare_token() -> str:
    """Return a cached JWT for Anthem's public Find Care API."""
    now = time.time()
    with _ANTHEM_FINDCARE_TOKEN_LOCK:
        cached = str(_ANTHEM_FINDCARE_TOKEN_CACHE.get("value") or "")
        expires_at = float(
            _ANTHEM_FINDCARE_TOKEN_CACHE.get("expires_at") or 0.0
        )
        if cached and expires_at > now:
            return cached
    token = ""
    try:
        response = requests.get(
            _ANTHEM_FINDCARE_BASE
            + "/precare/api/utility/data-modifiedon",
            headers=_anthem_findcare_headers(),
            timeout=(5, 20),
        )
        response.raise_for_status()
        token = str(response.json().get("token") or "").strip()
    except (requests.RequestException, ValueError):
        token = ""
    if token:
        with _ANTHEM_FINDCARE_TOKEN_LOCK:
            _ANTHEM_FINDCARE_TOKEN_CACHE["value"] = token
            _ANTHEM_FINDCARE_TOKEN_CACHE["expires_at"] = now + 2700
    return token

def _anthem_member_plan() -> dict:
    """Return the Anthem plan tied to the stored member ID."""
    with _ANTHEM_MEMBER_PLAN_LOCK:
        cached = _ANTHEM_MEMBER_PLAN_CACHE.get("value")
    if cached:
        return dict(cached)
    member_id = ""
    for path in _ANTHEM_MEMBER_ID_FILES:
        try:
            if path.is_file():
                member_id = " ".join(
                    path.read_text(encoding="utf-8", errors="ignore").split()
                )
                if member_id:
                    break
        except OSError:
            continue
    plan = {
        "member_id": member_id,
        "prefix": member_id[:3].upper() if member_id else "",
        "plan_name": "",
        "product_type": "",
        "state": "",
        "effective_date": "",
    }
    token = _anthem_findcare_token()
    if member_id and token:
        try:
            response = requests.post(
                _ANTHEM_FINDCARE_BASE
                + "/precare/api/member/public/v1/summary",
                headers=_anthem_findcare_headers(token),
                json={
                    "identificationNumber": member_id,
                    "locale": "en_US",
                },
                timeout=(5, 25),
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
            data = {}
        members = data.get("memberList") or []
        if members:
            member = members[0] if isinstance(members[0], dict) else {}
            coverage = member.get("coverageDetails") or {}
            prefix = str(coverage.get("surrogatePrefix") or "").strip()
            if prefix:
                plan["prefix"] = prefix.upper()
            product_names = [
                item
                for item in coverage.get("productNames") or []
                if isinstance(item, dict)
                and str(item.get("type") or "").upper() == "MEDICAL"
            ]
            if product_names:
                plan["plan_name"] = str(
                    product_names[0].get("name") or ""
                ).strip()
            plan["product_type"] = str(
                coverage.get("productType") or ""
            ).strip()
            plan["state"] = str(
                (member.get("address") or {}).get("state") or ""
            ).strip()
            plan["effective_date"] = str(
                coverage.get("coverageEffectiveDate") or ""
            ).strip()
    with _ANTHEM_MEMBER_PLAN_LOCK:
        _ANTHEM_MEMBER_PLAN_CACHE["value"] = dict(plan)
    return dict(plan)

_ANTHEM_MEMBER_PLAN_FLAGS = {
    "listed": "In Network",
    "not_listed": "Out Of Network",
    "unknown": "Out Of Network",
}

def _anthem_member_plan_label(status: str) -> str:
    """Return the binary member-plan status shown in the result list.

    Anthem's public directory can occasionally fail to confirm a provider.
    The result list deliberately shows only In Network or Out Of Network, so
    an unconfirmed lookup is reported as Out Of Network rather than an
    Unknown state.
    """
    return {
        "listed": "In Network",
        "not_listed": "Out Of Network",
        "unknown": "Out Of Network",
    }.get(status, "Out Of Network")

def _anthem_findcare_member_status(
    name: str,
    state: str,
    npi: str = "",
    city: str = "",
) -> str:
    """Return network status for the stored Anthem member plan."""
    plan = _anthem_member_plan()
    prefix = str(plan.get("prefix") or "").strip()
    if not prefix:
        return "unknown"
    return _anthem_findcare_plan_status(
        name,
        state,
        (prefix,),
        _ANTHEM_MEMBER_STATUS_CACHE,
        _ANTHEM_MEMBER_STATUS_LOCK,
        npi=npi,
        city=city,
    )

def _anthem_findcare_plans(state: str) -> list[dict]:
    """Return Anthem's public plan list for one state."""
    state = " ".join(str(state or "CA").split()).upper() or "CA"
    with _ANTHEM_PLAN_LOCK:
        cached = _ANTHEM_PLAN_CACHE.get(state)
    if cached is not None:
        return [dict(plan) for plan in cached]
    token = _anthem_findcare_token()
    plans: list[dict] = []
    if token:
        try:
            response = requests.get(
                _ANTHEM_FINDCARE_BASE
                + "/precare/api/utility/v1/public-plan/ABCBS/"
                f"states/{state}/plans",
                headers=_anthem_findcare_headers(token),
                timeout=(5, 25),
            )
            response.raise_for_status()
            plans = [
                plan
                for plan in response.json().get("data") or []
                if isinstance(plan, dict)
            ]
        except (requests.RequestException, ValueError):
            plans = []
    with _ANTHEM_PLAN_LOCK:
        _ANTHEM_PLAN_CACHE[state] = plans
    return [dict(plan) for plan in plans]

def _anthem_findcare_ppo_prefixes(state: str) -> tuple[str, ...]:
    """Return Anthem commercial PPO alpha prefixes offered in one state."""
    state = " ".join(str(state or "CA").split()).upper() or "CA"
    with _ANTHEM_PPO_PREFIX_LOCK:
        cached = _ANTHEM_PPO_PREFIX_CACHE.get(state)
    if cached:
        return cached
    prefixes: list[str] = []
    ranked: list[tuple[int, str]] = []
    for plan in _anthem_findcare_plans(state):
        if str(plan.get("careType") or "").upper() != "MEDICAL":
            continue
        if "PPO" not in str(plan.get("planType") or "").upper():
            continue
        prefix = str(plan.get("prefix") or "").strip()
        if not prefix or prefix in prefixes:
            continue
        categories = {
            str(value).upper()
            for value in plan.get("planCategoryList") or []
        }
        commercial = bool(categories & {"EMP", "IND", "STU", "NON"})
        ranked.append((0 if commercial else 1, prefix))
    for _score, prefix in sorted(ranked):
        if prefix not in prefixes:
            prefixes.append(prefix)
    if not prefixes:
        prefixes = list(_ANTHEM_PPO_PREFIX_FALLBACK)
    result = tuple(prefixes[:3])
    with _ANTHEM_PPO_PREFIX_LOCK:
        _ANTHEM_PPO_PREFIX_CACHE[state] = result
    return result

def _anthem_findcare_bluecard_prefixes(state: str) -> tuple[str, ...]:
    """Return Anthem BlueCard National PPO alpha prefixes."""
    state = " ".join(str(state or "CA").split()).upper() or "CA"
    with _ANTHEM_BLUECARD_PREFIX_LOCK:
        cached = _ANTHEM_BLUECARD_PREFIX_CACHE.get(state)
    if cached:
        return cached
    prefixes: list[str] = []
    for plan in _anthem_findcare_plans(state):
        if str(plan.get("careType") or "").upper() != "MEDICAL":
            continue
        plan_type = str(plan.get("planType") or "").upper()
        name = str(plan.get("name") or "")
        if "PPO" not in plan_type:
            continue
        if not plan.get("isNationalPlan"):
            continue
        if "bluecard" not in name.casefold() and "national ppo" not in name.casefold():
            continue
        prefix = str(plan.get("prefix") or "").strip()
        if prefix and prefix not in prefixes:
            prefixes.append(prefix)
    if not prefixes:
        prefixes = list(_ANTHEM_BLUECARD_PREFIX_FALLBACK)
    result = tuple(prefixes[:2])
    with _ANTHEM_BLUECARD_PREFIX_LOCK:
        _ANTHEM_BLUECARD_PREFIX_CACHE[state] = result
    return result

def _anthem_name_tokens(value: str) -> list[str]:
    credentials = {
        "md", "do", "phd", "dr", "mba", "mph", "ms", "msc", "mbbs",
        "mbs", "bsc", "bs", "bsn", "rn", "np", "pa", "pac", "facp",
        "facc", "facs", "faap", "frcp", "dds", "dmd", "od", "dc",
    }
    return [
        word
        for word in re.findall(r"[A-Za-z]{2,}", str(value or "").casefold())
        if word not in credentials
    ]

def _anthem_names_match(left: str, right: str) -> bool:
    left_tokens = _anthem_name_tokens(left)
    right_tokens = _anthem_name_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    if left_tokens[-1] != right_tokens[-1]:
        return False
    return left_tokens[0] == right_tokens[0]

def _anthem_facility_names_match(left: str, right: str) -> bool:
    """Return whether one name's tokens are contained in the other."""
    left_tokens = set(_anthem_name_tokens(left))
    right_tokens = set(_anthem_name_tokens(right))
    if not left_tokens or not right_tokens:
        return False
    return left_tokens.issubset(right_tokens) or right_tokens.issubset(
        left_tokens
    )

def _anthem_findcare_provider_lookup(
    name: str,
    npi: str,
    state: str,
    city: str,
    prefix: str,
) -> str:
    """Return ``listed``/``not_listed``/``unknown`` for one plan prefix.

    The NPI-based provider endpoint is authoritative and much faster than
    asking the search-box for suggestions first. A successful response with
    no matching provider is a definitive ``not_listed``; a transport error is
    ``unknown`` and is deliberately not written to the persistent cache.
    """
    name = " ".join(str(name or "").split())
    state = " ".join(str(state or "").split()).upper() or "CA"
    city = " ".join(str(city or "").split())
    if not name:
        return "unknown"
    latitude, longitude = _ANTHEM_STATE_COORDS.get(
        state, _ANTHEM_STATE_COORDS["CA"]
    )
    token = _anthem_findcare_token()
    if not token:
        return "unknown"
    npi = str(npi or "").strip()

    def attempt(query: str) -> bool | None:
        body = {
            "providerName": query,
            "latitude": latitude,
            "longitude": longitude,
            "postalCode": "",
            "city": city,
            "county": "",
            "state": state,
            "brandCode": "ABCBS",
            "locale": "en_US",
            "planStateCode": state,
            "alphaPrefix": prefix,
            "distance": "250",
        }
        try:
            response = requests.post(
                _ANTHEM_FINDCARE_BASE
                + "/precare/api/lookup/v1/provider",
                headers=_anthem_findcare_headers(token),
                json=body,
                timeout=(5, 25),
            )
            response.raise_for_status()
            providers = response.json().get("providers") or []
        except (requests.RequestException, ValueError):
            return None
        for provider in providers:
            if not isinstance(provider, dict):
                continue
            if npi and npi in str(provider.get("npiIds") or ""):
                return True
            provider_name = str(provider.get("providerName") or "")
            if _anthem_names_match(query, provider_name) or (
                _anthem_facility_names_match(query, provider_name)
            ):
                return True
        return False

    any_success = False
    for variant in _anthem_facility_name_variants(name):
        result = attempt(variant)
        if result is True:
            return "listed"
        if result is False:
            any_success = True
    return "not_listed" if any_success else "unknown"

def _anthem_findcare_prefix_status(
    name: str,
    state: str,
    prefix: str,
    npi: str = "",
    city: str = "",
) -> str:
    """Look up one provider for one plan prefix, using the persistent cache."""
    key = _anthem_status_cache_key(npi, name, state, prefix)
    cached = _anthem_status_cache_get(key)
    if cached:
        return cached
    status = _anthem_findcare_provider_lookup(
        name, npi, state, city, prefix
    )
    if status != "unknown":
        _anthem_status_cache_set(key, status)
    return status

def _anthem_strip_credentials(name: str) -> str:
    """Strip trailing medical credentials from a person's name."""
    credential_pattern = (
        r"(?:M\.?D\.?|D\.?O\.?|Ph\.?D\.?|MBBS|MPH|MBA|"
        r"MS|M\.S\.|BSN|NP|PA-C|RN|FACS|FACP|FACC|FAAP)"
    )
    return re.sub(
        rf"(?:,\s*{credential_pattern}\b.*"
        rf"|\s+{credential_pattern}\s*$)",
        "",
        " ".join(str(name or "").split()),
        flags=re.IGNORECASE,
    ).strip(" ,")

def _anthem_facility_name_variants(name: str) -> list[str]:
    """Return common hospital-name variants for Anthem's lookup."""
    name = " ".join(str(name or "").split())
    if not name:
        return []
    variants = [name]
    lowered = name.casefold()
    prefixes = (
        "the university of texas ",
        "university of texas ",
        "the university of ",
        "uthealth ",
        "ut health ",
        "ut ",
        "the ",
    )
    for prefix in prefixes:
        if lowered.startswith(prefix) and len(name) > len(prefix):
            candidate = name[len(prefix):].strip(" ,")
            if candidate and candidate not in variants:
                variants.append(candidate)
    for variant in list(variants):
        collapsed = re.sub(
            r"\bM\.?\s*D\.?(?![A-Za-z])",
            "MD",
            variant,
            flags=re.IGNORECASE,
        )
        collapsed = re.sub(r"\.", "", collapsed)
        collapsed = " ".join(collapsed.split())
        if collapsed and collapsed not in variants:
            variants.append(collapsed)
    facility_words = (
        "hospital", "center", "clinic", "medical", "health", "cancer",
        "institute", "university", "foundation", "group", "system",
    )
    clean_name = _anthem_strip_credentials(name)
    clean_words = clean_name.split()
    if 2 <= len(clean_words) <= 4 and not any(
        word.casefold().strip(".,") in facility_words
        for word in clean_words
    ):
        first, last = clean_words[0], clean_words[-1]
        middle = clean_words[1:-1]
        short = f"{first} {last}"
        if short not in variants:
            variants.append(short)
        if middle:
            initials = " ".join(
                word.strip(".,")[0].upper()
                for word in middle
                if word.strip(".,")
            )
            with_initial = f"{first} {initials} {last}"
            if with_initial not in variants:
                variants.append(with_initial)
    return variants

def _anthem_findcare_plan_status(
    name: str,
    state: str,
    prefixes: tuple[str, ...],
    cache: dict[tuple[str, str], str],
    lock: threading.Lock,
    npi: str = "",
    city: str = "",
) -> str:
    """Return ``listed``, ``not_listed``, or ``unknown`` for a plan group."""
    name = " ".join(str(name or "").split())
    state = " ".join(str(state or "CA").split()).upper() or "CA"
    if not name:
        return "unknown"
    key = (str(npi or "").strip() or name.casefold(), state)
    with lock:
        cached = cache.get(key)
    if cached:
        return cached
    query_name = _anthem_strip_credentials(name)
    any_not_listed = False
    status = "unknown"
    for prefix in prefixes:
        prefix_status = _anthem_findcare_prefix_status(
            query_name or name,
            state,
            prefix,
            npi=npi,
            city=city,
        )
        if prefix_status == "listed":
            status = "listed"
            break
        if prefix_status == "not_listed":
            any_not_listed = True
    if status != "listed" and any_not_listed:
        status = "not_listed"
    with lock:
        cache[key] = status
    return status

def _anthem_findcare_ppo_status(
    name: str,
    state: str,
    npi: str = "",
    city: str = "",
) -> str:
    """Return Anthem commercial PPO directory status for a provider."""
    return _anthem_findcare_plan_status(
        name,
        state,
        _anthem_findcare_ppo_prefixes(state),
        _ANTHEM_PPO_CACHE,
        _ANTHEM_PPO_LOCK,
        npi=npi,
        city=city,
    )

def _anthem_findcare_bluecard_status(
    name: str,
    state: str,
    npi: str = "",
    city: str = "",
) -> str:
    """Return Anthem BlueCard National PPO directory status for a provider."""
    return _anthem_findcare_plan_status(
        name,
        state,
        _anthem_findcare_bluecard_prefixes(state),
        _ANTHEM_BLUECARD_CACHE,
        _ANTHEM_BLUECARD_LOCK,
        npi=npi,
        city=city,
    )

def _anthem_item_state(item: dict) -> str:
    address = str(item.get("address") or "")
    match = re.search(r"\b([A-Z]{2})\s+\d{5}", address)
    return match.group(1) if match else ""

def _anthem_item_city(item: dict) -> str:
    address = str(item.get("address") or "")
    match = re.search(r"\b([A-Z]{2})\s+\d{5}", address)
    if not match:
        return ""
    prefix = address[: match.start()].strip(" ,")
    return prefix.split(",")[-1].strip()

def _anthem_annotate_member_status(
    items: list[dict],
    default_state: str = "",
    member_plan: dict | None = None,
    on_update: Callable[[dict], None] | None = None,
    institution: str = "",
    title_deadline_seconds: float = 8.0,
    title_limit: int = 24,
) -> None:
    """Add the stored member-plan network status to provider cards.

    Only the member plan is checked. Anthem PPO and BlueCard National PPO are
    deliberately not queried because their status is not needed in the result
    list. When ``on_update`` is supplied, it is called in the SSE handler
    thread as each provider finishes, allowing the browser to render the list
    before all member-plan checks have completed.

    Academic titles are enriched in the same worker pool, but only for the
    first ``title_limit`` cards and only until ``title_deadline_seconds``
    elapses. This keeps the result list fast: cards are already on screen, and
    title updates are extra rather than a prerequisite for rendering.
    """
    if not items:
        return
    if member_plan is None:
        member_plan = _anthem_member_plan()
    member_prefix = str(member_plan.get("prefix") or "").strip()
    institution = " ".join(str(institution or "").split())
    title_supported = bool(
        institution and _anthem_facility_profile_supported(institution)
    )
    title_deadline = time.monotonic() + max(0.0, title_deadline_seconds)

    def finish(
        item: dict,
        member_status: str,
        academic_title: str = "",
    ) -> dict:
        # Drop any PPO/BlueCard data left over from an earlier response so a
        # card only ever shows the member-plan result.
        for key in (
            "anthem_ppo",
            "anthem_ppo_label",
            "anthem_ppo_flag",
            "anthem_bluecard_national",
            "anthem_bluecard_national_label",
            "anthem_bluecard_national_flag",
        ):
            item.pop(key, None)
        member_label = ""
        if member_prefix:
            member_label = _anthem_member_plan_label(member_status)
            item["anthem_member_plan"] = member_status
            item["anthem_member_plan_label"] = member_label
            item["anthem_member_plan_flag"] = _ANTHEM_MEMBER_PLAN_FLAGS[
                member_status
            ]
            item["anthem_member_plan_name"] = str(
                member_plan.get("plan_name") or ""
            )
        title = academic_title or str(
            item.get("academic_title") or ""
        ).strip()
        if title:
            item["academic_title"] = title
        summary = str(item.get("summary") or "").strip()
        for value in (
            f"Academic title: {title}" if title else "",
            member_label,
        ):
            if value and value not in summary:
                summary = f"{summary} · {value}" if summary else value
        item["summary"] = summary
        item["paragraphs"] = [summary] if summary else []
        item["anthem_status_pending"] = False
        return item

    def annotate(index: int, item: dict) -> dict:
        name = str(item.get("name") or item.get("title") or "").strip()
        state = (
            _anthem_item_state(item)
            or str(member_plan.get("state") or "")
            or str(default_state or "")
        )
        city = _anthem_item_city(item)
        npi = str(item.get("npi") or "").strip()
        member_status = "unknown"
        if member_prefix:
            member_status = _anthem_findcare_member_status(
                name, state, npi=npi, city=city
            )
        academic_title = ""
        if (
            title_supported
            and index < title_limit
            and not str(item.get("academic_title") or "").strip()
        ):
            academic_title = _anthem_academic_title(
                name, institution, deadline=title_deadline
            )
        return finish(item, member_status, academic_title)

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = {
            pool.submit(annotate, index, item): item
            for index, item in enumerate(items)
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                result = future.result()
            except Exception:
                result = finish(item, "unknown")
            if on_update is not None:
                on_update(result)

def _anthem_directory_condition_term(
    question: str,
    plan: dict[str, str],
    facility: str,
) -> str:
    """Return the condition phrase a provider question is about.

    Examples: ``lung cancer``, ``head and neck cancer``, or
    ``heart failure``. Generic query words and facility names are skipped
    so they do not become part of the directory search term.
    """
    text = " ".join(str(question or "").casefold().split())
    if not text:
        return ""
    facility_tokens = set(
        re.findall(r"[a-z0-9]+", str(facility or "").casefold())
    )
    match = _ANTHEM_CONDITION_PATTERN.search(text)
    if not match:
        return ""
    words = [
        word
        for word in match.group(1).split()
        if (
            word not in facility_tokens
            and word not in _ANTHEM_DIRECTORY_STOPWORDS
            and len(word) > 1
        )
    ]
    while words and words[0] in _ANTHEM_CONDITION_ANCHORS:
        words.pop(0)
    synonyms = {
        "neoplasm": "cancer",
        "tumor": "cancer",
        "tumour": "cancer",
    }
    words = [synonyms.get(word, word) for word in words]
    condition = " ".join(dict.fromkeys(words))
    if condition:
        return condition
    fallback = match.group(1).split()[-1]
    return synonyms.get(fallback, fallback)

_ANTHEM_CONDITION_ALIASES = {
    "bone": {"bone", "orthopedic", "orthopaedic", "musculoskeletal"},
    "brain": {"brain", "neuro", "neurologic", "neurosurgery"},
    "breast": {"breast", "mammary"},
    "cancer": {
        "cancer",
        "oncology",
        "carcinoma",
        "tumor",
        "tumour",
        "neoplasm",
        "sarcoma",
        "lymphoma",
        "leukemia",
        "melanoma",
        "thoracic",
        "cardiothoracic",
        "surgical oncology",
    },
    "head": {"head"},
    "heart": {"heart", "cardiac", "cardio", "cardiovascular"},
    "kidney": {"kidney", "renal"},
    "liver": {"liver", "hepatic"},
    "lung": {
        "lung",
        "pulmonary",
        "thoracic",
        "cardiothoracic",
        "respiratory",
    },
    "neck": {"neck"},
}

_ANTHEM_MODALITY_ALIASES = {
    "chemotherapy": ("chemo", "medical oncology"),
    "radiation": ("radi", "radiation"),
    "surgery": ("surg",),
}

def _anthem_condition_tokens(term: str) -> list[str]:
    """Split a directory condition phrase into filterable tokens."""
    return [
        token
        for token in re.findall(r"[a-z0-9]+", str(term or "").casefold())
        if token not in _ANTHEM_DIRECTORY_STOPWORDS
    ]

def _anthem_requested_modalities(question: str) -> list[tuple[str, ...]]:
    """Return treatment modalities explicitly requested in a question."""
    text = " ".join(str(question or "").casefold().split())
    groups: list[tuple[str, ...]] = []
    if re.search(
        r"\b(?:surgery|surgical|surgeon|operation|procedure)\b", text
    ):
        groups.append(_ANTHEM_MODALITY_ALIASES["surgery"])
    if re.search(r"\b(?:radiation|radiotherapy|radiosurgery)\b", text):
        groups.append(_ANTHEM_MODALITY_ALIASES["radiation"])
    if re.search(r"\b(?:chemotherapy|chemo)\b", text):
        groups.append(_ANTHEM_MODALITY_ALIASES["chemotherapy"])
    return groups

def _anthem_provider_matches_condition(
    item: dict,
    condition_tokens: list[str],
    modalities: list[tuple[str, ...]],
) -> bool:
    """Return whether a provider matches the condition and treatment.

    Condition words are expanded through small synonym groups, so a
    lung-cancer question also accepts thoracic/cardiothoracic surgery, and a
    surgery question requires a surgical specialty.
    """
    text = " ".join(
        str(item.get(key) or "")
        for key in ("specialty", "section")
    ).casefold()
    if not text.strip():
        # Without a specialty there is nothing reliable to filter on.
        return True
    for token in condition_tokens:
        aliases = _ANTHEM_CONDITION_ALIASES.get(token, {token})
        if not any(alias in text for alias in aliases):
            return False
    for group in modalities:
        if not any(alias in text for alias in group):
            return False
    return True

def _anthem_directory_condition_items(
    question: str,
    plan: dict[str, str],
    facility: str,
    source_label: str,
) -> tuple[list[dict], str]:
    """Supplement NPPES results with the facility's own provider directory.

    NPPES is a registration database, so a specialist can be listed under a
    stale out-of-state address or a broader taxonomy. Searching the UCSF
    Health directory by the condition in the question finds current
    specialists such as the thoracic surgical oncologists who treat lung
    cancer. The supplement is deduplicated by NPI against the NPPES list.
    """
    if not _anthem_is_ucsf_facility(facility):
        return [], ""
    if plan.get("doctor_name"):
        return [], ""
    term = _anthem_directory_condition_term(
        question, plan, facility
    )
    if not term:
        return [], ""
    results = _anthem_ucsf_directory_query(term, limit=50)
    condition_tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", term)
        if token not in _ANTHEM_DIRECTORY_STOPWORDS
    ]
    selected: list[dict] = []
    for result in results:
        if (
            str(result.get("providerType") or "").casefold()
            != "physician"
        ):
            continue
        specialty_text = " ".join(
            str(value)
            for value in result.get("specialties") or []
        ).casefold()
        if not all(
            token in specialty_text for token in condition_tokens
        ):
            continue
        selected.append(result)
        if len(selected) >= 8:
            break
    return (
        _anthem_ucsf_directory_items(
            selected, facility, source_label
        ),
        term,
    )

_ANTHEM_FINDCARE_SUMMARY_PATH = "/fad/api/provider/summary"

_ANTHEM_FINDCARE_DETAIL_PATH = "/precare/api/provider/public/v2/detail"

def _anthem_findcare_output_flags() -> dict[str, object]:
    """Return the output flags the Find Care public APIs expect."""
    return {
        "includeTaxonomy": True,
        "includeVisibilityRules": True,
        "improveSearchCriteria": True,
        "includeDisclaimerRules": True,
        "includeReviewRatings": True,
        "imposeAddressConsolidation": True,
        "includeSpfInfo": True,
        "includeProviderNetworks": True,
        "includePrimeGroupSearch": False,
        "includeHealthSystemSearch": False,
        "inFcrMode": False,
        "includeAllNetworks": False,
    }

def _anthem_findcare_location(
    plan: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return the location object used by Find Care public APIs."""
    plan = plan or {}
    member_plan = _anthem_member_plan()
    city, state, postal = _anthem_parse_location(
        str(plan.get("location") or "")
    )
    state = (
        state
        or str(member_plan.get("state") or "").strip().upper()
        or "CA"
    )
    latitude, longitude = _ANTHEM_STATE_COORDS.get(
        state, _ANTHEM_STATE_COORDS["CA"]
    )
    location = {
        "distance": "50",
        "latitude": latitude,
        "longitude": longitude,
    }
    if city:
        location["city"] = city
    location["state"] = state
    if postal:
        location["postalCode"] = postal
    return location

def _anthem_findcare_plan_choice(
    state: str,
    member_plan: dict | None = None,
) -> dict[str, object]:
    """Pick a public plan/network for the Find Care provider APIs.

    The public endpoints need an alpha prefix and plan metadata even when
    the question is not about a specific plan. Prefer the prefix from the
    stored member plan, then a commercial PPO, then Anthem's documented
    fallback prefix.
    """
    member_plan = member_plan or _anthem_member_plan()
    prefix = str(member_plan.get("prefix") or "").strip()
    plans = _anthem_findcare_plans(state)
    chosen: dict | None = None
    if prefix:
        chosen = next(
            (
                plan
                for plan in plans
                if str(plan.get("prefix") or "").strip() == prefix
            ),
            None,
        )
    if chosen is None:
        chosen = next(
            (
                plan
                for plan in plans
                if str(plan.get("careType") or "").upper()
                == "MEDICAL"
                and "PPO" in str(plan.get("planType") or "").upper()
            ),
            None,
        )
    if chosen is None:
        chosen = next(
            (
                plan
                for plan in plans
                if str(plan.get("careType") or "").upper()
                == "MEDICAL"
            ),
            None,
        )
    categories = []
    plan_types = []
    plan_entries = []
    if isinstance(chosen, dict):
        categories = [
            str(value).strip()
            for value in chosen.get("planCategoryList") or []
            if str(value).strip()
        ]
        plan_type = str(chosen.get("planType") or "").strip()
        if plan_type:
            plan_types.append(plan_type)
        identifier = str(chosen.get("id") or "").strip()
        if identifier:
            plan_entries.append(
                {
                    "identifier": identifier,
                    "isNationalPlan": bool(
                        chosen.get("isNationalPlan")
                    ),
                }
            )
        prefix = str(chosen.get("prefix") or "").strip() or prefix
    if not prefix:
        prefix = _ANTHEM_PPO_PREFIX_FALLBACK[0]
    return {
        "category": categories[0] if categories else "EMP",
        "types": plan_types or ["PPOGRP"],
        "plans": plan_entries,
        "prefix": prefix,
    }

def _anthem_findcare_summary_request(
    plan: dict[str, str],
) -> dict[str, object]:
    """Build the body for ``/fad/api/provider/summary``.

    Retained for reference only. The doctor-list flow no longer calls this
    endpoint because it did not return usable provider records.
    """
    member_plan = _anthem_member_plan()
    location = _anthem_findcare_location(plan)
    state = str(location.get("state") or "CA")
    plan_choice = _anthem_findcare_plan_choice(state, member_plan)
    demographic_filter: dict[str, object] = {
        "specialtyCategoryCodes": [],
        "typeCodes": ["P"],
        "selectedTypeCodes": [],
    }
    search_text = " ".join(
        str(
            plan.get("doctor_name")
            or plan.get("specialty")
            or plan.get("facility")
            or plan.get("keywords")
            or ""
        ).split()
    )
    if search_text:
        demographic_filter["providerName"] = search_text
    facility = " ".join(str(plan.get("facility") or "").split())
    if facility:
        demographic_filter["hospitalAffiliation"] = facility
    return {
        "alphaPrefix": str(plan_choice.get("prefix") or ""),
        "isFuturePlan": "false",
        "locale": "en_US",
        "location": location,
        "memberCriteria": {},
        "outputFlags": _anthem_findcare_output_flags(),
        "pageNumber": 1,
        "pageSize": 50,
        "plan": {
            "category": plan_choice.get("category"),
            "stateCode": state,
            "types": plan_choice.get("types") or [],
            "plans": plan_choice.get("plans") or [],
        },
        "searchCriteria": {
            "miscFilter": {},
            "demographicFilter": demographic_filter,
            "brandCode": "ABCBS",
            "isInitialSearch": True,
        },
    }

def _anthem_findcare_text(value: object) -> str:
    """Flatten a scalar-or-list API field into display text."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        return "; ".join(
            part for part in (_anthem_findcare_text(item) for item in value) if part
        )
    if isinstance(value, dict):
        for key in ("name", "code", "description", "value"):
            text = _anthem_findcare_text(value.get(key))
            if text:
                return text
        return ""
    return " ".join(str(value).split())

def _anthem_findcare_address(address: object) -> str:
    """Format an Anthem address object as one line."""
    if not isinstance(address, dict):
        return _anthem_findcare_text(address)
    parts = [
        _anthem_findcare_text(address.get("addressLineOne")),
        _anthem_findcare_text(address.get("addressLineTwo")),
        _anthem_findcare_text(address.get("addressOne")),
        _anthem_findcare_text(address.get("addressTwo")),
    ]
    city = _anthem_findcare_text(address.get("city"))
    state = _anthem_findcare_text(
        address.get("stateCd")
        or address.get("state")
        or address.get("stateCode")
    )
    postal = _anthem_findcare_text(
        address.get("zipCd") or address.get("postalCode")
    )
    locality = " ".join(part for part in (city, state) if part)
    if postal:
        locality = f"{locality} {postal}".strip()
    if locality:
        parts.append(locality)
    return ", ".join(
        dict.fromkeys(part for part in parts if part)
    )

def _anthem_findcare_provider_record(provider: object) -> dict[str, str]:
    """Normalize one Find Care summary provider record."""
    if not isinstance(provider, dict):
        return {}
    location = provider.get("location")
    if not isinstance(location, dict):
        location = {}
    address = location.get("address")
    if not isinstance(address, dict):
        address = provider.get("address")
    if not isinstance(address, dict):
        address = {}
    name = (
        _anthem_findcare_text(provider.get("providerName"))
        or _anthem_findcare_text(provider.get("fullNm"))
        or " ".join(
            part
            for part in (
                _anthem_findcare_text(provider.get("firstName")),
                _anthem_findcare_text(provider.get("middleName")),
                _anthem_findcare_text(provider.get("lastName")),
            )
            if part
        )
        or _anthem_findcare_text(provider.get("doingBusinessAs"))
    )
    specialty = (
        _anthem_findcare_text(provider.get("specialty"))
        or _anthem_findcare_text(provider.get("specialties"))
        or _anthem_findcare_text(provider.get("taxonomies"))
    )
    organization = (
        _anthem_findcare_text(provider.get("organizationName"))
        or _anthem_findcare_text(provider.get("organization"))
        or _anthem_findcare_text(location.get("affiliationName"))
        or _anthem_findcare_text(location.get("organizationName"))
    )
    npi = (
        _anthem_findcare_text(location.get("npiIds"))
        or _anthem_findcare_text(provider.get("npi"))
    )
    phone = (
        _anthem_findcare_text(address.get("phone"))
        or _anthem_findcare_text(address.get("phoneNumber"))
        or _anthem_findcare_text(location.get("phone"))
    )
    provider_id = (
        _anthem_findcare_text(provider.get("providerIdentifier"))
        or _anthem_findcare_text(provider.get("providerId"))
        or _anthem_findcare_text(provider.get("id"))
    )
    address_id = (
        _anthem_findcare_text(address.get("addressId"))
        or _anthem_findcare_text(provider.get("addressId"))
    )
    return {
        "name": name,
        "npi": npi,
        "specialty": specialty,
        "organization": organization,
        "address": _anthem_findcare_address(address),
        "phone": phone,
        "provider_id": provider_id,
        "address_id": address_id,
    }

def _anthem_findcare_summary_context(
    data: dict,
) -> tuple[str, int]:
    """Turn a Find Care summary response into provider blocks."""
    providers = (
        data.get("providerList")
        or data.get("providers")
        or data.get("data")
        or []
    )
    if isinstance(providers, dict):
        providers = (
            providers.get("providerList")
            or providers.get("providers")
            or providers.get("data")
            or []
        )
    if not isinstance(providers, list):
        providers = []
    blocks: list[str] = []
    seen: set[tuple[str, str]] = set()
    for provider in providers:
        record = _anthem_findcare_provider_record(provider)
        if not record.get("name"):
            continue
        identity = (
            record.get("provider_id") or record["name"].casefold(),
            record.get("address", "").casefold(),
        )
        if identity in seen:
            continue
        seen.add(identity)
        lines = [f"Provider: {record['name']}"]
        for label, key in (
            ("NPI", "npi"),
            ("Specialty", "specialty"),
            ("Organization", "organization"),
            ("Address", "address"),
            ("Phone/contact", "phone"),
            ("Anthem provider id", "provider_id"),
            ("Anthem address id", "address_id"),
        ):
            if record.get(key):
                lines.append(f"{label}: {record[key]}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)[:ANTHEM_CONTEXT_CHARS], len(blocks)

def _anthem_findcare_summary(
    plan: dict[str, str],
    log: Callable[[str], None],
) -> tuple[str, int]:
    """Retained but unused: fetch the Find Care summary response.

    The doctor-list flow no longer calls this API because it did not return
    usable provider records.
    """
    token = _anthem_findcare_token()
    if not token:
        raise AnthemFHIRError("Find Care token was unavailable")
    body = _anthem_findcare_summary_request(plan)
    response = requests.post(
        _ANTHEM_FINDCARE_BASE + _ANTHEM_FINDCARE_SUMMARY_PATH,
        headers=_anthem_findcare_headers(token),
        json=body,
        timeout=(5, 30),
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise AnthemFHIRError("Find Care summary returned no JSON object")
    context, count = _anthem_findcare_summary_context(data)
    if count:
        log(
            "[anthem: Find Care summary API returned "
            f"{count} provider record(s)]"
        )
    return context, count

def _anthem_findcare_detail_request(
    provider_id: str,
    address_id: str = "",
) -> dict[str, object]:
    """Build the body for the public Anthem provider-detail API."""
    member_plan = _anthem_member_plan()
    location = _anthem_findcare_location({})
    state = str(location.get("state") or "CA")
    plan_choice = _anthem_findcare_plan_choice(state, member_plan)
    request: dict[str, object] = {
        "id": str(provider_id or "").strip(),
        "addressId": str(address_id or "").strip(),
        "distance": 50,
        "planStateCode": state,
        "networks": [],
        "alphaPrefix": str(plan_choice.get("prefix") or ""),
        "latitude": str(location.get("latitude") or ""),
        "longitude": str(location.get("longitude") or ""),
        "outputFlags": _anthem_findcare_output_flags(),
        "brandCode": "ABCBS",
        "locale": "en_US",
        "memberCriteria": {},
        "typeCodes": ["P"],
        "plans": plan_choice.get("plans") or [],
    }
    for key in ("city", "postalCode"):
        if location.get(key):
            request[key] = location[key]
    return request

def _anthem_findcare_provider_detail(
    provider_id: str,
    address_id: str = "",
) -> dict:
    """Fetch one provider profile from the public Find Care detail API."""
    if not str(provider_id or "").strip():
        return {}
    token = _anthem_findcare_token()
    if not token:
        return {}
    response = requests.post(
        _ANTHEM_FINDCARE_BASE + _ANTHEM_FINDCARE_DETAIL_PATH,
        headers=_anthem_findcare_headers(token),
        json=_anthem_findcare_detail_request(provider_id, address_id),
        timeout=(5, 30),
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        return {}
    detail = data.get("providerDetail") or data.get("detail") or data
    return detail if isinstance(detail, dict) else {}

def _anthem_findcare_detail_facts(detail: dict) -> tuple[str, str, str]:
    """Return ``(name, npi, facts)`` from a Find Care detail response."""
    record = _anthem_findcare_provider_record(detail)
    name = record.get("name") or "Provider"
    npi = record.get("npi", "")
    location = detail.get("location")
    if not isinstance(location, dict):
        location = {}
    address = location.get("address")
    if not isinstance(address, dict):
        address = {}
    lines = [f"Provider: {name}"]
    if npi:
        lines.append(f"NPI: {npi}")
    specialty = (
        _anthem_findcare_text(detail.get("specialty"))
        or record.get("specialty", "")
    )
    if specialty:
        lines.append(f"Specialty: {specialty}")
    organization = (
        _anthem_findcare_text(detail.get("organizationName"))
        or record.get("organization", "")
    )
    if organization:
        lines.append(f"Organization: {organization}")
    address_text = _anthem_findcare_address(address)
    if address_text:
        lines.append(f"Address: {address_text}")
    phone = (
        _anthem_findcare_text(address.get("clinicalContactPhone"))
        or _anthem_findcare_text(address.get("phone"))
        or record.get("phone", "")
    )
    if phone:
        lines.append(f"Phone/contact: {phone}")
    gender = _anthem_findcare_text(detail.get("gender"))
    if gender:
        lines.append(f"Gender: {gender}")
    languages = _anthem_findcare_text(location.get("languages"))
    if languages:
        lines.append(f"Languages: {languages}")
    networks = _anthem_findcare_text(
        location.get("participatingNetworks")
    )
    if networks:
        lines.append(f"Network: {networks}")
    educations = detail.get("educations")
    if educations:
        lines.append(
            "Education & Training: "
            + _anthem_findcare_text(educations)
        )
    experiences = (
        detail.get("areaOfExpertises")
        or detail.get("areasOfExpertise")
    )
    if experiences:
        lines.append(
            "Areas of Expertise: "
            + _anthem_findcare_text(experiences)
        )
    licenses = detail.get("licenseTypes") or detail.get("licenseNumbers")
    if licenses:
        lines.append("Licenses: " + _anthem_findcare_text(licenses))
    biography = _anthem_findcare_text(detail.get("biography"))
    if biography:
        lines.append(f"Biography: {biography}")
    return name, npi, "\n".join(lines)

def _anthem_lookup(
    plan: dict[str, str],
    log: Callable[[str], None],
    on_batch: Callable[[str], None] | None = None,
    condition_tokens: list[str] | None = None,
    modalities: list[tuple[str, ...]] | None = None,
    max_results: int = 0,
) -> tuple[str, str, list[str], int]:
    """Return providers from the CMS NPPES registry.

    The Anthem Find Care provider summary API and the Anthem Provider
    Directory FHIR API are deliberately not called: neither returned usable
    provider records. The list flow now uses the public CMS NPPES registry.
    When ``on_batch`` is supplied, result batches are streamed as soon as
    they are verified.

    Returns ``(context, source_label, errors, record_count)``.
    """
    errors: list[str] = []
    nppes_context, nppes_count = _anthem_nppes_search(
        plan,
        log,
        on_batch=on_batch,
        condition_tokens=condition_tokens,
        modalities=modalities,
        max_results=max_results,
    )
    if nppes_context.strip():
        return (
            nppes_context,
            "CMS NPPES registry",
            errors,
            nppes_count,
        )
    errors.append("CMS NPPES registry: no matching providers")
    return "", "", errors, 0
