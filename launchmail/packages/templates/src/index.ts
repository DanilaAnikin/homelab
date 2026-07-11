import { renderLayout } from "./layouts";
import {
  buildFieldsHtml,
  buildFieldsText,
  interpolate,
  interpolateHtml,
  visibleFields,
} from "./render-helpers";
import { DEFAULT_DESIGN_KEY, getDesign } from "./registry";
import type { RenderedEmail, RenderInput } from "./types";

export { DESIGNS, getDesign, listDesigns, DEFAULT_DESIGN_KEY } from "./registry";
export { renderBlocks, renderCustomHtml } from "./blocks";
export { escapeHtml } from "./render-helpers";
export type { EmailBlock } from "./blocks";
export type {
  TemplateDesign,
  TemplateStyle,
  FormBranding,
  RenderInput,
  RenderedEmail,
} from "./types";

export const SAMPLE_DATA: Record<string, string> = {
  name: "Jane Doe",
  email: "jane@example.com",
  message: "Hi! I'd love to learn more about your product.",
  company: "Acme Inc",
};

export function renderForm(input: RenderInput): RenderedEmail {
  const design = input.design;
  const accent = input.branding?.accent || design.accent;
  const brandName = input.branding?.brandName || "LaunchMail";
  const footer = input.branding?.footer || "Sent via LaunchMail";
  const submittedAt =
    input.submittedAt || "just now";

  const dark = input.branding?.theme
    ? input.branding.theme === "dark"
    : design.style === "dark";

  const fieldsHtml = buildFieldsHtml(input.data, { dark });

  const subject = input.subjectTemplate
    ? interpolate(input.subjectTemplate, input.data) ||
      `${design.name} submission`
    : `${design.heading}`;

  const html = input.customHtml
    ? interpolateHtml(
        input.customHtml,
        {
          ...input.data,
          _fields: fieldsHtml,
          _brand: brandName,
          _heading: design.heading,
        },
        // Pre-built, already-safe HTML fragments — inject raw, don't escape.
        ["_fields", "_brand", "_heading"],
      )
    : renderLayout(dark, {
        brandName,
        accent,
        heading: design.heading,
        intro: design.intro,
        fieldsHtml,
        footer,
        submittedAt,
      });

  const text = `${design.heading}\n\n${buildFieldsText(input.data)}\n\n${footer}`;

  return { subject, html, text };
}

export function renderPreview(
  designKey: string,
  theme?: "light" | "dark",
): RenderedEmail {
  const design = getDesign(designKey) ?? getDesign(DEFAULT_DESIGN_KEY)!;
  return renderForm({
    design,
    data: SAMPLE_DATA,
    branding: theme ? { theme } : undefined,
    submittedAt: "just now",
  });
}

export { visibleFields };
