import { describe, expect, it } from "vitest";
import { setLang, translate } from "./i18n.js";

describe("i18n", () => {
  it("flips the document to RTL in Persian and falls back to English for unknown strings", () => {
    setLang("fa");
    expect(document.documentElement.dir).toBe("rtl");
    expect(translate("Home")).toBe("خانه");
    expect(translate("A sentence nobody translated")).toBe("A sentence nobody translated");
    expect(translate(42)).toBe(42);
    setLang("en");
    expect(document.documentElement.dir).toBe("ltr");
    expect(translate("Home")).toBe("Home");
  });
});
