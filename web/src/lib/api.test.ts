import { afterEach, describe, expect, it, vi } from "vitest";
import { apiBase, ApiError, getArtifacts, publishRun, startRun } from "@/lib/api";

const ok = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), { status }));

afterEach(() => vi.unstubAllGlobals());

describe("apiBase", () => {
  it("falls back to the local API so a fresh clone runs with no env file", () => {
    expect(apiBase()).toMatch(/^http:\/\/(localhost|127\.0\.0\.1):8080$/);
  });
});

describe("startRun", () => {
  it("sends the issue URL and options", async () => {
    // Typed on the mock rather than its parameters, so `mock.calls[0]` is a
    // [url, init] tuple without declaring arguments the body never reads.
    const fetchMock = vi.fn<(url: string, init: RequestInit) => Promise<Response>>(
      () => ok({ run_id: "r1" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await startRun({
      issue_url: "https://github.com/a/b/issues/1",
      auto_approve: false,
      auto_publish: false,
      draft: true,
    });

    const [, init] = fetchMock.mock.calls[0];
    const body = JSON.parse(init.body as string);
    expect(body.issue_url).toBe("https://github.com/a/b/issues/1");
    expect(body.draft).toBe(true);
  });

  it("omits credential fields entirely when the visitor supplied none", async () => {
    // Typed on the mock rather than its parameters, so `mock.calls[0]` is a
    // [url, init] tuple without declaring arguments the body never reads.
    const fetchMock = vi.fn<(url: string, init: RequestInit) => Promise<Response>>(
      () => ok({ run_id: "r1" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await startRun({
      issue_url: "https://github.com/a/b/issues/1",
      auto_approve: true,
      auto_publish: false,
      draft: true,
    });

    const [, init] = fetchMock.mock.calls[0];
    const body = JSON.parse(init.body as string);
    // Sending "" would be read as a supplied-but-blank key; the backend
    // coerces that to absent, but the wire should not claim one exists.
    expect("anthropic_api_key" in body).toBe(false);
    expect("github_token" in body).toBe(false);
  });

  it("surfaces the API's own explanation, which the UI needs verbatim", async () => {
    vi.stubGlobal("fetch", () =>
      ok({ detail: "this server's daily model budget is spent" }, 429),
    );
    await expect(
      startRun({
        issue_url: "https://github.com/a/b/issues/1",
        auto_approve: false,
        auto_publish: false,
        draft: true,
      }),
    ).rejects.toMatchObject({ status: 429, message: /daily model budget/ });
  });

  it("reports a dead API as something a reader can act on", async () => {
    vi.stubGlobal("fetch", () => Promise.reject(new TypeError("Failed to fetch")));
    await expect(
      startRun({
        issue_url: "https://github.com/a/b/issues/1",
        auto_approve: false,
        auto_publish: false,
        draft: true,
      }),
    ).rejects.toThrow(/could not reach/i);
  });
});

describe("publishRun", () => {
  it("returns the pull request URL", async () => {
    vi.stubGlobal("fetch", () => ok({ pr_url: "https://github.com/a/b/pull/9" }));
    expect(await publishRun("r1", true)).toBe("https://github.com/a/b/pull/9");
  });

  it("raises with the git failure when publishing is refused", async () => {
    vi.stubGlobal("fetch", () => ok({ detail: "publish failed: patch does not apply" }, 502));
    await expect(publishRun("r1", false)).rejects.toBeInstanceOf(ApiError);
  });
});

describe("getArtifacts", () => {
  it("treats a run with nothing stashed yet as empty, not broken", async () => {
    vi.stubGlobal("fetch", () =>
      ok({ run_id: "r1", diff: null, pr_summary: null, test_summary: null }),
    );
    expect((await getArtifacts("r1")).diff).toBeNull();
  });
});
