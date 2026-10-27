import { Link } from "react-router-dom";

export default function NotFound() {
  return (
    <div className="mx-auto max-w-md py-20 text-center">
      <p className="text-sm font-semibold text-brand">404</p>
      <h1 className="mt-2 text-2xl font-semibold text-ink">Page not found</h1>
      <Link to="/ledger" className="mt-6 inline-block text-sm font-medium text-brand hover:underline">
        Back to the ledger
      </Link>
    </div>
  );
}
