# Can Riya hand screening to this before the Fund II announcement?

Kushal's question: *"How do I know I can trust this tool?"* The answer is a set of numbers agreed **before**
looking at the results, measured on real data, plus a fallback that keeps Riya in charge.

## 1. Go-live bar (agree these in advance)

| # | Measure | Bar | Where it's measured |
|---|---|---|---|
| 1 | Funded past deals the system would mark **Pass** | **0** (the costly error) | `/backtest` |
| 2 | Agreement with partners where the system commits | **≥ 80%** | `/backtest`, then shadow mode |
| 3 | Abstain rate ("Riya to review") | **≤ 30%**, else it isn't saving time | `/backtest`, inbox |
| 4 | Scores with no valid citation reaching a partner | **0** (enforced in code) | `tests/test_system.py::test_every_score_has_a_source` |
| 5 | Model payloads containing email / phone / founder name | **0** | `/audit`, `test_no_pii_reaches_the_model` |
| 6 | Volume: 100 applications in one day, none lost | every app scored, queued or "needs attention" with a reason | `/api/health`, `test_volume_20_none_disappear` (raise `n`) |
| 7 | Cost per application | under the agreed budget; daily limit set to 100 × that + headroom | `/costs` |

## 2. Status as built

Measured on the real Case 03 seed data (12 memos, 10 applications) with the **offline mock model**:

* Bars 4, 5 and 6 pass (automated tests on SQLite and Postgres 16; volume, failure, budget-pause tests).
* On the 10 real applications, the deterministic checks do what the partner notes ask: Talkbook goes to **Hold**
  (overlaps portfolio company MeetRecord, PN-4); StockSathi is held for a possible SuperK overlap; Settlr fails the
  stage gate (Series A); CareCorner fails the sector gate (healthcare); deck-vs-form contradictions are flagged
  for EarnWise (team of 12 vs 5) and StockSathi (38 vs 42 retailers).
* Bars 1-3 **do not pass with the mock model**: on the 12 memos it commits on only 5 (4 agree) and marks one
  funded deal (Unbox Robotics) Pass. A keyword heuristic cannot read founder behaviour, which is what the memos
  decide on. **Re-measure with `LLM_MODE=live` (Gemini) before anyone relies on it.**
* Backtest caveat: each memo's input includes the partners' founder assessment from the call, which a first-pass
  screen would not have. The backtest therefore tests whether the rubric reproduces the partners' judgement, not
  whether a deck alone predicts it.

## 3. Rollout

1. **Backtest on the real memos** with the live model. Partners mark each disagreement "fund was right" or
   "system was right" on `/backtest`. Stop if bar 1 fails.
2. **Shadow mode, 2 weeks / ~50 live applications.** Riya screens as today *and* the system screens. The inbox
   records agreement on every decision. Stop if bars 2–3 fail.
3. **Announcement day.** The system screens first. Riya reviews every "Take the call" and every "Riya to review",
   and spot-checks 1 in 5 "Pass" verdicts. Nothing is sent without her.
4. **Monthly**: re-run the backtest after any prompt or rubric change. The prompt version is in every trace, so a
   drop can be tied to the change that caused it.

## 4. Where it breaks (known limits)

* **The thesis is reconstructed.** Gates and weights marked ASSUMPTION in `/thesis` are guesses until partners confirm them.
* **Founder-supplied evidence dominates** when the web has little on a company (no site, no profile). Coverage drops
  and the system abstains rather than guessing, which shows up as more "Riya to review".
* **Web research is free sources only**: up to 5 pages of the applicant's site plus up to 5 independent results
  from Google Search (press, directories, founder profiles). No paid databases. LinkedIn is never opened; a profile
  Google surfaces is used only as Google's summary, labelled as such.
* **Same-name companies** are the main search risk. Results must state why they are the same company, and unsure
  matches are dropped. A confident but wrong match is still possible; the source link is on every claim so Riya can check.
* **Thin web presence**: most pre-seed companies have little press. Search often finds nothing, which shows up as a
  named gap, not as a penalty.
* **Learning is retrieval, not retraining.** A correction helps similar companies (same sector or strong keyword
  overlap). It will not generalise to unlike companies, and a bad correction will propagate until deleted.
* **Simulated mail and calendar** in this build; Gmail / Google Calendar adapters are a small swap but untested here.
