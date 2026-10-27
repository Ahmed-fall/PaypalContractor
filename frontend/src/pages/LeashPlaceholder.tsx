import { PageHeader } from "../components/common/Card";

/**
 * Route slot for Person 5's Leash screens (plan A.7). P5 owns the real pages under
 * src/components/Leash/; swap the element in App.tsx when they land. The shared
 * client already serves every Leash route from fixtures in mock mode.
 */
export default function LeashPlaceholder({ title, route }: { title: string; route: string }) {
  return (
    <div>
      <PageHeader title={title} />
      <div className="max-w-[68ch] rounded-lg border border-dashed border-paper-line bg-white p-8 text-[14px] leading-relaxed text-ink-muted">
        <p className="font-medium text-ink">This screen is built by the Leash owner (Person 5).</p>
        <p className="mt-2">
          Route <code className="num text-ink">{route}</code> is wired in the app shell. The shared API client
          (<code className="num">src/api/client.ts</code>) already exposes every Leash endpoint and serves them from
          <code className="num"> shared/fixtures/leash</code> in mock mode.
        </p>
      </div>
    </div>
  );
}
