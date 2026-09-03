// frontend/src/store.jsx — global demo state: mode + per-case replay position.
// Replay states are NEVER fabricated here: every state shown in the UI comes from
// the backend (`state_after` on timeline events / `?at=` on the dag endpoint).

import React, { createContext, useContext, useEffect, useMemo, useRef, useState, useCallback } from 'react';
import { api } from './api.js';

const Ctx = createContext(null);
export const useDemo = () => useContext(Ctx);

const AUTO_DELAY_MS = 1400;

export function DemoProvider({ children }) {
  const [mode, setModeState] = useState('replay'); // replay | live
  const [view, setViewState] = useState(() => {
    const v = new URLSearchParams(window.location.search).get('view');
    return ['operations', 'presentation', 'evidence'].includes(v) ? v : 'operations';
  }); // operations (default) | presentation | evidence — deep-linkable via ?view=
  const setView = useCallback((v) => {
    setViewState(v);
    const u = new URL(window.location);
    u.searchParams.set('view', v);
    window.history.replaceState(null, '', u);
  }, []);
  const [modes, setModes] = useState(null);
  const [health, setHealth] = useState(null);
  const [cases, setCases] = useState(null);
  const [error, setError] = useState(null);
  const [timeline, setTimeline] = useState(null); // cached timeline of activeCase (for demobar current-event info)

  // replay position per case: number of events applied (0..N)
  const [pos, setPos] = useState({});
  const [playing, setPlaying] = useState(false);
  const [activeCase, setActiveCase] = useState('pr2-high-risk-human-gate');

  const refreshModes = useCallback(async (m = mode) => {
    try {
      const r = await api(`/api/modes?mode=${m}`);
      setModes(r);
      setError(null);
      return r;
    } catch (e) {
      setError(e);
      return null;
    }
  }, [mode]);

  useEffect(() => {
    (async () => {
      const h = await api('/api/health').catch((e) => {
        setError(e);
        return null;
      });
      setHealth(h);
      const c = await api('/api/cases').catch(() => null);
      setCases(c);
      refreshModes('replay');
    })();
    const t = setInterval(() => api('/api/health').then(setHealth).catch(() => {}), 30000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => { refreshModes(mode); }, [mode]); // eslint-disable-line

  const totalEvents = useMemo(() => {
    const c = cases?.cases?.find((x) => x.case_id === activeCase);
    return c?.events ?? 0;
  }, [cases, activeCase]);

  const idx = pos[activeCase] ?? 0;

  const setIdx = useCallback((n) => {
    setPos((p) => ({ ...p, [activeCase]: Math.max(0, Math.min(n, totalEvents || n)) }));
  }, [activeCase, totalEvents]);

  // Playback rules (presentation contract):
  //  - auto-play pauses at the HUMAN GATE event (HUMAN_SECURITY_REVIEW_REQUIRED);
  //  - for PR #3, once the REJECTION event is reached, playback can never continue
  //    (no resume past a human rejection — the same rule the runtime enforces).
  const rejectionIdx = useMemo(() => {
    const evs = timeline?.events;
    if (!evs) return -1;
    return evs.findIndex((e) => e.summary.includes('HUMAN_SECURITY_REJECTED'));
  }, [timeline]);
  const playBlocked = activeCase === 'pr3-high-risk-human-reject' && rejectionIdx >= 0 && idx >= rejectionIdx + 1;

  const next = useCallback(() => {
    if (playBlocked) return; // PR #3 rejected terminal: no advancing playback
    setPos((p) => {
      const cur = p[activeCase] ?? 0;
      const total = cases?.cases?.find((x) => x.case_id === activeCase)?.events ?? 0;
      return { ...p, [activeCase]: Math.min(cur + 1, total) };
    });
  }, [activeCase, cases, playBlocked]);

  const setPlayingGuarded = useCallback((v) => {
    const val = typeof v === 'function' ? v(playing) : v;
    if (val && playBlocked) return; // rejected terminal: no resume
    setPlaying(val);
  }, [playing, playBlocked]);

  // auto-play
  const timer = useRef(null);
  useEffect(() => {
    if (playing) {
      timer.current = setInterval(() => {
        setPos((p) => {
          const cur = p[activeCase] ?? 0;
          const total = cases?.cases?.find((x) => x.case_id === activeCase)?.events ?? 0;
          if (cur >= total) {
            setPlaying(false);
            return p;
          }
          const nxt = cur + 1;
          const revealed = timeline?.events?.[nxt - 1];
          if (revealed && revealed.summary.includes('HUMAN_SECURITY_REJECTED')) {
            setPlaying(false); // rejection reached — hard stop
          } else if (revealed && revealed.summary.includes('HUMAN_SECURITY_REVIEW_REQUIRED')) {
            setPlaying(false); // human gate reached — auto pause
          }
          return { ...p, [activeCase]: nxt };
        });
      }, AUTO_DELAY_MS);
    }
    return () => clearInterval(timer.current);
  }, [playing, activeCase, cases, timeline]);

  const jumpToGate = useCallback(async () => {
    try {
      const tl = await api(`/api/cases/${activeCase}/timeline`);
      const gi = tl.events.findIndex((e) => e.summary.includes('HUMAN_SECURITY_REVIEW_REQUIRED'));
      if (gi >= 0) setPos((p) => ({ ...p, [activeCase]: gi + 1 }));
    } catch { /* surfaced elsewhere */ }
  }, [activeCase]);

  const jumpToReject = useCallback(async () => {
    try {
      const tl = await api(`/api/cases/${activeCase}/timeline`);
      const ri = tl.events.findIndex((e) => e.summary.includes('HUMAN_SECURITY_REJECTED'));
      if (ri >= 0) {
        setPlaying(false);
        setPos((p) => ({ ...p, [activeCase]: ri + 1 }));
      }
    } catch { /* surfaced elsewhere */ }
  }, [activeCase]);

  const reset = useCallback(() => {
    setPlaying(false);
    setPos((p) => ({ ...p, [activeCase]: 0 }));
  }, [activeCase]);

  const setMode = useCallback((m) => {
    setPlaying(false);
    setModeState(m);
  }, []);

  // keyboard shortcuts (space/→/g/r) — skip when typing in inputs
  useEffect(() => {
    const h = (e) => {
      if (e.target.closest('input,textarea,select')) return;
      if (e.code === 'Space') { e.preventDefault(); setPlaying((p) => !p); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); next(); }
      else if (e.key === 'g' || e.key === 'G') jumpToGate();
      else if (e.key === 'r' || e.key === 'R') reset();
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [next, jumpToGate, reset]);

  useEffect(() => {
    let alive = true;
    setTimeline(null);
    api(`/api/cases/${activeCase}/timeline`).then((r) => alive && setTimeline(r)).catch(() => {});
    return () => { alive = false; };
  }, [activeCase]);

  const currentEvent = idx > 0 ? (timeline?.events?.[idx - 1] ?? null) : null;

  const value = {
    mode, setMode, view, setView, modes, health, cases, error,
    activeCase, setActiveCase,
    idx, setIdx, next, reset, jumpToGate, jumpToReject, playing, setPlaying: setPlayingGuarded, playBlocked,
    totalEvents, timeline, currentEvent,
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}
