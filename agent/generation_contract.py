"""Compact generation guidance derived from the existing public task evidence.

This is implementation context, not another acceptance checker. The original
requirements and published case bodies remain authoritative and unchanged.
"""
from __future__ import annotations

from .ticketbooking_contract import public_acceptance_context


# REQ-2.1 / expectSearchSummary: city substring locators also match stations.
# REQ-2.2 visitors can open a selected journey; REQ-3.1 protects submission.
# REQ-3.2 public cases name the review heading and validation feedback patterns.
_STATE_RULES = (
    (("REQ-2.1",), "Search results",
     "Render each departure/arrival location in its own visible text node. "
     "A station containing its city already supplies that city: do not repeat "
     "the city in a criteria summary. Keep the selected date and times visible."),
    (("REQ-2.2", "REQ-3.1"), "Selected journey / signed out",
     "Keep Train Information and the selected train, locations, times and date "
     "visible to the visitor; request sign-in and provide no working booking "
     "submission. Do not hide the read-only summary behind a page-wide auth return."),
    (("REQ-3.1", "REQ-3.2"), "Entry / review / success",
     "Use one selected-journey summary. Each endpoint must be a separate visible "
     "location; do not repeat city and station aliases or duplicate train/date "
     "in another card. Preserve the selected journey when the state changes."),
    (("REQ-3.2",), "Valid Place order -> review -> Confirm",
     "Show Please confirm the following information with read-only passenger "
     "and ticket values, Confirm and Back. Only Confirm creates the booking; "
     "Back retains entered values. Success shows one concrete booking number."),
    (("REQ-3.2",), "Invalid Place order",
     "Keep the form and create no booking. Distinguish missing name/nationality "
     "(required) from short name (at least 2) and short ID (at least 6); keep all "
     "original upper bounds, format and terms rules. Check feedback against the "
     "published expressions, not merely the presence of an error."),
)


def generation_contract_context(subtree: dict) -> str:
    """Select short state constraints using the existing task/scope gate."""
    if not public_acceptance_context(subtree):
        return ""
    scope = subtree.get("id", "ROOT")
    selected = [
        f"- {state}: {rule}"
        for requirements, state, rule in _STATE_RULES
        if scope == "ROOT" or any(
            requirement == scope or requirement.startswith(scope + ".")
            for requirement in requirements
        )
    ]
    if not selected:
        return ""
    return (
        "\n\nFirst implementation state checklist (from the supplied requirements "
        "and public interaction evidence):\n"
        + "\n".join(selected)
        + "\nBefore completing these pages, exercise these states through the real "
        "App/API using the published inputs and selectors. This checklist does "
        "not replace the original requirements or establish official acceptance.\n"
    )
