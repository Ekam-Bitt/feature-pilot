import Link from "next/link";

export function Masthead({ subdued = false }: { subdued?: boolean }) {
  return (
    <header className="border-b border-rule">
      <div className="mx-auto flex max-w-5xl items-baseline justify-between px-6 py-5">
        <Link href="/" className="group flex items-baseline gap-3">
          <span className="text-[15px] font-medium tracking-tight text-ink">
            Feature Pilot
          </span>
          {!subdued && (
            <span className="hidden text-[13px] text-ink-muted sm:inline">
              an autonomous software engineer
            </span>
          )}
        </Link>
        <a
          href="https://github.com/Ekam-Bitt/feature-pilot"
          target="_blank"
          rel="noreferrer"
          className="text-[13px] text-ink-muted underline decoration-rule-strong underline-offset-4 transition-colors hover:text-ink"
        >
          Source
        </a>
      </div>
    </header>
  );
}
