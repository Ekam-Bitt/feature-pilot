import { RunView } from "@/components/RunView";

export default async function RunPage({ params }: PageProps<"/runs/[id]">) {
  const { id } = await params;
  return <RunView runId={id} />;
}
