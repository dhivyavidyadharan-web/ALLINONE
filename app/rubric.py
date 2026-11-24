"""All In Capital working thesis: the rubric every score is pinned to.

v2 is reconstructed from the Case 03 seed data: 12 deal memos (M-01..M-12) and Riya's partner meeting
notes (PN-1..PN-5). Every criterion and gate records its `basis`. ASSUMPTION marks anything the data
does not evidence; verify those with the partners before go-live (docs/THESIS.md).
"""

RUBRIC_VERSION = "thesis-v2.0-memos"

# Kushal (PN-5): "tell me which one you used and why."
THESIS_USED = (
    "Scored against what the fund does in practice (12 deal memos + partner notes), not the one-line public "
    "thesis. Why: the memos are the partners' own record of how they decide, and Riya's drift observation "
    "(PN-5) is that the public wording no longer predicts the yeses. The public sector list is still applied "
    "as a gate, read the way the partners read it (label matters less than behaviour, PN-2)."
)

SECTORS_IN_THESIS = ["fintech", "deeptech", "consumer tech", "consumer brands"]

GATES = [
    {
        "key": "sector",
        "name": "Sector is in thesis",
        "definition": "Fintech, deeptech, consumer tech or consumer brand, read by substance not label "
                      "(rural credit counts as fintech).",
        "basis": "Case 03 brief + public line (PN-2); M-11 Jai Kisan: 'founder behaviour mattered more than the sector label'.",
    },
    {
        "key": "stage",
        "name": "Stage is early",
        "definition": "Pre-seed or an early first institutional round. Fail at Series A or later.",
        "basis": "Case 03 brief says pre-seed; memos show investments at 'Early round' (M-01, M-03, M-05, M-07, M-09, M-11). "
                 "Exact round-size line is an ASSUMPTION.",
    },
    {
        "key": "anti_thesis",
        "name": "Not an excluded category",
        "definition": "Not crypto/web3/token businesses, nor a business whose existence depends on a favourable "
                      "regulatory change. (ASSUMPTION, not in the memos: real-money gaming; agency/services revenue "
                      "presented as product; single-location offline businesses.)",
        "basis": "M-10 Coinlane: 'outside the areas we back, and no founder quality could change that'. Bracketed items are ASSUMPTIONS.",
    },
]

CRITERIA = [
    {
        "key": "founder_quality",
        "name": "Founder quality",
        "weight": 30,
        "definition": "Evidence of how the founders behave, not their pedigree: went to the customer / field first, "
                      "own the hardest risk in their model, know the numbers that matter for their category, are "
                      "candid about what broke, have stayed with the problem. Credentials alone cannot score above 3.",
        "anchors": {
            5: "Specific behavioural evidence (field work, customer conversations, owning risk, fluent in their key metric) from more than one source.",
            3: "Relevant experience and some behavioural evidence, or strong credentials with little behaviour shown.",
            1: "No link to the problem, or dismisses the core risk / key metric as someone else's problem.",
        },
        "basis": "M-05 Wint, M-09 SuperK, M-11 Jai Kisan (field-first); M-06 CampusCredit vs M-11 (owns risk); "
                 "M-07 Giva vs M-02 Petalry (knows cohorts/margins); M-03 Unbox (candid about failures); "
                 "PN-3 'credentials are not a business. Score the evidence, not the pedigree.'",
    },
    {
        "key": "path_to_market",
        "name": "Path to market",
        "weight": 20,
        "definition": "Who pays, how they are reached, and what stands in the way (regulator, licence, procurement), "
                      "with a concrete milestone sequence. Scored separately from founder quality.",
        "anchors": {5: "Named paying customers or live sites, and a clear milestone path through any regulator.",
                    3: "Plausible customer and channel, path partly specified.",
                    1: "No paying customer identified; regulatory or liability path treated as 'solved once the tech works'."},
        "basis": "PN-3 'separate founder quality from path to market'; M-01 ePlane vs M-08 AeroDrop; M-03 Unbox (live sites).",
    },
    {
        "key": "evidence_of_pull",
        "name": "Evidence of pull",
        "weight": 20,
        "definition": "Stage-appropriate proof customers want it, measured the way the category demands: consumer brands "
                      "need repeat/cohort data; consumer apps need 30/90-day retention; B2B needs pilots converting. "
                      "A missing key number drags the score; it is not skipped.",
        "anchors": {5: "Repeat / retention / conversion data that is good for the category.",
                    3: "Real usage or revenue, but the category's key number is weak or partial.",
                    1: "Growth bought (mostly paid) or the key number is missing."},
        "basis": "M-02 Petalry, M-07 Giva, M-12 Prepnest; PN-1 'a missing number should drag the score, not be skipped'.",
    },
    {
        "key": "capital_fit",
        "name": "Fits a pre-seed cheque",
        "weight": 15,
        "definition": "The model can reach a meaningful milestone on pre-seed-scale capital, without needing a large raise "
                      "for density or a loan book funded by equity.",
        "anchors": {5: "Lean, positive unit economics or a clear capital-light milestone.",
                    3: "Capital need plausible but unproven.",
                    1: "Only works with far more capital than a pre-seed fund deploys; negative contribution margin."},
        "basis": "M-04 HopBox: 'pass on structure, not people'; M-01 ePlane (cheque sized for a slow path).",
    },
    {
        "key": "defensibility",
        "name": "Defensibility",
        "weight": 15,
        "definition": "What stops a better-funded player copying it: hard-won field execution, data, distribution. "
                      "Founder-dependent or easily copied formats score low.",
        "anchors": {5: "Execution or assets that are hard to copy, evidenced.", 3: "Some differentiation.",
                    1: "Crowded category, easily copied, or the product is one person."},
        "basis": "M-12 Prepnest ('the product is the teacher'); M-09 SuperK ('hard to copy'); M-02 crowded category.",
    },
]

# Category-specific tests the partners apply (passed to the model as guidance, not as a checklist - PN-2).
CATEGORY_TESTS = {
    "consumer brands": "Repeat purchase and cohort margins decide it (M-02 vs M-07). No repeat data = low evidence_of_pull.",
    "fintech": "If it lends, do the founders own credit risk, or is it 'the NBFC's problem'? (M-06 vs M-11).",
    "deeptech": "Is there a paying customer and a concrete regulatory/certification milestone path? (M-01 vs M-08). "
                "Aditya joins the second call (PN-3).",
    "consumer tech": "30/90-day retention and defensibility against bigger players; capital needed to reach density (M-04, M-12).",
}

THRESHOLDS = {"take_call": 65, "pass_below": 45, "min_coverage": 0.60}

CRITERIA_BY_KEY = {c["key"]: c for c in CRITERIA}


def rubric_text(sector: str = "") -> str:
    lines = [f"RUBRIC {RUBRIC_VERSION}", "Hard gates (fail any -> PASS):"]
    for g in GATES:
        lines.append(f"- {g['key']}: {g['definition']}")
    lines.append("Scored criteria (0-5 each):")
    for c in CRITERIA:
        anchors = "; ".join(f"{k}={v}" for k, v in sorted(c["anchors"].items(), reverse=True))
        lines.append(f"- {c['key']} (weight {c['weight']}): {c['definition']} Anchors: {anchors}")
    test = CATEGORY_TESTS.get((sector or "").lower())
    if test:
        lines.append(f"How the partners judge this category: {test}")
    return "\n".join(lines)


def compute_total(scores: dict) -> tuple[float, float]:
    """Missing evidence counts as 0 points AND lowers coverage - never filled with an average."""
    total, covered = 0.0, 0
    for c in CRITERIA:
        s = scores.get(c["key"])
        if s is not None:
            total += (float(s) / 5.0) * c["weight"]
            covered += c["weight"]
    return round(total, 1), round(covered / 100.0, 2)


def verdict(total: float, coverage: float, gate_failures: list, hold: bool = False) -> str:
    if gate_failures:
        return "pass"
    if hold:
        return "hold"  # PN-4: possible portfolio conflict -> Kushal decides, nothing else happens first
    if coverage < THRESHOLDS["min_coverage"]:
        return "riya_review"
    if total >= THRESHOLDS["take_call"]:
        return "take_call"
    if total < THRESHOLDS["pass_below"]:
        return "pass"
    return "riya_review"


VERDICT_LABEL = {
    "take_call": "Take the call",
    "riya_review": "Riya to review",
    "hold": "Hold: portfolio overlap",
    "pass": "Pass",
}
