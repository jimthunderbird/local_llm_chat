"""Anthem and UnitedHealthcare request handling."""

from __future__ import annotations

from .core import *

class HandlerDirectoryMixin:
    def _command_anthem(self, argument: str) -> None:
        """Start, stop, or query the Anthem provider-directory mode.

        Usage: /anthem <question>
               /anthem
               /anthem on|off
        Follow-up questions are answered in the same provider-directory
        context until /anthem off is entered.
        """
        state = self.state
        value = " ".join(argument.split())
        normalized = value.casefold()
        if normalized in {"off", "stop", "exit", "disable", "disabled"}:
            if not state.anthem_mode:
                self._sse_text(
                    "output",
                    "\nAnthem provider search is already off; normal chat "
                    "is active.\n",
                )
                return
            state.anthem_mode = False
            self._reset_anthem_context()
            self._sse("meta", state.snapshot())
            self._sse_text(
                "output",
                "\nAnthem provider search is off; normal chat is back.\n",
            )
            return
        if normalized in {"on", "start", "enable", "enabled"}:
            if state.anthem_mode:
                self._sse_text(
                    "output",
                    "\nAnthem provider search is already on.\n",
                )
                return
            state.moonbit_mode = False
            state.anthem_mode = True
            if state.united_healthcare_mode:
                state.united_healthcare_mode = False
                self._reset_united_healthcare_context()
            self._reset_anthem_context()
            self._sse("meta", state.snapshot())
            self._sse_text(
                "output",
                "\nAnthem provider search is on. Ask about a doctor, "
                "specialty, hospital, or clinic; follow-up questions keep "
                "the same provider context. Use /anthem off to return to "
                "normal chat.\n",
            )
            return
        if not value:
            state.moonbit_mode = False
            state.anthem_mode = True
            if state.united_healthcare_mode:
                state.united_healthcare_mode = False
                self._reset_united_healthcare_context()
            self._reset_anthem_context()
            self._sse("meta", state.snapshot())
            self._sse_text(
                "output",
                "\nAnthem provider search is ready. Ask about a doctor, "
                "specialty, hospital, or clinic. Follow-up questions keep "
                "the same provider context; /anthem off returns to normal "
                "chat.\n",
            )
            return
        was_on = state.anthem_mode
        state.moonbit_mode = False
        state.anthem_mode = True
        if state.united_healthcare_mode:
            state.united_healthcare_mode = False
            self._reset_united_healthcare_context()
        if not was_on:
            self._reset_anthem_context()
        self._sse("meta", state.snapshot())
        self._run_anthem_question(value)

    def _reset_anthem_context(self) -> None:
        """Clear the accumulated provider-search thread and its caches."""
        state = self.state
        state.reset_conversation()
        state.anthem_context = ""
        state.anthem_plan = {}
        state.anthem_providers = []
        state.anthem_source = ""
        state.anthem_records = 0
        state.anthem_items_sent = False
        state.cache.clear()
        state.latest_answer = None
        state.latest_sources = []
        state.latest_benchmark = None
        state.code_language = ""

    def _reset_united_healthcare_context(self) -> None:
        """Clear the UnitedHealthcare provider-search thread."""
        state = self.state
        state.reset_conversation()
        state.united_healthcare_context = ""
        state.united_healthcare_plan = {}
        state.united_healthcare_providers = []
        state.united_healthcare_source = ""
        state.united_healthcare_records = 0
        state.cache.clear()
        state.latest_answer = None
        state.latest_sources = []
        state.latest_benchmark = None
        state.code_language = ""

    def _command_united_healthcare(self, argument: str) -> None:
        """Start, stop, or query the UnitedHealthcare provider mode."""
        state = self.state
        value = " ".join(argument.split())
        normalized = value.casefold()
        if normalized in {"off", "stop", "exit", "disable", "disabled"}:
            if not state.united_healthcare_mode:
                self._sse_text(
                    "output",
                    "\nUnitedHealthcare provider search is already off; "
                    "normal chat is active.\n",
                )
                return
            state.united_healthcare_mode = False
            self._reset_united_healthcare_context()
            self._sse("meta", state.snapshot())
            self._sse_text(
                "output",
                "\nUnitedHealthcare provider search is off; normal chat "
                "is back.\n",
            )
            return
        if normalized in {"on", "start", "enable", "enabled"}:
            if state.united_healthcare_mode:
                self._sse_text(
                    "output",
                    "\nUnitedHealthcare provider search is already on.\n",
                )
                return
            state.moonbit_mode = False
            state.united_healthcare_mode = True
            if state.anthem_mode:
                state.anthem_mode = False
                self._reset_anthem_context()
            self._reset_united_healthcare_context()
            self._sse("meta", state.snapshot())
            self._sse_text(
                "output",
                "\nUnitedHealthcare provider search is on. Ask about a "
                "doctor, NPI, specialty, or hospital. Use "
                "/united_healthcare off to return to normal chat.\n",
            )
            return
        if not value:
            state.moonbit_mode = False
            state.united_healthcare_mode = True
            if state.anthem_mode:
                state.anthem_mode = False
                self._reset_anthem_context()
            self._reset_united_healthcare_context()
            self._sse("meta", state.snapshot())
            self._sse_text(
                "output",
                "\nUnitedHealthcare provider search is ready. Ask about a "
                "doctor, NPI, specialty, or hospital. Use "
                "/united_healthcare off to return to normal chat.\n",
            )
            return
        was_on = state.united_healthcare_mode
        state.moonbit_mode = False
        state.united_healthcare_mode = True
        if state.anthem_mode:
            state.anthem_mode = False
            self._reset_anthem_context()
        if not was_on:
            self._reset_united_healthcare_context()
        self._sse("meta", state.snapshot())
        self._run_united_healthcare_question(value)

    def _emit_uhc_cards(
        self,
        cards: list[dict],
        description: str,
    ) -> None:
        """Stream UnitedHealthcare provider cards into the reader."""
        if not cards:
            return
        self._sse(
            "news",
            {
                "type": "start",
                "source": "UnitedHealthcare Provider Directory",
                "description": description,
                "docs": True,
                "docs_label": "UHC",
                "no_scroll": True,
                "reset": True,
            },
        )
        for index, card in enumerate(cards, start=1):
            card["index"] = index
            self._sse("news", {"type": "item", **card})

    def _run_united_healthcare_question(self, question: str) -> None:
        """Classify and answer one UnitedHealthcare provider question."""
        question = _anthem_normalize_question(question)
        question = " ".join(question.split())
        if not question:
            return
        state = self.state
        conversation = format_conversation(
            state.conversation, state.conversation_summary
        )
        self._sse_text(
            "log", "[uhc: classifying the provider question]"
        )
        plan = _uhc_classify_question(question, conversation)
        state.united_healthcare_plan = plan
        self._sse_text(
            "log",
            "[uhc: type="
            f"{plan.get('type') or 'unknown'}, "
            f"doctor={plan.get('doctor_name') or 'none'}, "
            f"npi={plan.get('npi') or 'none'}, "
            f"specialty={plan.get('specialty') or 'none'}, "
            f"facility={plan.get('facility') or 'none'}, "
            f"network={plan.get('network') or 'none'}, "
            f"location={plan.get('location') or 'none'}]"
        )
        plan_type = plan.get("type")
        if plan_type == "doctor_detail":
            practitioner, roles_bundle = (
                _uhc_find_practitioner_with_roles(
                    plan, lambda text: self._sse_text("log", text)
                )
            )
            if practitioner is None:
                fallback = _uhc_nppes_fallback_card(
                    plan, lambda text: self._sse_text("log", text)
                )
                if fallback is not None:
                    note = (
                        f"{fallback.get('name')} is not listed in the "
                        "UnitedHealthcare public provider directory. "
                        "Showing the public CMS NPPES record instead; this "
                        "does not confirm UnitedHealthcare network "
                        "participation."
                    )
                    self._sse_text("delta", note)
                    self._emit_uhc_cards(
                        [fallback],
                        "Not found in the UnitedHealthcare public "
                        "directory · showing CMS NPPES record",
                    )
                    state.united_healthcare_context = (
                        note + " " + str(fallback.get("summary") or "")
                    ).strip()
                    state.united_healthcare_providers = [fallback]
                    state.united_healthcare_source = (
                        "CMS NPPES registry (UHC directory has no match)"
                    )
                    state.united_healthcare_records = 1
                    state.latest_answer = note
                    state.latest_sources = []
                    state.latest_benchmark = {
                        "uhc_source": state.united_healthcare_source,
                        "uhc_records": 1,
                        "model": "CMS NPPES fallback",
                    }
                    state.record_turn(question, question, note)
                    self._sse("annotations", {"items": []})
                    return
                message = (
                    "I could not find that doctor in the UnitedHealthcare "
                    "public provider directory or the CMS NPPES registry."
                )
                self._sse_text("delta", message)
                state.latest_answer = message
                state.latest_sources = []
                state.latest_benchmark = {
                    "uhc_source": "UnitedHealthcare Provider Directory",
                    "uhc_records": 0,
                    "model": "UnitedHealthcare Plan-Net FHIR",
                }
                state.record_turn(question, question, message)
                self._sse("annotations", {"items": []})
                return
            item = _uhc_detail_item(practitioner, roles_bundle)
            self._emit_uhc_cards(
                [item],
                f"UnitedHealthcare profile for {item.get('name')}",
            )
            state.united_healthcare_context = str(
                item.get("summary") or ""
            )
            state.united_healthcare_providers = [item]
            state.united_healthcare_source = (
                "UnitedHealthcare Provider Directory"
            )
            state.united_healthcare_records = 1
            state.latest_answer = ""
            state.latest_sources = []
            state.latest_benchmark = {
                "uhc_source": state.united_healthcare_source,
                "uhc_records": 1,
                "model": "UnitedHealthcare Plan-Net FHIR",
            }
            state.record_turn(
                question, question, "Provider details displayed as 1 card."
            )
            self._sse("annotations", {"items": []})
            return

        if plan_type == "network_membership":
            result = _uhc_check_network_membership(
                plan, lambda text: self._sse_text("log", text)
            )
            status = result.get("status")
            network = result.get("network") or plan.get("network") or ""
            location_state = (
                result.get("state") or plan.get("location") or ""
            )
            practitioner_name = (
                result.get("practitioner")
                or plan.get("doctor_name")
                or "The doctor"
            )
            npi = result.get("npi") or plan.get("npi") or ""
            where = f" in {location_state}" if location_state else ""
            if status == "listed":
                message = (
                    f"Yes — {practitioner_name}"
                    + (f" (NPI {npi})" if npi else "")
                    + f" is listed in the UnitedHealthcare public {network} "
                    f"network{where}."
                )
                if result.get("location"):
                    message += (
                        f" Directory location: {result['location']}."
                    )
            elif status == "not_listed":
                message = (
                    f"No — {practitioner_name}"
                    + (f" (NPI {npi})" if npi else "")
                    + " is in the UnitedHealthcare public provider "
                    f"directory, but I did not find a {network} role{where}."
                )
            elif status == "not_found":
                message = (
                    f"{practitioner_name}"
                    + (f" (NPI {npi})" if npi else "")
                    + " is not listed in the UnitedHealthcare public "
                    f"provider directory, so there is no {network} record "
                    f"for them{where}. This does not by itself prove "
                    "out-of-network status: hospital insurance-acceptance "
                    "pages list plans at the facility level and do not "
                    "confirm an individual doctor's network status. "
                    "Confirm with UnitedHealthcare or the doctor's office."
                )
            elif status == "network_not_found":
                message = (
                    f"I could not resolve the network {network!r} in the "
                    "UnitedHealthcare public directory."
                )
            else:
                message = (
                    "Tell me which UnitedHealthcare network to check, "
                    "such as Select Plus or Choice Plus."
                )
            self._sse_text("delta", message)
            state.latest_answer = message
            state.latest_sources = []
            state.latest_benchmark = {
                "uhc_source": "UnitedHealthcare Plan-Net FHIR",
                "uhc_records": 1 if status == "listed" else 0,
                "model": "UnitedHealthcare Plan-Net FHIR",
            }
            state.record_turn(question, question, message)
            self._sse("annotations", {"items": []})
            return

        specialty = str(plan.get("specialty") or "").strip()
        if not _uhc_specialty_code(specialty):
            message = (
                "Tell me which medical specialty to search for, such as "
                "cardiology, thoracic surgery, otolaryngology, or "
                "dermatology."
            )
            self._sse_text("delta", message)
            state.latest_answer = message
            state.latest_sources = []
            state.latest_benchmark = {
                "uhc_source": "UnitedHealthcare Provider Directory",
                "uhc_records": 0,
                "model": "UnitedHealthcare Plan-Net FHIR",
            }
            state.record_turn(question, question, message)
            self._sse("annotations", {"items": []})
            return

        organization: dict | None = None
        facility = str(plan.get("facility") or "").strip()
        location = str(plan.get("location") or "").strip()
        if plan_type == "specialty_facility_list" and facility:
            organization = _uhc_find_organization(
                facility, lambda text: self._sse_text("log", text)
            )
            if organization is None and not location:
                message = (
                    f"I could not resolve {facility!r} in the "
                    "UnitedHealthcare provider directory."
                )
                self._sse_text("delta", message)
                state.latest_answer = message
                state.latest_sources = []
                state.latest_benchmark = {
                    "uhc_source": "UnitedHealthcare Provider Directory",
                    "uhc_records": 0,
                    "model": "UnitedHealthcare Plan-Net FHIR",
                }
                state.record_turn(question, question, message)
                self._sse("annotations", {"items": []})
                return

        cards = _uhc_roles_search(
            plan,
            organization=organization,
            limit=20,
            log=lambda text: self._sse_text("log", text),
        )
        if not cards:
            where = facility or location
            message = (
                "No UnitedHealthcare directory providers were found for "
                f"{specialty}"
                + (f" in {where}." if where else ".")
            )
            self._sse_text("delta", message)
            state.latest_answer = message
            state.latest_sources = []
            state.latest_benchmark = {
                "uhc_source": "UnitedHealthcare Provider Directory",
                "uhc_records": 0,
                "model": "UnitedHealthcare Plan-Net FHIR",
            }
            state.record_turn(question, question, message)
            self._sse("annotations", {"items": []})
            return
        description = (
            f"UnitedHealthcare Provider Directory · {specialty}"
            + (f" · {facility}" if facility else "")
            + f" · {len(cards)} provider record(s); click a result to open "
            "the profile"
        )
        self._emit_uhc_cards(cards, description)
        state.united_healthcare_context = " ".join(
            str(card.get("summary") or "") for card in cards
        )
        state.united_healthcare_providers = cards
        state.united_healthcare_source = (
            "UnitedHealthcare Provider Directory"
        )
        state.united_healthcare_records = len(cards)
        state.latest_answer = ""
        state.latest_sources = []
        state.latest_benchmark = {
            "uhc_source": state.united_healthcare_source,
            "uhc_records": len(cards),
            "model": "UnitedHealthcare Plan-Net FHIR",
        }
        state.record_turn(
            question,
            question,
            f"Provider results displayed as {len(cards)} cards.",
        )
        self._sse("annotations", {"items": []})

    def _run_anthem_question(self, question: str) -> None:
        """Search the Anthem directory and stream API results directly."""
        question = _anthem_normalize_question(question)
        question = " ".join(question.split())
        if not question:
            return
        state = self.state
        previous_context = state.anthem_context
        search_keys = ("doctor_name", "specialty", "facility", "location")

        # Fast local parsing is the primary path. The question model is used
        # only when the fast plan has no search fields or needs a more
        # clinically specific specialty (for example a neck lump that the
        # fast parser reduced to generic "Surgery").
        plan = _anthem_fast_plan(question)
        self._sse_text(
            "log",
            "[anthem: fast search parameters: "
            + (
                ", ".join(
                    f"{key}={value}"
                    for key, value in plan.items()
                    if value
                )
                or "none"
            )
            + "]",
        )
        if not plan.get("doctor_name") and _anthem_needs_smart_specialty(
            question, plan
        ):
            smart = _anthem_smart_specialty(question, plan)
            if smart.get("specialty"):
                fast_specialty = plan.get("specialty") or "none"
                plan["specialty"] = smart["specialty"]
                if smart.get("keywords"):
                    plan["keywords"] = smart["keywords"]
                self._sse_text(
                    "log",
                    "[anthem: smart specialty "
                    f"{plan['specialty']!r} (fast parser: "
                    f"{fast_specialty!r})]",
                )
        similar = _anthem_similar_request(question)
        if similar:
            self._run_anthem_similar_doctors(question, similar)
            return
        has_search_fields = any(plan.get(key) for key in search_keys)

        target = (
            _anthem_select_provider(question, state.anthem_providers)
            if (
                state.anthem_providers
                and _anthem_is_detail_request(question)
            )
            else None
        )
        if target is not None:
            target_name = str(
                target.get("name") or target.get("title") or ""
            ).strip()
            self._sse_text(
                "log",
                "[anthem: loading the full profile for "
                f"{target_name or 'the selected provider'}]",
            )
            try:
                detail = _anthem_provider_detail(
                    str(target.get("npi") or ""),
                    target_name,
                    specialty=str(target.get("specialty") or ""),
                    institution=str(
                        target.get("institution")
                        or state.anthem_plan.get("facility")
                        or ""
                    ),
                    source_label=state.anthem_source,
                    anthem_provider_id=str(
                        target.get("anthem_provider_id") or ""
                    ),
                    anthem_address_id=str(
                        target.get("anthem_address_id") or ""
                    ),
                )
                answer = _anthem_provider_detail_text(detail)
            except Exception as exc:
                self._sse_text(
                    "log",
                    f"[anthem: could not load provider detail: {exc}]",
                )
                answer = ""
            if not answer:
                answer = _anthem_follow_up_answer(
                    question, state.anthem_providers
                ) or (
                    f"Could not load the full profile for "
                    f"{target_name or 'the selected provider'}."
                )
            self._sse_text("delta", answer)
            state.latest_answer = answer
            state.latest_sources = []
            state.latest_benchmark = {
                "anthem_source": (
                    state.anthem_source or "current provider results"
                ),
                "anthem_records": len(state.anthem_providers),
                "model": "direct API records + provider profile",
            }
            state.record_turn(question, question, answer)
            self._sse("annotations", {"items": []})
            return

        if not has_search_fields and state.anthem_providers:
            answer = _anthem_follow_up_answer(
                question, state.anthem_providers
            )
            if answer is None:
                answer = (
                    f"I have {len(state.anthem_providers)} provider(s) in "
                    "the current result set. Click a provider for the full "
                    "profile, or ask for a phone number, address, or "
                    "specialty."
                )
            self._sse_text("delta", answer)
            state.latest_answer = answer
            state.latest_sources = []
            state.latest_benchmark = {
                "anthem_source": (
                    state.anthem_source or "current provider results"
                ),
                "anthem_records": len(state.anthem_providers),
                "model": "direct API records",
            }
            state.record_turn(question, question, answer)
            self._sse("annotations", {"items": []})
            return

        if not has_search_fields:
            conversation_text = format_conversation(
                state.conversation, state.conversation_summary
            )
            try:
                plan = _anthem_search_plan(
                    question, conversation_text
                )
            except Exception as exc:
                self._sse_text(
                    "log",
                    f"[anthem: could not parse the search plan: {exc}]",
                )
                plan = _anthem_plan_fallback(question)
            plan = _anthem_clean_plan(plan, question)
            has_search_fields = any(
                plan.get(key) for key in search_keys
            )
            self._sse_text(
                "log",
                "[anthem: model search parameters: "
                + (
                    ", ".join(
                        f"{key}={value}"
                        for key, value in plan.items()
                        if value
                    )
                    or "none"
                )
                + "]",
            )

        # A question that names a doctor and asks for background should load
        # that provider's full profile directly. Requiring a previous result
        # list here made "tell me about <doctor> in <hospital>" return a
        # search list instead of the requested detail.
        if _anthem_should_show_provider_detail(question, plan):
            doctor_name = str(plan.get("doctor_name") or "").strip()
            specialty = str(plan.get("specialty") or "").strip()
            institution = str(
                plan.get("facility")
                or state.anthem_plan.get("facility")
                or ""
            ).strip()
            source_label = (
                state.anthem_source or "Anthem Provider Directory"
            )
            npi = ""
            selected = (
                _anthem_select_provider(question, state.anthem_providers)
                if state.anthem_providers
                else None
            )
            if selected is not None:
                doctor_name = str(
                    selected.get("name")
                    or selected.get("title")
                    or doctor_name
                ).strip()
                npi = str(selected.get("npi") or "").strip()
                specialty = str(
                    selected.get("specialty") or specialty
                ).strip()
                institution = str(
                    selected.get("institution") or institution
                ).strip()
                source_label = state.anthem_source or source_label
            self._sse_text(
                "log",
                "[anthem: loading the full profile for "
                f"{doctor_name or 'the requested doctor'}]",
            )
            try:
                detail = _anthem_provider_detail(
                    npi,
                    doctor_name,
                    specialty=specialty,
                    institution=institution,
                    source_label=source_label,
                    anthem_provider_id=str(
                        (selected or {}).get("anthem_provider_id") or ""
                    ),
                    anthem_address_id=str(
                        (selected or {}).get("anthem_address_id") or ""
                    ),
                )
                answer = _anthem_provider_detail_text(detail)
            except Exception as exc:
                self._sse_text(
                    "log",
                    f"[anthem: could not load provider detail: {exc}]",
                )
                answer = ""
            if answer:
                self._sse_text("delta", answer)
                state.latest_answer = answer
                state.latest_sources = []
                state.latest_benchmark = {
                    "anthem_source": source_label or "provider profile",
                    "anthem_records": 1,
                    "model": "direct API records + provider profile",
                }
                state.record_turn(question, question, answer)
                self._sse("annotations", {"items": []})
                return
            self._sse_text(
                "log",
                "[anthem: provider detail was unavailable; falling back "
                "to a directory search]",
            )

        if not has_search_fields:
            message = (
                "Tell me a doctor's name, a specialty, a hospital or "
                "clinic, or a city/state to search."
            )
            self._sse_text("delta", message)
            state.latest_answer = message
            state.latest_sources = []
            state.latest_benchmark = {
                "anthem_source": "none",
                "anthem_records": 0,
                "model": "direct API records",
            }
            state.record_turn(question, question, message)
            self._sse("annotations", {"items": []})
            return

        institution = plan.get("facility", "")
        condition_filter_term = _anthem_directory_condition_term(
            question, plan, institution
        )
        condition_filter_tokens = _anthem_condition_tokens(
            condition_filter_term
        )
        modality_filters = _anthem_requested_modalities(question)
        requested_limit = _anthem_requested_limit(question)

        streamed_items: list[dict] = []
        stream_started = False
        status_executor: ThreadPoolExecutor | None = None
        status_futures: list = []

        def stream_provider_status(provider_item: dict) -> None:
            self._sse("news", {"type": "update", **provider_item})

        def annotate_provider_batch(batch: list[dict]) -> None:
            _anthem_annotate_member_status(
                batch,
                plan.get("location", ""),
                on_update=stream_provider_status,
                institution=institution,
            )

        def queue_provider_status(batch: list[dict]) -> None:
            nonlocal status_executor
            if not batch:
                return
            if status_executor is None:
                status_executor = ThreadPoolExecutor(max_workers=1)
            status_futures.append(
                status_executor.submit(
                    annotate_provider_batch, list(batch)
                )
            )

        def finish_provider_status() -> None:
            nonlocal status_executor
            for future in status_futures:
                try:
                    future.result()
                except Exception:
                    pass
            status_futures.clear()
            if status_executor is not None:
                status_executor.shutdown(wait=True)
                status_executor = None

        def stream_provider_batch(batch_context: str) -> None:
            nonlocal stream_started
            batch_items = _anthem_provider_items_from_context(
                batch_context,
                "CMS NPPES registry",
                institution=institution,
            )
            if not batch_items:
                return
            if not stream_started:
                self._sse(
                    "news",
                    {
                        "type": "start",
                        "source": "Anthem Provider Directory",
                        "description": (
                            "CMS NPPES registry · streaming provider "
                            "results; click a result to open its profile"
                        ),
                        "docs": True,
                        "docs_label": "NPPES",
                        "no_scroll": True,
                        "reset": True,
                    },
                )
                stream_started = True
            for item in batch_items:
                item["index"] = len(streamed_items) + 1
                item["anthem_status_pending"] = True
                streamed_items.append(item)
                self._sse("news", {"type": "item", **item})
            queue_provider_status(batch_items)

        self._sse_text(
            "log", "[anthem: querying the provider directory APIs]"
        )
        context, source_label, errors, record_count = _anthem_lookup(
            plan,
            lambda text: self._sse_text("log", text),
            on_batch=stream_provider_batch,
            condition_tokens=condition_filter_tokens,
            modalities=modality_filters,
            max_results=requested_limit,
        )
        if not context:
            finish_provider_status()
            message = (
                "I could not retrieve provider records from the CMS "
                "NPPES registry.\n"
            )
            if errors:
                message += "\nWhat failed:\n" + "\n".join(
                    f"- {error}" for error in errors[-4:]
                )
            if state.anthem_providers:
                message += (
                    "\nThe previous provider results are still available."
                )
            else:
                state.anthem_context = ""
                state.anthem_plan = {}
                state.anthem_providers = []
                state.anthem_source = ""
                state.anthem_records = 0
                state.anthem_items_sent = False
            self._sse_text("delta", message)
            state.latest_answer = message
            state.latest_sources = []
            state.latest_benchmark = {
                "anthem_source": "none",
                "anthem_errors": errors,
                "model": "direct API records",
            }
            state.record_turn(question, question, message)
            self._sse("annotations", {"items": []})
            return

        if streamed_items:
            provider_items = streamed_items
            self._sse_text(
                "log",
                f"[anthem: streamed {len(provider_items)} provider(s) "
                "in batches]",
            )
        else:
            # The API response itself is the result list. Parse it into
            # clickable cards and keep the same records for follow-ups.
            provider_items = _anthem_provider_items_from_context(
                context, source_label, institution=institution
            )
            if condition_filter_tokens and modality_filters:
                relevant_items = [
                    item
                    for item in provider_items
                    if _anthem_provider_matches_condition(
                        item, condition_filter_tokens, modality_filters
                    )
                ]
                if relevant_items:
                    self._sse_text(
                        "log",
                        "[anthem: kept "
                        f"{len(relevant_items)} provider(s) matching "
                        f"{condition_filter_term!r} and the requested "
                        "treatment]",
                    )
                    provider_items = relevant_items
                else:
                    self._sse_text(
                        "log",
                        "[anthem: no NPPES provider matched the "
                        "condition and treatment; keeping the wider "
                        "list for facility verification]",
                    )
            if requested_limit and len(provider_items) > requested_limit:
                provider_items = provider_items[:requested_limit]
                self._sse_text(
                    "log",
                    "[anthem: limiting the result list to "
                    f"{requested_limit} provider(s)]",
                )
        member_plan: dict = {}
        if provider_items:
            member_plan = _anthem_member_plan()
            for index, provider_item in enumerate(
                provider_items, start=1
            ):
                provider_item["index"] = index
                if not streamed_items:
                    provider_item["anthem_status_pending"] = True
            if member_plan.get("member_id"):
                plan_name = (
                    member_plan.get("plan_name") or "Anthem member plan"
                )
                self._sse_text(
                    "log",
                    f"[anthem: member plan {plan_name} "
                    f"(prefix {member_plan.get('prefix') or '?'}, "
                    f"state {member_plan.get('state') or '?'}, "
                    f"effective "
                    f"{member_plan.get('effective_date') or '?'})]",
                )
        state.anthem_context = context
        state.anthem_plan = plan
        state.anthem_providers = provider_items
        state.anthem_source = source_label
        state.anthem_records = record_count
        state.anthem_items_sent = bool(provider_items)

        added_related: list[dict] = []
        condition_term = ""

        # Send the result cards before the network checks. The cards show a
        # pending label, and each card is updated by a later "news" event as
        # soon as its own member-plan check finishes.
        if provider_items:
            if not stream_started:
                self._sse(
                    "news",
                    {
                        "type": "start",
                        "source": "Anthem Provider Directory",
                        "description": (
                            f"{source_label} · "
                            f"{len(provider_items)} provider record(s)"
                            + (
                                " · member plan "
                                + (
                                    member_plan.get("plan_name")
                                    or "Anthem member plan"
                                )
                                + f" ({member_plan.get('prefix')})"
                                if member_plan.get("prefix")
                                else ""
                            )
                            + "; click a result to open its profile"
                        ),
                        "docs": True,
                        "docs_label": "NPPES",
                        "no_scroll": True,
                        "reset": True,
                    },
                )
                for provider_item in provider_items:
                    self._sse(
                        "news", {"type": "item", **provider_item}
                    )

            # The facility's own directory can add current specialists who
            # are missing from NPPES. Fetch it after the first cards are on
            # screen so its API latency does not delay the result list.
            related_items, condition_term = (
                _anthem_directory_condition_items(
                    question, plan, institution, source_label
                )
            )
            if condition_term:
                self._sse_text(
                    "log",
                    "[anthem: UCSF directory condition "
                    f"{condition_term!r} matched {len(related_items)} "
                    "specialist(s)]",
                )
            if related_items:
                existing = {
                    str(item.get("npi") or item.get("name", "")).casefold()
                    for item in provider_items
                }
                for item in related_items:
                    if (
                        condition_filter_tokens
                        and modality_filters
                        and not _anthem_provider_matches_condition(
                            item,
                            condition_filter_tokens,
                            modality_filters,
                        )
                    ):
                        continue
                    if (
                        requested_limit
                        and len(provider_items) >= requested_limit
                    ):
                        break
                    identity = str(
                        item.get("npi") or item.get("name", "")
                    ).casefold()
                    if identity in existing:
                        continue
                    existing.add(identity)
                    item["index"] = len(provider_items) + 1
                    item["anthem_status_pending"] = True
                    provider_items.append(item)
                    added_related.append(item)
                    self._sse(
                        "news", {"type": "item", **item}
                    )
                if added_related:
                    state.anthem_providers = provider_items
                    self._sse_text(
                        "log",
                        "[anthem: added "
                        f"{len(added_related)} UCSF directory "
                        f"specialist(s) for {condition_term!r}]",
                    )

            self._sse_text(
                "log",
                "[anthem: checking the member-plan directory and "
                "academic titles in the background]",
            )

            if streamed_items:
                if added_related:
                    queue_provider_status(added_related)
                finish_provider_status()
            else:
                _anthem_annotate_member_status(
                    provider_items,
                    plan.get("location", ""),
                    member_plan=member_plan,
                    on_update=stream_provider_status,
                    institution=institution,
                )
            member_counts = {"listed": 0, "not_listed": 0, "unknown": 0}
            for provider_item in provider_items:
                member_status = str(
                    provider_item.get("anthem_member_plan") or "unknown"
                )
                member_counts[member_status] = (
                    member_counts.get(member_status, 0) + 1
                )
            if member_plan.get("prefix"):
                self._sse_text(
                    "log",
                    "[anthem: member-plan flag: "
                    f"{member_counts['listed']} in network, "
                    f"{member_counts['not_listed']} out of network, "
                    f"{member_counts['unknown']} unconfirmed]",
                )

        # The provider cards are the answer. Do not duplicate them as a
        # verbose text list, plan summary, or source note in the transcript.
        response = ""
        if not provider_items:
            response = (
                f"Found directory records from {source_label}, but they "
                "could not be formatted as provider cards.\n\n"
                + context[:2000]
            )
            self._sse_text("delta", response)

        state.latest_answer = response
        state.latest_sources = []
        state.latest_benchmark = {
            "anthem_source": source_label,
            "anthem_records": record_count,
            "model": "direct API records",
        }
        if response:
            state.record_turn(question, question, response)
        elif provider_items:
            state.record_turn(
                question,
                question,
                f"Provider results displayed as "
                f"{len(provider_items)} cards.",
            )
        self._sse("annotations", {"items": []})

    def _run_anthem_similar_doctors(
        self,
        question: str,
        request: dict,
    ) -> None:
        """Find doctors whose specialty/research is similar to one provider."""
        state = self.state
        limit = max(1, min(int(request.get("limit") or 5), 50))
        target_name = str(request.get("doctor_name") or "").strip()
        facility = str(request.get("facility") or "").strip()
        self._sse_text(
            "log",
            f"[anthem: finding {limit} similar doctor(s) to "
            f"{target_name or 'the requested doctor'}]",
        )
        try:
            target_detail = _anthem_provider_detail(
                "",
                target_name,
                institution=facility,
                source_label="CMS NPPES registry",
            )
        except Exception as exc:
            target_detail = {}
            self._sse_text(
                "log",
                f"[anthem: could not load the target doctor: {exc}]",
            )
        if not target_detail:
            message = (
                f"I could not find {target_name or 'that doctor'} in the "
                "provider directory."
            )
            self._sse_text("delta", message)
            state.latest_answer = message
            state.latest_sources = []
            state.latest_benchmark = {
                "anthem_source": "CMS NPPES registry",
                "anthem_records": 0,
                "model": "direct API records + similar-doctor ranking",
            }
            state.record_turn(question, question, message)
            self._sse("annotations", {"items": []})
            return
        target_title = str(
            target_detail.get("title") or target_name
        ).strip()
        target_specialty = _anthem_infer_target_specialty(target_detail)
        target_npi = _anthem_detail_npi(target_detail)
        target_terms = _anthem_similarity_terms(target_detail)
        self._sse_text(
            "log",
            "[anthem: similar-doctor target "
            f"{target_title!r}, specialty {target_specialty!r}]",
        )
        if not target_specialty:
            message = (
                f"I found {target_title} but could not determine a "
                "searchable specialty for similar-doctor matching."
            )
            self._sse_text("delta", message)
            state.latest_answer = message
            state.latest_sources = []
            state.latest_benchmark = {
                "anthem_source": "CMS NPPES registry",
                "anthem_records": 0,
                "model": "direct API records + similar-doctor ranking",
            }
            state.record_turn(question, question, message)
            self._sse("annotations", {"items": []})
            return
        cards: list[dict] = []
        seen: set[str] = set()

        def collect(
            context: str,
            source: str,
            institution: str,
        ) -> None:
            for item in _anthem_provider_items_from_context(
                context, source, institution=institution
            ):
                name = str(
                    item.get("name") or item.get("title") or ""
                ).strip()
                npi = str(item.get("npi") or "").strip()
                if (
                    (target_npi and npi == target_npi)
                    or (name and _anthem_names_match(target_title, name))
                ):
                    continue
                key = npi or name.casefold()
                if not key or key in seen:
                    continue
                seen.add(key)
                text = " ".join(
                    str(item.get(key) or "")
                    for key in ("specialty", "section", "summary")
                ).casefold()
                score = sum(1 for term in target_terms if term in text)
                specialty_key = target_specialty.casefold()
                if specialty_key and specialty_key in text:
                    score += 20
                else:
                    specialty_tokens = [
                        token
                        for token in re.split(
                            r"[^a-z0-9]+", specialty_key
                        )
                        if len(token) > 3
                    ]
                    if specialty_tokens and all(
                        token in text for token in specialty_tokens
                    ):
                        score += 15
                    elif any(
                        token in text for token in specialty_tokens
                    ):
                        score += 5
                if institution:
                    score += 2
                item["_similarity"] = score
                cards.append(item)
                if len(cards) >= max(limit * 8, 40):
                    break

        def search(facility_value: str, institution: str) -> None:
            plan = {
                "doctor_name": "",
                "specialty": target_specialty,
                "facility": facility_value,
                "location": "",
                "plan": "",
                "keywords": "",
            }
            try:
                context, source, _errors, _count = _anthem_lookup(
                    plan,
                    lambda text: self._sse_text("log", text),
                    max_results=max(limit * 5, 25),
                )
            except Exception as exc:
                self._sse_text(
                    "log",
                    f"[anthem: similar-doctor search failed: {exc}]",
                )
                return
            if context:
                collect(context, source, institution)

        if facility:
            search(facility, facility)
        if len(cards) < limit:
            search("", "")
        cards.sort(
            key=lambda item: (
                -int(item.get("_similarity") or 0),
                str(item.get("name") or ""),
            )
        )
        cards = cards[:limit]
        for item in cards:
            item.pop("_similarity", None)
        if not cards:
            message = (
                f"No similar doctors were found for {target_title} in "
                f"{target_specialty}."
            )
            self._sse_text("delta", message)
            state.latest_answer = message
            state.latest_sources = []
            state.latest_benchmark = {
                "anthem_source": "CMS NPPES registry",
                "anthem_records": 0,
                "model": "direct API records + similar-doctor ranking",
            }
            state.record_turn(question, question, message)
            self._sse("annotations", {"items": []})
            return
        self._sse(
            "news",
            {
                "type": "start",
                "source": "Anthem Provider Directory",
                "description": (
                    f"Similar doctors to {target_title} · "
                    f"{target_specialty} · {len(cards)} provider "
                    "record(s); click a result to open the profile"
                ),
                "docs": True,
                "docs_label": "NPPES",
                "no_scroll": True,
                "reset": True,
            },
        )
        for index, card in enumerate(cards, start=1):
            card["index"] = index
            card["anthem_status_pending"] = True
            self._sse("news", {"type": "item", **card})

        def stream_provider_status(provider_item: dict) -> None:
            self._sse("news", {"type": "update", **provider_item})

        _anthem_annotate_member_status(
            cards,
            "",
            on_update=stream_provider_status,
            institution=facility,
        )
        state.anthem_plan = {
            "doctor_name": "",
            "specialty": target_specialty,
            "facility": facility,
            "location": "",
            "plan": "",
            "keywords": "",
        }
        state.anthem_context = " ".join(
            str(card.get("summary") or "") for card in cards
        )
        state.anthem_providers = cards
        state.anthem_source = "CMS NPPES registry"
        state.anthem_records = len(cards)
        state.anthem_items_sent = True
        state.latest_answer = ""
        state.latest_sources = []
        state.latest_benchmark = {
            "anthem_source": state.anthem_source,
            "anthem_records": len(cards),
            "model": "direct API records + similar-doctor ranking",
        }
        state.record_turn(
            question,
            question,
            f"Similar doctors displayed as {len(cards)} cards.",
        )
        self._sse("annotations", {"items": []})


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .conversation import format_conversation
from .directory_anthem import _anthem_annotate_member_status, _anthem_clean_plan, _anthem_condition_tokens, _anthem_detail_npi, _anthem_directory_condition_items, _anthem_directory_condition_term, _anthem_fast_plan, _anthem_follow_up_answer, _anthem_infer_target_specialty, _anthem_is_detail_request, _anthem_lookup, _anthem_member_plan, _anthem_names_match, _anthem_needs_smart_specialty, _anthem_normalize_question, _anthem_plan_fallback, _anthem_provider_detail, _anthem_provider_detail_text, _anthem_provider_items_from_context, _anthem_provider_matches_condition, _anthem_requested_limit, _anthem_requested_modalities, _anthem_search_plan, _anthem_select_provider, _anthem_should_show_provider_detail, _anthem_similar_request, _anthem_similarity_terms, _anthem_smart_specialty
from .directory_uhc import _uhc_check_network_membership, _uhc_classify_question, _uhc_detail_item, _uhc_find_organization, _uhc_find_practitioner_with_roles, _uhc_nppes_fallback_card, _uhc_roles_search, _uhc_specialty_code
