"use client";

import { useEffect, useMemo, useState, useSyncExternalStore } from "react";

export type Keys = { anthropic: string; github: string };

const STORE = "fp.keys";
const CHANGED = "fp.keys.changed";
const NONE: Keys = { anthropic: "", github: "" };

/**
 * sessionStorage read through useSyncExternalStore.
 *
 * This is external mutable state that does not exist on the server, which is
 * exactly what that hook is for: reading it in an effect and calling setState
 * causes the cascading render React 19 complains about, and reading it during
 * render would disagree with the prerendered HTML.
 */
function subscribe(onChange: () => void): () => void {
  window.addEventListener(CHANGED, onChange);
  return () => window.removeEventListener(CHANGED, onChange);
}

function snapshot(): string {
  try {
    return sessionStorage.getItem(STORE) ?? "";
  } catch {
    // Private modes throw on access; the server's credentials are used.
    return "";
  }
}

const serverSnapshot = (): string => "";

export function useKeys(): Keys {
  const raw = useSyncExternalStore(subscribe, snapshot, serverSnapshot);
  return useMemo(() => {
    if (!raw) return NONE;
    try {
      return JSON.parse(raw) as Keys;
    } catch {
      return NONE;
    }
  }, [raw]);
}

export function saveKeys(keys: Keys) {
  try {
    sessionStorage.setItem(STORE, JSON.stringify(keys));
  } catch {
    /* nothing to do: the run simply uses the server's credentials */
  }
  window.dispatchEvent(new Event(CHANGED));
}

/**
 * Keys for this session only.
 *
 * sessionStorage rather than localStorage, deliberately: these die with the
 * tab. They are sent once in the run's own request, and the server holds them
 * in memory for that run alone — never written to its database, never in an
 * API response, never in an event that leaves its process.
 */
export function KeysModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const stored = useKeys();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    if (open) window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  // Mounted only while open, so the form starts from what is stored without
  // an effect to copy it in.
  if (!open) return null;
  return <KeysForm initial={stored} onClose={onClose} />;
}

function KeysForm({ initial, onClose }: { initial: Keys; onClose: () => void }) {
  const [keys, setKeys] = useState<Keys>(initial);

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/25 p-6"
      role="dialog"
      aria-modal="true"
      aria-label="Use your own keys"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className="w-full max-w-lg border border-rule-strong bg-paper p-6">
        <h2 className="text-[15px] font-medium text-ink">Use your own keys</h2>
        <p className="mt-2 text-[13px] leading-relaxed text-ink-secondary">
          Then the run is billed to you and the pull request is authored by you. Kept
          for this tab only, sent once with the run, and never stored on the server.
          Leave a field blank to use this host&rsquo;s own credential.
        </p>

        <label className="mt-5 block">
          <span className="text-[12px] uppercase tracking-[0.08em] text-ink-muted">
            Anthropic API key
          </span>
          <input
            type="password"
            autoComplete="off"
            value={keys.anthropic}
            onChange={(e) => setKeys({ ...keys, anthropic: e.target.value })}
            placeholder="sk-ant-…"
            className="mt-1 w-full border border-rule-strong bg-paper px-2 py-1.5 font-mono text-[13px] outline-none focus:border-accent"
          />
        </label>

        <label className="mt-4 block">
          <span className="text-[12px] uppercase tracking-[0.08em] text-ink-muted">
            GitHub token
          </span>
          <input
            type="password"
            autoComplete="off"
            value={keys.github}
            onChange={(e) => setKeys({ ...keys, github: e.target.value })}
            placeholder="ghp_…"
            className="mt-1 w-full border border-rule-strong bg-paper px-2 py-1.5 font-mono text-[13px] outline-none focus:border-accent"
          />
          <span className="mt-1 block text-[12px] text-ink-muted">
            Needs the <span className="font-mono">repo</span> scope, to fork and open a
            pull request as you.
          </span>
        </label>

        <div className="mt-6 flex items-center gap-3">
          <button
            type="button"
            onClick={() => {
              saveKeys(keys);
              onClose();
            }}
            className="bg-ink px-4 py-2 text-[13px] font-medium text-paper hover:opacity-85"
          >
            Use these
          </button>
          <button
            type="button"
            onClick={() => {
              saveKeys(NONE);
              onClose();
            }}
            className="text-[13px] text-ink-secondary underline decoration-rule-strong underline-offset-4 hover:text-ink"
          >
            Clear
          </button>
        </div>
      </div>
    </div>
  );
}
