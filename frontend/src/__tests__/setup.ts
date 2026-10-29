import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => {
  cleanup();
  sessionStorage.clear();
  vi.restoreAllMocks();
});

// jsdom never loads images, so make every <img>/Image fail fast unless a test overrides it.
class FailingImage {
  onload: (() => void) | null = null;
  onerror: (() => void) | null = null;
  naturalWidth = 0;
  naturalHeight = 0;
  set src(_v: string) {
    setTimeout(() => this.onerror?.(), 0);
  }
}
vi.stubGlobal("Image", FailingImage);
