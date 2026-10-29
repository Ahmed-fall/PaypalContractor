import QRCode from "qrcode";

/** Fallback B: draw the payer QR in the browser from the invoice's payer-view link. */
export function qrDataUrl(text: string, size = 220): Promise<string> {
  return QRCode.toDataURL(text, {
    width: size,
    margin: 1,
    errorCorrectionLevel: "M",
    color: { dark: "#0f172a", light: "#ffffff" },
  });
}
