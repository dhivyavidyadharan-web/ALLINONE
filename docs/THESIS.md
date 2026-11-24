# The working thesis: what we know, what we assumed, and what the gaps cost

Rubric `thesis-v2.0-memos` (`app/rubric.py`) is reconstructed from the Case 03 seed data in `data/seed/`:
12 deal memos (M-01..M-12: 6 invested, 6 passed) and Riya's partner meeting notes (PN-1..PN-5).

**Which thesis is used, and why** (Kushal, PN-5: "tell me which one you used and why"): what the fund does in
practice, as recorded in the memos, rather than the one-line public wording. Riya's drift observation is that the
public wording no longer predicts the yeses (SuperK, Wint). The public sector list is still a gate, read by
substance, as the partners did for Jai Kisan.

## What the data evidences

| Rubric element | Evidence |
|---|---|
| Founder quality is the heaviest criterion (30) | "the founder is most of the investment" (M-01); founder behaviour decided M-05, M-06, M-09, M-11 |
| Score behaviour, not pedigree | PN-3 "credentials are not a business"; M-08 AeroDrop (PhD, passed) |
| Founder quality separate from path to market | PN-3, agreed going forward |
| A missing key number drags the score | PN-1 (Petalry scored 6.5, "too high given the missing repeat data") |
| Consumer brands live or die on repeat/cohorts | M-02 vs M-07 |
| Lenders must own credit risk | M-06 vs M-11; PN-2 |
| Deeptech needs a customer + regulator milestone path | M-01 vs M-08; PN-3 |
| Capital-hungry models fail the fit test | M-04 HopBox ("pass on structure, not people") |
| Crypto/web3 excluded | M-10 Coinlane |
| Portfolio conflict → hold for Kushal | PN-4 (MeetRecord near-miss) |
| Calls: Tue/Thu afternoons, 30 min, Aditya for deeptech | PN-5, PN-3 |
| Nobody auto-rejected or auto-advanced | PN-5 (the system recommends; a human decides) |

## Still assumed or missing, and what each gap costs

| Item | Status | Cost to the system |
|---|---|---|
| Full portfolio list | **Missing.** Only the 6 invested memos plus MeetRecord are known | A conflict with any other portfolio company will not be flagged. Add it to `data/seed/portfolio.json` (set `"complete": true`). |
| Stage line (how late is too late) | ASSUMPTION: fail at Series A+ | Late-seed companies may be misjudged |
| Real-money gaming, agency revenue, single-location businesses as exclusions | ASSUMPTION (not in the memos) | Possible false Passes in those categories |
| Criterion weights (30/20/20/15/15) | Inferred from emphasis in memos, not stated | Totals near the 45/65 thresholds are fragile; re-tune with the backtest |
| Founder behaviour (defensiveness, owning risk) | Visible in calls, rarely in decks | First-pass founder scores rest on the deck and form; the memos' strongest signals only appear after a call |
| Applicant websites | All `.example` placeholder domains in the seed data | The scraper refuses placeholder domains, so every claim is founder-supplied (shown as a gap). Real applications with real sites get up to 5 pages read. |
| Ledgerly memo (PN-5 "Kushal still thinks that was a miss") | Not in the pack | Can't be backtested |
| Riya's drift question | Open (PN-5) | The rubric follows practice; if partners decide on the public wording instead, the weights change |
