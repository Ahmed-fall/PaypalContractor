import { PageHeader } from "../components/common/Card";

/**
 * Route slot for Person 5's Leash screens (plan A.7). P5 owns the real pages under
 * src/components/Leash/; swap the element in App.tsx when they land. The shared
 * client already serves every Leash route from fixtures in mock mode.
 */
export default function LeashPlaceholder({ title, route }: { title: string; route: string }) {
  return (
    <div>
      <PageHeader eyebrow="Leash" title={title} />
      <div className="rounded-xl border border-dashed border-slate-300 bg-white p-8 text-sm text-ink-muted">
        <p className="font-medium text-ink">This screen is built by the Leash owner (Person 5).</p>
        <p className="mt-2">
          Route <code className="font-mono text-ink">{route}</code> is wired in the app shell. The shared API client
          (<code className="font-mono">src/api/client.ts</code>) already exposes every Leash endpoint and serves them from
          <code className="font-mono"> shared/fixtures/leash</code> in mock mode.
        </p>
      </div>
    </div>
  );
}
