"""Locate a proposed check in public requirement sources. Case names only select plan rows."""
import argparse
import json
from pathlib import Path


def locate(sources, owner, quote, source_id=None):
    """Return a unique owner-local occurrence, including repetitions within one source."""
    if not isinstance(quote, str) or not quote.strip():
        return {"error": "empty_quote"}
    address = None
    for src in sources:
        if src["owner_id"] != owner or (source_id is not None and src["source_id"] != source_id):
            continue
        start = src["text"].find(quote)
        if start < 0:
            continue
        if address is not None or src["text"].find(quote, start + 1) >= 0:
            return {"error": "ambiguous_quote"}
        address = dict(source_id=src["source_id"], owner_id=owner, scenario_id=src["scenario_id"],
                       kind=src["kind"], span=[start, start + len(quote)])
    return address if address is not None else {"error": "quote_not_found"}


def bind_check_source(sources, leaf, claim, plan, link):
    """Bind a case/step link and its two quotations. A description provides only owner-level context."""
    result = {}
    def reject(reason):
        return dict(result, error=reason)

    if not leaf or link.get("leaf_id") != leaf["leaf_id"] or claim.get("source_id") not in leaf["source_ids"]:
        return reject("claim_outside_leaf")
    claim_source = locate(sources, claim.get("owner_id"), claim.get("quote"), claim["source_id"])
    if "error" in claim_source:
        return reject("claim_" + claim_source["error"])
    result["claim_source"] = claim_source

    name, index = link.get("case"), link.get("step")
    cases = plan.get("cases") if isinstance(plan, dict) else None
    if not isinstance(name, str) or not name.strip() or not isinstance(cases, list):
        return reject("case_not_found")
    matches = [c for c in cases if isinstance(c, dict) and c.get("name") == name]
    if len(matches) != 1:
        return reject("case_not_found" if not matches else "ambiguous_case_name")
    case, = matches
    if case.get("requirement_id") != leaf["leaf_id"]:
        return reject("case_owner_mismatch")
    if type(index) is not int or not isinstance(case.get("steps"), list) or not 0 <= index < len(case["steps"]):
        return reject("step_not_found")

    case_source = locate(sources, leaf["leaf_id"], case.get("requirement_quote"))
    if "error" in case_source:
        return reject("case_" + case_source["error"])
    result["case_source"] = case_source
    if claim_source["scenario_id"] is not None:
        if case_source["scenario_id"] is None:
            return reject("case_quote_has_no_scenario")
        if (claim_source["owner_id"], claim_source["scenario_id"]) != (case_source["owner_id"], case_source["scenario_id"]):
            return reject("different_public_scenario")
    result["scope"] = "same_scenario" if claim_source["scenario_id"] is not None else "requirement_context"
    return result


def replay(packet, proposal):
    """Evaluate the original declared links without changing required_ids, unresolved, or any proposed step."""
    leaves = {row["leaf_id"]: row for row in packet["leaves"]}
    plans = {}
    for row in proposal["leaves"]:
        key = row["leaf_id"]
        # Two candidate plans for one leaf do not identify a unique case.
        plans[key] = None if key in plans else row.get("plan")
    links = []
    for claim in proposal["obligations"]:
        for link in claim.get("checks") or []:
            key = link.get("leaf_id")
            binding = bind_check_source(packet["sources"], leaves.get(key), claim, plans.get(key), link)
            links.append(dict(obligation=claim["id"], link=link, binding=binding))
    return links


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packet", type=Path)
    parser.add_argument("proposal", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    read = lambda path: json.loads(path.read_text(encoding="utf-8-sig"))
    output = json.dumps(replay(read(args.packet), read(args.proposal)), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    else:
        print(output)


if __name__ == "__main__":
    main()
