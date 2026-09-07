"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Masthead } from "@/components/Masthead";
import { KeysModal, useKeys } from "@/components/KeysModal";
import { ApiError, startRun } from "@/lib/api";
import type { StartRunBody } from "@/lib/types";

const ISSUE_URL =
  /^https?:\/\/(?:www\.)?github\.com\/[^/\s]+\/[^/\s]+\/issues\/\d+\/?(?:[?#].*)?$/;

const EXAMPLE = "https://github.com/pallets/click/issues/2951";

export default function Home() {
  const router = useRouter();
  const [url, setUrl] = useState("");
  const [approve, setApprove] = useState(true);
  const [draft, setDraft] = useState(true);
  const [keysOpen, setKeysOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const keys = useKeys();

  const valid = ISSUE_URL.test(url.trim());
  const own = keys.anthropic.trim() !== "" || keys.github.trim() !== "";

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!valid || busy) return;
    setBusy(true);
    setError(null);

    const body: StartRunBody = {
      issue_url: url.trim(),
      // Approving interactively means *not* auto-approving on the server.
      auto_approve: !approve,
      auto_publish: false,
      draft,
    };
    // Omitted entirely when blank: sending "" would claim a key exists.
    if (keys.anthropic.trim()) body.anthropic_api_key = keys.anthropic.trim();
    if (keys.github.trim()) body.github_token = keys.github.trim();

    try {
      const run = await startRun(body);
      router.push(`/runs/${run.run_id}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Something went wrong.");
      setBusy(false);
    }
  }

  return (
    <>
      <Masthead />
      <main className="mx-auto w-full max-w-2xl grow px-6 py-16 sm:py-24">
        <h1 className="text-[28px] font-semibold leading-[1.15] tracking-tight text-ink sm:text-[34px]">
          Give it a GitHub issue.
          <br />
          <span className="text-ink-muted">Watch it open the pull request.</span>
        </h1>

        <p className="mt-5 max-w-xl text-[15px] leading-relaxed text-ink-secondary">
          It clones the repository, reads the code, writes a plan for you to approve,
          edits inside a throwaway container, runs the test suite, repairs its own
          failures, and pushes a branch. Every step is shown as it happens.
        </p>

        <form onSubmit={submit} className="mt-10">
          <label htmlFor="issue" className="block text-[12px] uppercase tracking-[0.08em] text-ink-muted">
            Public issue URL
          </label>
          <input
            id="issue"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder={EXAMPLE}
            spellCheck={false}
            className="mt-2 w-full border-b border-rule-strong bg-transparent pb-2 font-mono text-[14px] outline-none transition-colors placeholder:text-ink-muted/50 focus:border-accent"
          />
          {url.trim() !== "" && !valid && (
            <p className="mt-2 text-[12.5px] text-bad">
              That needs to be a github.com issue link — owner, repository, then{" "}
              <span className="font-mono">/issues/</span> and a number.
            </p>
          )}

          <div className="mt-6 space-y-3 text-[13.5px]">
            <label className="flex cursor-pointer items-start gap-2.5">
              <input
                type="checkbox"
                checked={approve}
                onChange={(e) => setApprove(e.target.checked)}
                className="mt-[3px] accent-accent"
              />
              <span className="text-ink-secondary">
                <span className="text-ink">Let me approve the plan</span> — it stops and
                shows you what it intends to do before touching any code.
              </span>
            </label>
            <label className="flex cursor-pointer items-start gap-2.5">
              <input
                type="checkbox"
                checked={draft}
                onChange={(e) => setDraft(e.target.checked)}
                className="mt-[3px] accent-accent"
              />
              <span className="text-ink-secondary">
                <span className="text-ink">Open the pull request as a draft</span> — so
                nobody is notified while you read it.
              </span>
            </label>
          </div>

          <div className="mt-8 flex flex-wrap items-center gap-x-5 gap-y-3">
            <button
              type="submit"
              disabled={!valid || busy}
              className="bg-ink px-5 py-2.5 text-[13.5px] font-medium text-paper transition-opacity hover:opacity-85 disabled:cursor-not-allowed disabled:opacity-30"
            >
              {busy ? "Starting…" : "Solve it"}
            </button>
            <button
              type="button"
              onClick={() => setKeysOpen(true)}
              className="text-[13px] text-ink-secondary underline decoration-rule-strong underline-offset-4 hover:text-ink"
            >
              {own ? "Using your keys" : "Use your own keys"}
            </button>
            {url === "" && (
              <button
                type="button"
                onClick={() => setUrl(EXAMPLE)}
                className="text-[13px] text-ink-muted underline decoration-rule underline-offset-4 hover:text-ink"
              >
                Try an example
              </button>
            )}
          </div>

          {error && (
            <p className="mt-6 border-l-2 border-bad pl-3 text-[13.5px] leading-relaxed text-bad">
              {error}
            </p>
          )}
        </form>

        <p className="mt-14 border-t border-rule pt-5 text-[12.5px] leading-relaxed text-ink-muted">
          Runs on this host&rsquo;s own credentials by default, which means a daily
          spending ceiling and draft pull requests authored by its owner. Supply your
          own keys and the run is yours — billed to you, authored by you, and kept only
          for that run.
        </p>
      </main>

      <KeysModal open={keysOpen} onClose={() => setKeysOpen(false)} />
    </>
  );
}
