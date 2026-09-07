/**
 * The API client.
 *
 * Thin on purpose: the interesting behaviour is on the server, and every
 * endpoint here already returns exactly what a screen needs. What this file
 * does own is error phrasing — the API's `detail` strings are written to be
 * read by a person ("supply your own Anthropic key to run now"), so they are
 * carried through verbatim rather than replaced with a status code.
 */

import type { RunArtifacts, RunStatus, StartRunBody } from "@/lib/types";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Defaults to the local API so a fresh clone runs with no env file. */
export const apiBase = (): string =>
  (process.env.NEXT_PUBLIC_FP_API_URL ?? "http://localhost:8080").replace(/\/$/, "");

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${apiBase()}${path}`, {
      ...init,
      headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch {
    // A failed fetch is nearly always the API being down or the page being
    // HTTPS while the API is HTTP — both worth saying out loud, because the
    // browser console's version of this is famously unhelpful.
    throw new ApiError(
      `Could not reach the agent at ${apiBase()}. Is it running, and served over the same scheme as this page?`,
      0,
    );
  }

  const text = await response.text();
  const body = text ? (JSON.parse(text) as Record<string, unknown>) : {};

  if (!response.ok) {
    const detail = typeof body.detail === "string" ? body.detail : response.statusText;
    throw new ApiError(detail, response.status);
  }
  return body as T;
}

export const startRun = (body: StartRunBody): Promise<RunStatus> =>
  call<RunStatus>("/runs", { method: "POST", body: JSON.stringify(body) });

export const getRun = (runId: string): Promise<RunStatus> =>
  call<RunStatus>(`/runs/${runId}`);

export const getArtifacts = (runId: string): Promise<RunArtifacts> =>
  call<RunArtifacts>(`/runs/${runId}/artifacts`);

export const approveRun = (
  runId: string,
  verdict: "approve" | "reject",
  feedback: string,
  answers: string[],
): Promise<{ verdict: string }> =>
  call(`/runs/${runId}/approve`, {
    method: "POST",
    body: JSON.stringify({ verdict, feedback, answers }),
  });

export async function publishRun(runId: string, draft: boolean): Promise<string | null> {
  const body = await call<{ pr_url: string | null }>(`/runs/${runId}/publish`, {
    method: "POST",
    body: JSON.stringify({ draft }),
  });
  return body.pr_url;
}

export const streamUrl = (runId: string): string => `${apiBase()}/runs/${runId}/stream`;
