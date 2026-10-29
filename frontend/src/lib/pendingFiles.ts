/** In-memory hand-off of the uploaded File to the review route (for the PDF preview). */
const files = new Map<string, File>();
export const pendingFiles = {
  set: (contractId: string, file: File) => files.set(contractId, file),
  get: (contractId: string) => files.get(contractId) ?? null,
};
