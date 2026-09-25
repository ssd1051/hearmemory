"""CLI / MCP test support: an in-memory `StoreAPI` fake and fake core / memory / judge / host
modules used by tests/test_iface_*.py (tests are built against the Protocols with fakes). `hearmemory.commands`
and `hearmemory.mcp_server` never import core/memory/judge/host at module scope; they
resolve `interfaces.ENTRY_POINTS` lazily via `importlib.import_module`, so a
fake registered in `sys.modules` under the entry point's module name is picked
up exactly like the real module will be once it exists.
"""
from __future__ import annotations

import copy
import datetime
import sys
import types
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

import hearmemory.interfaces as I


def now_ts() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def default_config() -> Dict[str, Any]:
    return copy.deepcopy(I.DEFAULT_CONFIG)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Fake StoreAPI
# ---------------------------------------------------------------------------
class FakeStore:
    """A minimal in-memory implementation of `interfaces.StoreAPI`."""

    def __init__(self, root: Path):
        self.root = root
        self.hearmemory_dir = root / ".hearmemory"
        self.initialised = True
        self._obs: List[I.Observation] = []
        self._claims: List[I.Claim] = []
        self._candidates: List[I.Candidate] = []
        self._judgments: List[I.Judgment] = []
        self._events: List[I.ControlEvent] = []
        self._state: Dict[str, Dict[str, Any]] = {}
        self.spool: List[Any] = []

    # -- observations --------------------------------------------------
    def append_observations(self, obs: Sequence[I.Observation]) -> List[str]:
        ids = []
        for o in obs:
            self._obs.append(o)
            ids.append(o.id)
        return ids

    def iter_observations(self, since_offset: int = 0) -> Iterator[Tuple[int, I.Observation]]:
        for i, o in enumerate(self._obs):
            if i >= since_offset:
                yield i, o

    def read_observations_window(self, from_offset: int, max_bytes: int) -> Tuple[int, List[I.Observation]]:
        subset = self._obs[from_offset:]
        return from_offset + len(subset), list(subset)

    # -- claims ----------------------------------------------------------
    def append_claims(self, claims: Sequence[I.Claim]) -> int:
        self._claims.extend(claims)
        return len(claims)

    def iter_claims(self) -> Iterator[I.Claim]:
        yield from self._claims

    # -- candidates --------------------------------------------------------
    def append_candidates(self, cands: Sequence[I.Candidate]) -> int:
        self._candidates.extend(cands)
        return len(cands)

    def iter_candidates(self) -> Iterator[I.Candidate]:
        yield from self._candidates

    # -- judgments -----------------------------------------------------
    def append_judgments(self, js: Sequence[I.Judgment]) -> int:
        self._judgments.extend(js)
        return len(js)

    def iter_judgments(self) -> Iterator[I.Judgment]:
        yield from self._judgments

    # -- events ----------------------------------------------------------
    def append_events(self, evs: Sequence[I.ControlEvent]) -> int:
        self._events.extend(evs)
        return len(evs)

    def iter_events(self) -> Iterator[I.ControlEvent]:
        yield from self._events

    # -- state -------------------------------------------------------------
    def read_state(self, name: str) -> Optional[Dict[str, Any]]:
        v = self._state.get(name)
        return copy.deepcopy(v) if v is not None else None

    def write_state(self, name: str, data: Mapping[str, Any]) -> None:
        self._state[name] = dict(data)

    def fingerprint(self) -> str:
        return f"fp-{len(self._obs)}-{len(self._claims)}-{len(self._events)}-{len(self._judgments)}"

    def is_initialised(self) -> bool:
        return self.initialised

    # -- test-only extras (mirrors the concrete Store) ----
    def merge_spool(self) -> int:
        n = len(self.spool)
        self.spool.clear()
        return n


class FakeRegistry:
    """Simulates on-disk persistence of `.hearmemory` across repeated `open_store()`
    calls for the same root (each MCP tool call re-opens the store)."""

    def __init__(self):
        self.by_root: Dict[str, FakeStore] = {}

    def open_store(self, root, create: bool = False):
        root = Path(root)
        key = str(root)
        store = self.by_root.get(key)
        if store is None:
            if not create:
                return None
            store = FakeStore(root)
            self.by_root[key] = store
            return store
        if not store.initialised:
            return None
        return store

    def create_store(self, root):
        """Mirrors the real `hearmemory.store.create_store`: idempotent, always
        returns an *initialised* Store (unlike `open_store(create=True)`,
        which stubs an uninitialised one for a not-yet-`init`-ed root)."""
        root = Path(root)
        key = str(root)
        store = self.by_root.get(key)
        if store is None:
            store = FakeStore(root)
            self.by_root[key] = store
        store.initialised = True
        return store

    def delete_version(self, root) -> None:
        store = self.by_root.get(str(Path(root)))
        if store is not None:
            store.initialised = False

    def purge(self, root) -> None:
        self.by_root.pop(str(Path(root)), None)


# ---------------------------------------------------------------------------
# Fake cross-module modules
# ---------------------------------------------------------------------------
def make_fake_modules(registry: FakeRegistry, *, jev_capable: bool = False,
                       jev_reason: str = "no_key") -> Dict[str, types.ModuleType]:
    """Builds a fresh set of fake core/memory/judge/host modules wired to `registry`.
    Caller installs them into `sys.modules` (see `installed_modules` below)."""
    mods: Dict[str, types.ModuleType] = {}

    def _mod(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        mods[name] = m
        return m

    # core ------------------------------------------------------------
    store_mod = _mod("hearmemory.store")
    store_mod.open_store = registry.open_store  # type: ignore[attr-defined]
    store_mod.create_store = registry.create_store  # type: ignore[attr-defined]

    config_mod = _mod("hearmemory.config")
    config_mod.load_config = lambda root: default_config()  # type: ignore[attr-defined]

    provenance_mod = _mod("hearmemory.provenance")

    def _capture(root, host, session_id=None, subagent_id=None, subagent_type=None, agent_label=None,
                 source=None, model=None):
        return I.Provenance(host=host, session_id=session_id, subagent_id=subagent_id, subagent_type=subagent_type,
                             agent_label=agent_label, model=model, git_branch="main", git_commit="0" * 40,
                             git_dirty=False, cwd=".", source=source)

    provenance_mod.capture = _capture  # type: ignore[attr-defined]

    observe_mod = _mod("hearmemory.observe")

    def _make_observation(root, cfg, kind, text, provenance, tool=None, event_key=None, refs=(), meta=None):
        ek = event_key or I.stable_id("ek-", text)
        return I.Observation(id=I.obs_id_for(ek), ts=now_ts(), kind=kind, event_key=ek, provenance=provenance,
                              text=text, tool=tool, text_sha256=I.sha256_text(text), refs=list(refs or []),
                              meta=dict(meta or {}))

    observe_mod.make_observation = _make_observation  # type: ignore[attr-defined]

    budget_mod = _mod("hearmemory.budget")

    class _FakeBudget:
        def __init__(self, store, cfg):
            self.store, self.cfg = store, cfg

        def status(self):
            j = self.cfg.get("jev", {})
            return I.BudgetStatus(day="2026-09-24", calls=0, usd=0.0, call_cap=int(j.get("daily_call_cap", 200)),
                                   usd_cap=float(j.get("daily_usd_cap", 0.05)))

    budget_mod.Budget = _FakeBudget  # type: ignore[attr-defined]

    safety_mod = _mod("hearmemory.safety")

    class _FakeDeadline:
        def __init__(self, profile, hooks_cfg):
            self.profile, self.hooks_cfg = profile, hooks_cfg

        def slice_ms(self, step):
            return I.HOOK_STEP_BUDGETS_MS.get(self.profile, {}).get(step, 0)

        def remaining_ms(self):
            return 10_000

    safety_mod.Deadline = _FakeDeadline  # type: ignore[attr-defined]

    # judge -----------------------------------------------------------
    worker_mod = _mod("hearmemory.judge.worker")
    worker_mod.calls = []  # type: ignore[attr-defined]

    def _run_pipeline(store, cfg, deadline_s, use_jev, mode):
        worker_mod.calls.append(("run_pipeline", mode, use_jev))  # type: ignore[attr-defined]
        return {"ran": True, "mode": mode, "candidates": 0, "judgments": 0}

    def _spawn_background(root, launched_by=None):
        worker_mod.calls.append(("spawn_background", launched_by))  # type: ignore[attr-defined]
        return True

    def _stop_worker(root, timeout_s):
        worker_mod.calls.append(("stop_worker", timeout_s))  # type: ignore[attr-defined]
        return False

    worker_mod.run_pipeline = _run_pipeline  # type: ignore[attr-defined]
    worker_mod.spawn_background = _spawn_background  # type: ignore[attr-defined]
    worker_mod.stop_worker = _stop_worker  # type: ignore[attr-defined]

    jev_mod = _mod("hearmemory.judge.jev")
    jev_mod.jev_capability = lambda cfg, environ: (jev_capable, None if jev_capable else jev_reason)  # type: ignore

    scope_mod = _mod("hearmemory.judge.scope")
    scope_mod.scope_facts = lambda observations, run_a, run_b, watched_paths: I.ScopeFacts(  # type: ignore
        ts_a=now_ts(), ts_b=now_ts())

    project_index_mod = _mod("hearmemory.judge.project_index")

    class _FakeProjectIndex:
        def resolve(self, kind, surface):
            return []

        def files(self):
            return []

    project_index_mod.ProjectIndex = _FakeProjectIndex  # type: ignore[attr-defined]

    # memory ---------------------------------------------------------------
    build_mod = _mod("hearmemory.memory.build")

    def _load_or_rebuild(store, cfg, allow_rebuild=True, deadline_s=None):
        n = sum(1 for _ in store.iter_observations())
        issues = store.read_state("__fake_issues__") or {}
        return I.MemoryState(built_ts=now_ts(), observation_count=n,
                              issues={k: I.Issue.from_dict(v) for k, v in issues.items()})

    build_mod.load_or_rebuild = _load_or_rebuild  # type: ignore[attr-defined]
    build_mod.MemoryBuilder = object  # type: ignore[attr-defined]

    recall_mod = _mod("hearmemory.memory.recall")

    def _recall(state, store, query, cfg):
        return I.RecallResult(query=query.query, items=[], text=f"hearmemory: recall for {query.query!r} (0 item(s))")

    recall_mod.recall = _recall  # type: ignore[attr-defined]

    brief_mod = _mod("hearmemory.memory.brief")

    def _build_brief(state, store, req, cfg):
        return I.Brief(text="", items=[], memory_as_of=state.built_ts)

    brief_mod.build_brief = _build_brief  # type: ignore[attr-defined]

    precommit_mod = _mod("hearmemory.memory.precommit")

    def _check(state, store, req, cfg):
        # Test hook: payload text may ask for a specific decision so tests can exercise
        # every exit code (EXIT_BLOCKED for hold/block) without a real rule/Jev engine.
        decision = "allow"
        warnings: List[I.CheckWarning] = []
        if "TRIGGER_BLOCK" in (req.payload_text or ""):
            decision = "block"
            warnings.append(I.CheckWarning(kind="relies_on_refuted", item_key="k1", text="relies on a refuted claim"))
        elif "TRIGGER_HOLD" in (req.payload_text or ""):
            decision = "hold"
            warnings.append(I.CheckWarning(kind="unresolved_issue", item_key="k2", text="an issue is still open"))
        return I.CheckResult(decision=decision, warnings=warnings, text=f"hearmemory check ({req.action}): {decision}",
                              mode=req.mode)

    precommit_mod.check = _check  # type: ignore[attr-defined]

    # host ----------------------------------------------------------------
    install_mod = _mod("hearmemory.host.install")
    install_mod.calls = []  # type: ignore[attr-defined]

    def _install(root, hosts, cfg, **kw):
        install_mod.calls.append(("install", list(hosts), kw))  # type: ignore[attr-defined]
        recs = [I.InstallRecord(path=f".hearmemory/host/{h}/launch.sh", action="created", host=h) for h in hosts]
        return I.InstallManifest(root=str(root), python=kw.get("python") or sys.executable, created_ts=now_ts(),
                                  records=recs)

    def _uninstall(root, purge=False):
        install_mod.calls.append(("uninstall", purge))  # type: ignore[attr-defined]
        notes = ["removed .hearmemory/host/claude/mcp.json"]
        if purge:
            registry.purge(root)
            notes.append("purged .hearmemory")
        return notes

    install_mod.install = _install  # type: ignore[attr-defined]
    install_mod.uninstall = _uninstall  # type: ignore[attr-defined]

    codex_mod = _mod("hearmemory.host.codex")
    codex_mod.import_rollouts = lambda store, cfg, since=None, session=None: 0  # type: ignore[attr-defined]

    return mods


class InstalledFakeModules:
    """Context manager: installs `make_fake_modules()` into `sys.modules` and
    restores whatever was there before on exit (so CLI/MCP tests never leak fake
    core/memory/judge/host modules into other test files)."""

    def __init__(self, registry: Optional[FakeRegistry] = None, **kw):
        self.registry = registry or FakeRegistry()
        self.kw = kw
        self._saved: Dict[str, Any] = {}

    def __enter__(self) -> "InstalledFakeModules":
        mods = make_fake_modules(self.registry, **self.kw)
        for name, mod in mods.items():
            self._saved[name] = sys.modules.get(name, _MISSING)
            sys.modules[name] = mod
        self.mods = mods
        return self

    def __exit__(self, *exc):
        for name, prev in self._saved.items():
            if prev is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prev
        return False


_MISSING = object()
