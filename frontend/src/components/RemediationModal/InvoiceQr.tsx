import { useEffect, useState } from "react";
import { qrDataUrl } from "../../lib/qr";

type Source = { kind: "paypal" | "local"; src: string } | { kind: "pending" } | { kind: "none" };

function paypalSrc(base64: string): string {
  return base64.startsWith("data:") ? base64 : `data:image/png;base64,${base64}`;
}

/** Resolve to true only if the image decodes and is big enough to be a real QR. */
function usable(src: string): Promise<boolean> {
  return new Promise((resolve) => {
    const img = new Image();
    img.onload = () => resolve(img.naturalWidth >= 21 && img.naturalHeight >= 21);
    img.onerror = () => resolve(false);
    img.src = src;
  });
}

/**
 * Shows PayPal's QR when it is a real image; otherwise draws one in the browser from
 * the payer-view link (plan Fallback B: /generate-qr-code can fail or return junk).
 */
export function InvoiceQr({ base64, payerUrl, size = 176 }: { base64: string | null | undefined; payerUrl: string; size?: number }) {
  const [source, setSource] = useState<Source>({ kind: "pending" });

  useEffect(() => {
    let alive = true;
    setSource({ kind: "pending" });
    (async () => {
      if (base64) {
        const src = paypalSrc(base64);
        if (await usable(src)) {
          if (alive) setSource({ kind: "paypal", src });
          return;
        }
      }
      if (!payerUrl) {
        if (alive) setSource({ kind: "none" });
        return;
      }
      try {
        const src = await qrDataUrl(payerUrl, size * 2);
        if (alive) setSource({ kind: "local", src });
      } catch {
        if (alive) setSource({ kind: "none" });
      }
    })();
    return () => {
      alive = false;
    };
  }, [base64, payerUrl, size]);

  return (
    <figure className="flex flex-col items-center">
      <div className="rounded border border-paper-line bg-white p-2" style={{ width: size + 18, height: size + 18 }}>
        {source.kind === "paypal" || source.kind === "local" ? (
          <img
            src={source.src}
            alt="QR code that opens the PayPal invoice"
            width={size}
            height={size}
            data-testid={source.kind === "local" ? "qr-local" : "qr-paypal"}
            className="h-full w-full [image-rendering:pixelated]"
          />
        ) : source.kind === "pending" ? (
          <div className="h-full w-full animate-pulse rounded bg-paper" />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-center text-xs text-ink-muted">Use the payer link</div>
        )}
      </div>
      <figcaption className="mt-2 max-w-[200px] text-center text-[12px] leading-snug text-ink-muted">
        {source.kind === "local" ? "Drawn from the invoice link because PayPal's code was unusable" : source.kind === "paypal" ? "Scan to pay on PayPal" : " "}
      </figcaption>
    </figure>
  );
}
