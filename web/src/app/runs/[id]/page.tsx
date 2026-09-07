import { RunView } from "@/components/RunView";

// `params` is a promise in the App Router, and spelled out here for the same
// reason as the layout: Next's `PageProps` only exists after a build has
// generated it.
export default async function RunPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <RunView runId={id} />;
}
