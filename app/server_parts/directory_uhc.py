"""UnitedHealthcare provider-directory search and profiles."""

from __future__ import annotations

from .core import *

_UHC_ROLE_INCLUDES = [
    ("_include", "PractitionerRole:practitioner"),
    ("_include", "PractitionerRole:location"),
    ("_include", "PractitionerRole:organization"),
    ("_include", "PractitionerRole:network"),
]

_UHC_QUESTION_CACHE: dict[str, dict[str, str]] = {}

_UHC_QUESTION_LOCK = threading.Lock()

def _uhc_fhir_get(
    resource: str,
    params: list[tuple[str, str]] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict:
    """Fetch one UnitedHealthcare public Plan-Net FHIR response."""
    return _uhc_fhir_url(
        f"{UHC_FHIR_BASE}/{resource}", params or [], log
    )

def _uhc_fhir_url(
    url: str,
    params: list[tuple[str, str]] | None = None,
    log: Callable[[str], None] | None = None,
) -> dict:
    """Fetch one absolute Plan-Net FHIR URL."""
    try:
        response = requests.get(
            url,
            params=params or [],
            headers={
                "Accept": "application/fhir+json",
                "User-Agent": "local-llm-chat-uhc/1.0",
            },
            timeout=(5, 30),
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        if log is not None:
            log(f"[uhc: FHIR request failed: {exc}]")
        return {}
    return data if isinstance(data, dict) else {}

def _uhc_next_link(bundle: dict) -> str:
    """Return a FHIR Bundle's next-page URL, if present."""
    for link in bundle.get("link") or []:
        if not isinstance(link, dict):
            continue
        if link.get("relation") == "next" and link.get("url"):
            return str(link.get("url")).strip()
    return ""

def _uhc_bundle_index(
    bundle: dict,
) -> tuple[dict[str, list[dict]], dict[tuple[str, str], dict]]:
    """Index a FHIR Bundle's resources by type and by ResourceType/id."""
    by_type: dict[str, list[dict]] = {}
    by_id: dict[tuple[str, str], dict] = {}
    for resource in _fhir_bundle_resources(bundle):
        resource_type = str(resource.get("resourceType") or "").strip()
        resource_id = str(resource.get("id") or "").strip()
        if not resource_type:
            continue
        by_type.setdefault(resource_type, []).append(resource)
        if resource_id:
            by_id[(resource_type, resource_id)] = resource
    return by_type, by_id

def _uhc_ref_id(value) -> str:
    """Return the id part of a FHIR reference."""
    key = _fhir_reference_id(value)
    return key.split("/", 1)[1] if "/" in key else ""

def _uhc_practitioner_npi(practitioner: dict) -> str:
    """Return a Practitioner's NPI identifier."""
    for identifier in practitioner.get("identifier") or []:
        if not isinstance(identifier, dict):
            continue
        value = str(identifier.get("value") or "").strip()
        system = str(identifier.get("system") or "").lower()
        if value and (
            system.endswith("us-npi")
            or re.fullmatch(r"\d{10}", value)
        ):
            return value
    return ""

def _uhc_practitioner_specialties(practitioner: dict) -> str:
    """Return a Practitioner's qualification specialties."""
    labels: list[str] = []
    for qualification in practitioner.get("qualification") or []:
        if not isinstance(qualification, dict):
            continue
        for label in _fhir_codeables([qualification.get("code")]):
            if label and label not in labels:
                labels.append(label)
    return "; ".join(labels)

def _uhc_role_specialties(role: dict) -> str:
    """Return the specialty labels on one PractitionerRole."""
    labels: list[str] = []
    for label in _fhir_codeables(role.get("specialty")):
        if label and label not in labels:
            labels.append(label)
    return "; ".join(labels)

def _uhc_role_network_ids(role: dict) -> list[str]:
    """Return Organization ids referenced as network participation."""
    ids: list[str] = []
    for extension in role.get("extension") or []:
        if not isinstance(extension, dict):
            continue
        if "network-reference" not in str(extension.get("url") or ""):
            continue
        network_id = _uhc_ref_id(extension.get("valueReference"))
        if network_id and network_id not in ids:
            ids.append(network_id)
    networks = role.get("network") or []
    if isinstance(networks, dict):
        networks = [networks]
    for reference in networks:
        network_id = _uhc_ref_id(reference)
        if network_id and network_id not in ids:
            ids.append(network_id)
    return ids

def _uhc_role_networks(
    role: dict,
    by_id: dict[tuple[str, str], dict],
) -> str:
    """Return readable network names for one PractitionerRole."""
    labels: list[str] = []
    for network_id in _uhc_role_network_ids(role):
        organization = by_id.get(("Organization", network_id)) or {}
        label = (
            str(organization.get("name") or "").strip()
            or network_id
        )
        if label and label not in labels:
            labels.append(label)
    return "; ".join(labels)

def _uhc_role_locations(
    role: dict,
    by_id: dict[tuple[str, str], dict],
) -> tuple[str, str, str]:
    """Return (location id, address, phone) for the first role location."""
    locations = role.get("location") or []
    if isinstance(locations, dict):
        locations = [locations]
    for reference in locations:
        location_id = _uhc_ref_id(reference)
        location = by_id.get(("Location", location_id)) or {}
        address = _fhir_address(location.get("address"))
        phone = _fhir_telecom(location.get("telecom"))
        if address or phone:
            return location_id, address, phone
    return "", "", ""

def _uhc_role_facility(
    role: dict,
    by_type: dict[str, list[dict]],
    by_id: dict[tuple[str, str], dict],
    network_ids: list[str],
    preferred_org_id: str = "",
) -> tuple[str, str]:
    """Return (organization id, name) for the role's practice facility."""
    if preferred_org_id:
        organization = by_id.get(("Organization", preferred_org_id))
        if organization:
            return preferred_org_id, str(
                organization.get("name") or ""
            ).strip()
    organization_id = _uhc_ref_id(role.get("organization"))
    if organization_id:
        organization = by_id.get(("Organization", organization_id)) or {}
        return organization_id, str(organization.get("name") or "").strip()
    network_set = set(network_ids)
    for organization in by_type.get("Organization") or []:
        candidate_id = str(organization.get("id") or "").strip()
        if candidate_id and candidate_id not in network_set:
            return candidate_id, str(
                organization.get("name") or ""
            ).strip()
    return "", ""

def _uhc_role_accepting(role: dict) -> str:
    """Return the Plan-Net accepting-patients label."""
    for extension in role.get("extension") or []:
        if not isinstance(extension, dict):
            continue
        if "newpatients" not in str(extension.get("url") or ""):
            continue
        for child in extension.get("extension") or []:
            if not isinstance(child, dict):
                continue
            concept = child.get("valueCodeableConcept") or {}
            text = str(concept.get("text") or "").strip()
            if text:
                return text
            labels = _fhir_codeables([concept])
            if labels:
                return labels[0]
    return ""

def _uhc_card_from_role(
    role: dict,
    by_type: dict[str, list[dict]],
    by_id: dict[tuple[str, str], dict],
    preferred_org_id: str = "",
) -> dict | None:
    """Convert one PractitionerRole and its includes into a provider card."""
    practitioner_id = _uhc_ref_id(role.get("practitioner"))
    practitioner = by_id.get(("Practitioner", practitioner_id)) or {}
    name = _fhir_human_name(practitioner) or _fhir_reference_display(
        role.get("practitioner")
    )
    if not name:
        return None
    network_ids = _uhc_role_network_ids(role)
    specialty = _uhc_role_specialties(role) or _uhc_practitioner_specialties(
        practitioner
    )
    location_id, address, location_phone = _uhc_role_locations(
        role, by_id
    )
    organization_id, organization = _uhc_role_facility(
        role,
        by_type,
        by_id,
        network_ids,
        preferred_org_id=preferred_org_id,
    )
    phone = _fhir_telecom(role.get("telecom")) or location_phone
    network_labels = [
        label
        for label in _uhc_role_networks(role, by_id).split("; ")
        if label
    ]
    networks = "; ".join(network_labels[:4])
    if len(network_labels) > 4:
        networks += " …"
    accepting = _uhc_role_accepting(role)
    summary = " · ".join(
        part
        for part in (specialty, organization, address, phone, networks)
        if part
    )
    link = f"{UHC_FHIR_BASE}/Practitioner/{practitioner_id}"
    return {
        "title": name,
        "name": name,
        "npi": _uhc_practitioner_npi(practitioner),
        "practitioner_id": practitioner_id,
        "role_id": str(role.get("id") or ""),
        "location_id": location_id,
        "organization_id": organization_id,
        "organization": organization,
        "specialty": specialty,
        "address": address,
        "phone": phone,
        "network": networks,
        "accepting": accepting,
        "link": link,
        "source": "UnitedHealthcare Provider Directory",
        "section": specialty,
        "summary": summary,
        "paragraphs": [summary] if summary else [],
        "docs": True,
        "docs_label": "UHC",
        "learnable": True,
        "learn_noun": "doctor",
        "findable": False,
        "article_api": "/api/uhc_provider",
        "complete": True,
    }

def _uhc_practitioner_candidates(
    plan: dict[str, str],
    log: Callable[[str], None] | None = None,
) -> list[dict]:
    """Return UHC Practitioner candidates matching an NPI or name."""
    npi = str(plan.get("npi") or "").strip()
    name = " ".join(str(plan.get("doctor_name") or "").split())
    candidates: list[dict] = []
    seen: set[str] = set()

    def add(practitioner: dict) -> None:
        practitioner_id = str(practitioner.get("id") or "")
        if practitioner_id and practitioner_id in seen:
            return
        if practitioner_id:
            seen.add(practitioner_id)
        candidates.append(practitioner)

    if npi:
        bundle = _uhc_fhir_get(
            "Practitioner",
            [
                (
                    "identifier",
                    f"http://hl7.org/fhir/sid/us-npi|{npi}",
                ),
                ("_count", "10"),
            ],
            log,
        )
        for practitioner in _fhir_bundle_resources(bundle):
            if _uhc_practitioner_npi(practitioner) == npi:
                add(practitioner)
    if name:
        first_name, last_name = _anthem_split_name(name)
        attempts: list[list[tuple[str, str]]] = [
            [("name", name), ("_count", "20")]
        ]
        if first_name or last_name:
            params: list[tuple[str, str]] = [("_count", "20")]
            if last_name:
                params.append(("family", last_name))
            if first_name:
                params.append(("given", first_name.split()[0]))
            attempts.append(params)
        if last_name:
            attempts.append([("name", last_name), ("_count", "50")])
        for params in attempts:
            bundle = _uhc_fhir_get("Practitioner", params, log)
            for practitioner in _fhir_bundle_resources(bundle):
                candidate = _fhir_human_name(practitioner)
                if candidate and _anthem_names_match(name, candidate):
                    add(practitioner)
    return candidates

def _uhc_find_practitioner(
    plan: dict[str, str],
    log: Callable[[str], None] | None = None,
) -> dict | None:
    """Find one UHC Practitioner by NPI, then by full/name family parts."""
    candidates = _uhc_practitioner_candidates(plan, log)
    return candidates[0] if candidates else None

def _uhc_find_practitioner_with_roles(
    plan: dict[str, str],
    log: Callable[[str], None] | None = None,
) -> tuple[dict | None, dict]:
    """Return the Practitioner record with the richest role information.

    The public directory can contain more than one Practitioner resource for
    the same NPI. One copy may have only a single sparse role while another
    carries many locations and networks, so rank candidates by how much
    PractitionerRole data they return.
    """
    candidates = _uhc_practitioner_candidates(plan, log)
    best_practitioner: dict | None = None
    best_roles: dict = {}
    best_score = -1
    for practitioner in candidates[:5]:
        roles = _uhc_roles_for_practitioner(
            str(practitioner.get("id") or ""), log
        )
        role_resources = [
            resource
            for resource in _fhir_bundle_resources(roles)
            if resource.get("resourceType") == "PractitionerRole"
        ]
        score = len(role_resources) * 10 + sum(
            len(_uhc_role_network_ids(role))
            for role in role_resources
        )
        if score > best_score:
            best_score = score
            best_practitioner = practitioner
            best_roles = roles
    if best_practitioner is not None:
        return best_practitioner, best_roles
    if candidates:
        return candidates[0], {}
    return None, {}

def _uhc_roles_for_practitioner(
    practitioner_id: str,
    log: Callable[[str], None] | None = None,
) -> dict:
    """Fetch all PractitionerRoles and included resources for one provider."""
    if not practitioner_id:
        return {}
    return _uhc_fhir_get(
        "PractitionerRole",
        [
            ("practitioner", f"Practitioner/{practitioner_id}"),
            ("_count", "50"),
            *_UHC_ROLE_INCLUDES,
        ],
        log,
    )

def _uhc_find_organization(
    facility: str,
    log: Callable[[str], None] | None = None,
) -> dict | None:
    """Resolve a hospital/clinic name to one UHC Organization."""
    facility = " ".join(str(facility or "").split())
    if not facility:
        return None
    variants = _anthem_facility_name_variants(facility) or [facility]
    for variant in variants:
        bundle = _uhc_fhir_get(
            "Organization",
            [("name", variant), ("_count", "20")],
            log,
        )
        organizations = [
            organization
            for organization in _fhir_bundle_resources(bundle)
            if organization.get("resourceType") == "Organization"
        ]
        if not organizations:
            continue
        wanted = facility.casefold()
        for organization in organizations:
            name = str(organization.get("name") or "").strip()
            if name.casefold() == wanted:
                return organization
        for organization in organizations:
            name = str(organization.get("name") or "").strip().casefold()
            if wanted in name or name in wanted:
                return organization
        return organizations[0]
    return None

def _uhc_specialty_code(specialty: str) -> str:
    """Map a specialty name to a NUCC taxonomy code used by UHC."""
    key = " ".join(str(specialty or "").casefold().split())
    if not key:
        return ""
    if key in _UHC_SPECIALTY_CODES:
        return _UHC_SPECIALTY_CODES[key]
    for label, code in _UHC_SPECIALTY_CODES.items():
        if label in key or key in label:
            return code
    if "head and neck" in key or "neck" in key:
        return "207Y00000X"
    if "thoracic" in key or "cardiothoracic" in key:
        return "208G00000X"
    if "surgical oncology" in key:
        return "2086X0206X"
    if "surgery" in key or "surgeon" in key:
        return "208600000X"
    return _ANTHEM_FHIR_SPECIALTY_CODES.get(key, "")

def _uhc_roles_search(
    plan: dict[str, str],
    organization: dict | None = None,
    limit: int = 30,
    log: Callable[[str], None] | None = None,
) -> list[dict]:
    """Search UHC PractitionerRoles and build one card per doctor."""
    specialty_code = _uhc_specialty_code(plan.get("specialty", ""))
    if not specialty_code:
        return []
    params: list[tuple[str, str]] = [
        ("specialty", specialty_code),
        ("_count", str(max(limit, 20))),
        *_UHC_ROLE_INCLUDES,
    ]
    organization_id = ""
    preferred_org_id = ""
    if organization:
        organization_id = str(organization.get("id") or "").strip()
        preferred_org_id = organization_id
        if organization_id:
            params.append(
                ("organization", f"Organization/{organization_id}")
            )
    else:
        city, state, postal = _anthem_parse_location(
            plan.get("location", "")
        )
        if city:
            params.append(("location.address-city", city))
        if state:
            params.append(("location.address-state", state))
        if postal:
            params.append(("location.address-postalcode", postal))
    bundle = _uhc_fhir_get("PractitionerRole", params, log)
    cards: list[dict] = []
    seen: set[str] = set()
    pages = 0
    while bundle and pages < 5:
        by_type, by_id = _uhc_bundle_index(bundle)
        for role in by_type.get("PractitionerRole") or []:
            practitioner_id = _uhc_ref_id(role.get("practitioner"))
            if not practitioner_id or practitioner_id in seen:
                continue
            card = _uhc_card_from_role(
                role,
                by_type,
                by_id,
                preferred_org_id=preferred_org_id,
            )
            if card is None:
                continue
            seen.add(practitioner_id)
            cards.append(card)
            if len(cards) >= limit:
                break
        if len(cards) >= limit:
            break
        next_url = _uhc_next_link(bundle)
        if not next_url:
            break
        bundle = _uhc_fhir_url(next_url, [], log)
        pages += 1
    return cards

def _uhc_detail_item(
    practitioner: dict,
    roles_bundle: dict,
    role_id: str = "",
    organization_id: str = "",
) -> dict:
    """Build the full UHC provider modal payload."""
    by_type, by_id = _uhc_bundle_index(roles_bundle)
    roles = [
        role
        for role in by_type.get("PractitionerRole") or []
        if isinstance(role, dict)
    ]
    chosen = next(
        (
            role
            for role in roles
            if str(role.get("id") or "") == str(role_id or "")
        ),
        roles[0] if roles else {},
    )
    base = _uhc_card_from_role(
        chosen,
        by_type,
        by_id,
        preferred_org_id=organization_id,
    ) if chosen else None
    name = (
        _fhir_human_name(practitioner)
        or (base or {}).get("name")
        or "Provider"
    )
    npi = _uhc_practitioner_npi(practitioner) or (base or {}).get("npi", "")
    specialties = (
        (base or {}).get("specialty", "")
        or _uhc_practitioner_specialties(practitioner)
    )
    locations: list[str] = []
    organizations: list[str] = []
    networks: list[str] = []
    phones: list[str] = []
    accepting: list[str] = []
    for role in roles:
        _location_id, address, location_phone = _uhc_role_locations(
            role, by_id
        )
        if address and address not in locations:
            locations.append(address)
        network_ids = _uhc_role_network_ids(role)
        _org_id, organization = _uhc_role_facility(
            role, by_type, by_id, network_ids
        )
        if organization and organization not in organizations:
            organizations.append(organization)
        role_networks = _uhc_role_networks(role, by_id)
        for network in role_networks.split("; "):
            if network and network not in networks:
                networks.append(network)
        phone = _fhir_telecom(role.get("telecom")) or location_phone
        if phone and phone not in phones:
            phones.append(phone)
        role_accepting = _uhc_role_accepting(role)
        if role_accepting and role_accepting not in accepting:
            accepting.append(role_accepting)
    if organization_id and not organizations:
        organization = by_id.get(("Organization", organization_id)) or {}
        name_value = str(organization.get("name") or "").strip()
        if name_value:
            organizations.append(name_value)
    blocks: list[dict] = [
        {"type": "heading", "text": "Provider", "level": 2}
    ]
    facts = [f"Name: {name}"]
    if npi:
        facts.append(f"NPI: {npi}")
    if specialties:
        facts.append(f"Specialty: {specialties}")
    if accepting:
        facts.append("Accepting new patients: " + "; ".join(accepting))
    if networks:
        shown = networks[:10]
        facts.append(
            "Networks: "
            + "; ".join(shown)
            + (" …" if len(networks) > len(shown) else "")
        )
    blocks.append({"type": "list", "items": facts})
    if locations:
        blocks.append(
            {"type": "heading", "text": "Locations", "level": 2}
        )
        blocks.append({"type": "list", "items": locations})
    if organizations:
        blocks.append(
            {"type": "heading", "text": "Organizations", "level": 2}
        )
        blocks.append({"type": "list", "items": organizations})
    if phones:
        blocks.append(
            {"type": "heading", "text": "Contact", "level": 2}
        )
        blocks.append({"type": "list", "items": phones})
    sources = [
        f"UnitedHealthcare Practitioner: {UHC_FHIR_BASE}/Practitioner/"
        f"{practitioner.get('id', '')}"
    ]
    if role_id:
        sources.append(
            f"UnitedHealthcare PractitionerRole: {UHC_FHIR_BASE}/"
            f"PractitionerRole/{role_id}"
        )
    blocks.append(
        {"type": "heading", "text": "Sources", "level": 2}
    )
    blocks.append({"type": "list", "items": sources})
    paragraphs: list[str] = []
    for block in blocks:
        if block.get("type") == "paragraph":
            text = " ".join(str(block.get("text") or "").split())
            if text:
                paragraphs.append(text)
        elif block.get("type") == "list":
            items = [
                " ".join(str(item).split())
                for item in block.get("items") or []
                if str(item).strip()
            ]
            if items:
                paragraphs.append(" ".join(items))
    summary = " · ".join(
        part
        for part in (
            specialties,
            "; ".join(organizations),
            locations[0] if locations else "",
            "; ".join(networks),
        )
        if part
    )
    return {
        "title": name,
        "name": name,
        "npi": npi,
        "practitioner_id": str(practitioner.get("id") or ""),
        "role_id": str(chosen.get("id") or ""),
        "organization_id": organization_id,
        "organization": "; ".join(organizations),
        "specialty": specialties,
        "address": locations[0] if locations else "",
        "phone": phones[0] if phones else "",
        "network": "; ".join(networks),
        "link": f"{UHC_FHIR_BASE}/Practitioner/"
        f"{practitioner.get('id', '')}",
        "source": "UnitedHealthcare Provider Directory",
        "section": specialties,
        "summary": summary,
        "paragraphs": list(dict.fromkeys(paragraphs)),
        "docs": True,
        "docs_label": "UHC",
        "learnable": True,
        "learn_noun": "doctor",
        "findable": False,
        "article_api": "/api/uhc_provider",
        "complete": True,
        "blocks": blocks,
    }

def _uhc_role_location_states(
    role: dict,
    by_id: dict[tuple[str, str], dict],
) -> list[str]:
    """Return the states on all locations attached to one role."""
    states: list[str] = []
    locations = role.get("location") or []
    if isinstance(locations, dict):
        locations = [locations]
    for reference in locations:
        location = by_id.get(
            ("Location", _uhc_ref_id(reference))
        ) or {}
        address = location.get("address") or {}
        state = str(address.get("state") or "").strip().upper()
        if state and state not in states:
            states.append(state)
    return states

def _uhc_check_network_membership(
    plan: dict[str, str],
    log: Callable[[str], None] | None = None,
) -> dict:
    """Check one doctor against a named UHC network and optional state."""
    network_name = " ".join(str(plan.get("network") or "").split())
    if not network_name:
        return {
            "status": "network_missing",
            "message": "Tell me which UnitedHealthcare network to check.",
        }
    _city, state, _postal = _anthem_parse_location(
        plan.get("location", "")
    )
    network_org = _uhc_find_organization(network_name, log)
    if network_org is None:
        return {
            "status": "network_not_found",
            "network": network_name,
            "state": state,
        }
    network_id = str(network_org.get("id") or "").strip()
    candidates = _uhc_practitioner_candidates(plan, log)
    if not candidates:
        return {
            "status": "not_found",
            "network": network_name,
            "state": state,
        }
    for practitioner in candidates[:5]:
        roles_bundle = _uhc_roles_for_practitioner(
            str(practitioner.get("id") or ""), log
        )
        by_type, by_id = _uhc_bundle_index(roles_bundle)
        for role in by_type.get("PractitionerRole") or []:
            if network_id not in _uhc_role_network_ids(role):
                continue
            role_states = _uhc_role_location_states(role, by_id)
            if state and state not in role_states:
                continue
            _location_id, address, _phone = _uhc_role_locations(
                role, by_id
            )
            return {
                "status": "listed",
                "network": network_name,
                "network_id": network_id,
                "state": state,
                "practitioner": _fhir_human_name(practitioner),
                "npi": _uhc_practitioner_npi(practitioner),
                "role_id": str(role.get("id") or ""),
                "location": address,
            }
    return {
        "status": "not_listed",
        "network": network_name,
        "network_id": network_id,
        "state": state,
        "practitioner": _fhir_human_name(candidates[0]),
        "npi": _uhc_practitioner_npi(candidates[0]),
    }

def _uhc_nppes_fallback_card(
    plan: dict[str, str],
    log: Callable[[str], None] | None = None,
) -> dict | None:
    """Show a CMS NPPES card when the provider is absent from UHC public data."""
    npi = str(plan.get("npi") or "").strip()
    name = " ".join(str(plan.get("doctor_name") or "").split())
    if not (npi or name):
        return None
    record = _anthem_nppes_record_for_provider(npi, name)
    if not record:
        if log is not None:
            log(
                "[uhc: no UnitedHealthcare record or CMS NPPES record "
                "matched the requested doctor]"
            )
        return None
    context = _anthem_format_nppes([record])
    items = _anthem_provider_items_from_context(
        context,
        "CMS NPPES registry",
        institution=str(plan.get("facility") or ""),
    )
    if not items:
        return None
    item = items[0]
    note = "Not listed in the UHC public directory"
    item["source"] = "CMS NPPES registry"
    item["profile_source"] = note
    summary = str(item.get("summary") or "").strip()
    item["summary"] = f"{summary} · {note}" if summary else note
    item["paragraphs"] = [item["summary"]]
    item["learnable"] = True
    item["learn_noun"] = "doctor"
    return item

_UHC_KNOWN_NETWORKS = (
    "Select Plus",
    "Choice Plus",
    "NexusACO RP",
    "NexusACO OAP",
    "Charter HMO",
    "Doctors Plan",
    "Navigate",
    "Core",
)

def _uhc_fallback_plan(question: str) -> dict[str, str]:
    """Deterministic question classification when the model is unavailable."""
    text = _anthem_normalize_question(question)
    fast = _anthem_fast_plan(text)
    npi_match = re.search(r"\b\d{10}\b", text)
    npi = npi_match.group(0) if npi_match else ""
    doctor_name = fast.get("doctor_name", "")
    specialty = fast.get("specialty", "")
    facility = fast.get("facility", "")
    location = fast.get("location", "")
    network = ""
    lowered = text.casefold()
    for name in _UHC_KNOWN_NETWORKS:
        if name.casefold() in lowered:
            network = name
            break
    if not network:
        network_match = re.search(
            r"\b(?:in|inside|under|within)\s+(?:the\s+)?"
            r"([A-Z][A-Za-z0-9 &/.-]{2,60}?)\s+(?:network|plan)\b",
            text,
        )
        if network_match:
            network = network_match.group(1).strip(" ,.")
    detail_words = re.search(
        r"\b(?:tell me about|who is|profile|background|details?|credentials?)\b",
        text,
        re.IGNORECASE,
    )
    if network and (doctor_name or npi):
        qtype = "network_membership"
    elif (doctor_name or npi) and detail_words:
        qtype = "doctor_detail"
    elif doctor_name and not specialty:
        qtype = "doctor_detail"
    elif specialty and facility:
        qtype = "specialty_facility_list"
    elif specialty:
        qtype = "specialty_list"
    elif facility:
        qtype = "specialty_facility_list"
    else:
        qtype = "specialty_list"
    return {
        "type": qtype,
        "doctor_name": doctor_name,
        "npi": npi,
        "specialty": specialty,
        "facility": facility,
        "network": network,
        "location": location,
    }

def _uhc_classify_question(
    question: str,
    conversation: str = "",
) -> dict[str, str]:
    """Use the question model to classify a UHC provider question."""
    question = " ".join(str(question or "").split())
    if not question:
        return _uhc_fallback_plan(question)
    key = question.casefold() + "\n" + " ".join(
        str(conversation or "").casefold().split()
    )
    with _UHC_QUESTION_LOCK:
        cached = _UHC_QUESTION_CACHE.get(key)
    if cached is not None:
        return dict(cached)
    prompt = _UHC_QUESTION_PROMPT.replace(
        "{{CONVERSATION}}", conversation or "(none)"
    ).replace("{{QUESTION}}", question)
    try:
        raw = chat.generate_text(
            prompt,
            model=UHC_MODEL,
            temperature=0.0,
            num_predict=300,
            num_ctx=chat.ANSWER_NUM_CTX,
        )
    except RuntimeError:
        return _uhc_fallback_plan(question)
    parsed = _anthem_json_object(raw)
    if not isinstance(parsed, dict):
        return _uhc_fallback_plan(question)
    result = {
        key: " ".join(str(parsed.get(key) or "").split())
        for key in (
            "type",
            "doctor_name",
            "npi",
            "specialty",
            "facility",
            "network",
            "location",
        )
    }
    if result["type"] not in {
        "doctor_detail",
        "specialty_list",
        "specialty_facility_list",
        "network_membership",
    }:
        result = _uhc_fallback_plan(question)
    if result["type"] == "doctor_detail" and not (
        result["doctor_name"] or result["npi"]
    ):
        result = _uhc_fallback_plan(question)
    if result["type"] == "network_membership" and not (
        result["network"]
        and (result["doctor_name"] or result["npi"])
    ):
        result = _uhc_fallback_plan(question)
    with _UHC_QUESTION_LOCK:
        _UHC_QUESTION_CACHE[key] = dict(result)
    return dict(result)


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .directory_anthem import UHC_FHIR_BASE, UHC_MODEL, _ANTHEM_FHIR_SPECIALTY_CODES, _UHC_QUESTION_PROMPT, _UHC_SPECIALTY_CODES, _anthem_facility_name_variants, _anthem_fast_plan, _anthem_format_nppes, _anthem_json_object, _anthem_names_match, _anthem_normalize_question, _anthem_nppes_record_for_provider, _anthem_parse_location, _anthem_provider_items_from_context, _anthem_split_name, _fhir_address, _fhir_bundle_resources, _fhir_codeables, _fhir_human_name, _fhir_reference_display, _fhir_reference_id, _fhir_telecom
