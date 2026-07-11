import { describe, expect, it } from "vitest";
import {
  DESIGNS,
  getDesign,
  renderCustomHtml,
  renderForm,
  renderPreview,
} from "../src/index";

describe("template registry", () => {
  it("has at least 24 unique designs", () => {
    expect(DESIGNS.length).toBeGreaterThanOrEqual(24);
    const keys = new Set(DESIGNS.map((d) => d.key));
    expect(keys.size).toBe(DESIGNS.length);
  });

  it("every design renders valid html with sample data", () => {
    for (const design of DESIGNS) {
      const out = renderPreview(design.key);
      expect(out.html).toContain("<!doctype html>");
      expect(out.html).toContain("Jane Doe");
      expect(out.subject.length).toBeGreaterThan(0);
      expect(out.text).toContain("Jane Doe");
    }
  });
});

describe("renderForm", () => {
  const design = getDesign("contact")!;

  it("escapes HTML in submitted values (no injection)", () => {
    const out = renderForm({
      design,
      data: { message: "<script>alert(1)</script>" },
    });
    expect(out.html).not.toContain("<script>alert(1)</script>");
    expect(out.html).toContain("&lt;script&gt;");
  });

  it("hides internal/honeypot fields", () => {
    const out = renderForm({
      design,
      data: { name: "Bob", _gotcha: "spam", _next: "/thanks" },
    });
    expect(out.html).toContain("Bob");
    expect(out.html).not.toContain("spam");
  });

  it("interpolates the subject template from data", () => {
    const out = renderForm({
      design,
      subjectTemplate: "New message from {{name}}",
      data: { name: "Carol" },
    });
    expect(out.subject).toBe("New message from Carol");
  });

  it("supports a custom html override with {{_fields}}", () => {
    const out = renderForm({
      design,
      customHtml: "<div>{{_brand}}: {{_fields}}</div>",
      branding: { brandName: "Acme" },
      data: { email: "x@y.com" },
    });
    expect(out.html).toContain("Acme:");
    expect(out.html).toContain("x@y.com");
  });

  it("escapes submitter data interpolated into custom html (no injection)", () => {
    const out = renderForm({
      design,
      customHtml: "<div>Hello {{name}} {{bio}}</div>",
      data: {
        name: "<script>alert(1)</script>",
        bio: '<img src=x onerror="alert(2)">',
      },
    });
    expect(out.html).not.toContain("<script");
    expect(out.html).not.toContain("<img");
    expect(out.html).toContain("&lt;script&gt;");
    expect(out.html).toContain("&lt;img");
    // The author-controlled template markup is preserved verbatim.
    expect(out.html).toContain("<div>Hello");
  });

  it("does not double-escape the pre-built _fields fragment", () => {
    const out = renderForm({
      design,
      customHtml: "<div>{{_fields}}</div>",
      data: { name: "Dana" },
    });
    // _fields is real HTML and must be injected raw (its <table> stays intact).
    expect(out.html).toContain("<table");
    expect(out.html).not.toContain("&lt;table");
    expect(out.html).toContain("Dana");
  });
});

describe("renderCustomHtml", () => {
  it("escapes submitter data values to prevent HTML injection", () => {
    const html = renderCustomHtml("<p>Hi {{name}}, {{msg}}</p>", {
      name: "<script>alert(1)</script>",
      msg: '<img src=x onerror="alert(2)">',
    });
    expect(html).not.toContain("<script");
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;script&gt;");
    expect(html).toContain("&lt;img");
    // Author template markup is left untouched.
    expect(html).toContain("<p>Hi");
  });

  it("leaves plain values intact", () => {
    const html = renderCustomHtml("<p>Hello {{name}}</p>", { name: "Dana" });
    expect(html).toBe("<p>Hello Dana</p>");
  });
});
