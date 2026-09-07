import type { Metadata } from "next";
import { IBM_Plex_Mono, Inter } from "next/font/google";
import "./globals.css";

// A grotesque for prose, a monospace for anything the machine produced. The
// distinction is load-bearing here: it is how a reader tells the page's own
// words from the agent's output.
const sans = Inter({ variable: "--font-sans-stack", subsets: ["latin"] });
const mono = IBM_Plex_Mono({
  variable: "--font-mono-stack",
  subsets: ["latin"],
  weight: ["400", "500"],
});

export const metadata: Metadata = {
  title: "Feature Pilot",
  description:
    "An autonomous software engineer. Give it a public GitHub issue; watch it plan, patch, test, repair, and open the pull request.",
};

// Typed explicitly rather than with Next's generated `LayoutProps`. Those
// globals are written into .next/types during a build, so `tsc --noEmit` on a
// clean checkout cannot see them — it passes locally only because a build has
// already run, and fails the moment CI does it in the right order.
export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${sans.variable} ${mono.variable} h-full antialiased`}>
      <body className="min-h-full flex flex-col">{children}</body>
    </html>
  );
}
