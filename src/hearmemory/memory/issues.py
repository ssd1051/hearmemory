"""Issue state machine (program rules, every change writes history).

  open -> disputed                  a related claim is judged "both"
  open|disputed|reopened -> resolved   a related claim later gets a clear supports/refutes, or the suggested
                                       check later passes (not for manual / disputed_claim issues)
  any -> closed                      issue_close event (manual)
  resolved|closed -> reopened        new refuting evidence for the related claim, or issue_reopen event
Issue ids are interfaces.issue_id_for(kind, subject) -> idempotent."""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence

from hearmemory.interfaces import OPEN_ISSUE_STATUSES, HistoryEntry, Issue, Provenance


# A disputed claim is settled by a new B1 judgment, a manual issue by a human: a passing check does not resolve them.
NO_AUTO_RESOLVE = ("manual", "disputed_claim")


class IssueBook:
    def __init__(self) -> None:
        self.issues: Dict[str, Issue] = {}
        self.targets: Dict[str, str] = {}          # issue id -> suggested check target (C2), program-side only
        self.status_ts: Dict[str, str] = {}        # issue id -> ts of the last status change
        self._by_claim: Dict[str, List[str]] = {}  # claim id -> issue ids

    def _set(self, iss: Issue, new: str, ts: str, reason: str, ref: Optional[str]) -> bool:
        if iss.status == new:
            return False
        iss.history.append(HistoryEntry(ts=ts, change=f"status {iss.status}->{new}", reason=reason, decision_ref=ref))
        iss.status = new
        self.status_ts[iss.issue_id] = ts
        return True

    def open(self, issue_id: str, kind: str, title: str, ts: str, ref: Optional[str], *, claim_id: Optional[str] = None,
             paths: Sequence[str] = (), mentions: Sequence[str] = (), source_obs_ids: Sequence[str] = (),
             suggestion: Optional[str] = None, target: Optional[str] = None, opened_by: Optional[Provenance] = None,
             status: str = "open") -> Issue:
        iss = self.issues.get(issue_id)
        if iss is None:
            iss = Issue(issue_id=issue_id, kind=kind, title=title.strip()[:300], status=status, claim_id=claim_id,
                        paths=sorted(set(paths)), mentions=sorted(set(mentions)),
                        source_obs_ids=list(dict.fromkeys(source_obs_ids)), suggestion=suggestion,
                        opened_by=opened_by, opened_ts=ts,
                        history=[HistoryEntry(ts=ts, change=f"status None->{status}", reason="issue_open:" + kind,
                                              decision_ref=ref)])
            self.issues[issue_id] = iss
            self.status_ts[issue_id] = ts
            if claim_id:
                self._by_claim.setdefault(claim_id, []).append(issue_id)
            if target:
                self.targets[issue_id] = target
            return iss
        # idempotent re-open of the same subject: merge inputs, move the state machine
        iss.paths = sorted(set(iss.paths) | set(paths))
        iss.mentions = sorted(set(iss.mentions) | set(mentions))
        for o in source_obs_ids:
            if o not in iss.source_obs_ids:
                iss.source_obs_ids.append(o)
        if suggestion and not iss.suggestion:
            iss.suggestion = suggestion
        if target and issue_id not in self.targets:
            self.targets[issue_id] = target
        if iss.status == "resolved":
            self._set(iss, "reopened", ts, "issue_open again:" + kind, ref)
        elif iss.status in OPEN_ISSUE_STATUSES and status == "disputed":
            self._set(iss, "disputed", ts, "disputed again", ref)
        return iss

    def close(self, issue_id: str, ts: str, reason: str, ref: Optional[str]) -> bool:
        iss = self.issues.get(issue_id)
        return bool(iss) and self._set(iss, "closed", ts, "issue_close:" + (reason or "manual"), ref)

    def reopen(self, issue_id: str, ts: str, reason: str, ref: Optional[str]) -> bool:
        iss = self.issues.get(issue_id)
        if iss is None or iss.status not in ("resolved", "closed"):
            return False
        return self._set(iss, "reopened", ts, "issue_reopen:" + (reason or "manual"), ref)

    def on_claim_status(self, claim_id: str, new: str, prev: Optional[str], ts: str, ref: Optional[str]) -> None:
        for iid in self._by_claim.get(claim_id, ()):
            iss = self.issues[iid]
            if new == "disputed" and iss.status in ("open", "reopened"):
                self._set(iss, "disputed", ts, "related claim judged both", ref)
            elif new in ("supported", "refuted") and iss.status in OPEN_ISSUE_STATUSES:
                self._set(iss, "resolved", ts, f"related claim judged {new}", ref)
            elif new == "refuted" and prev != "refuted" and iss.status in ("resolved", "closed"):
                self._set(iss, "reopened", ts, "new refuting evidence", ref)

    def resolve_by_passing_checks(self, passed_after) -> None:
        """passed_after(target, ts) -> run record | None (RunIndex.passed_after)."""
        for iid, target in sorted(self.targets.items()):
            iss = self.issues.get(iid)
            if iss is None or iss.kind in NO_AUTO_RESOLVE or iss.status not in OPEN_ISSUE_STATUSES:
                continue
            run = passed_after(target, self.status_ts.get(iid) or iss.opened_ts or "")
            if run is not None:
                self._set(iss, "resolved", run["ts"], "suggested check passed: " + target, run["obs_id"])

    def open_issues(self) -> List[Issue]:
        return [i for i in self.issues.values() if i.status in OPEN_ISSUE_STATUSES]
