export type TemplateStyle =
  | "minimal"
  | "card"
  | "branded"
  | "dark"
  | "modern"
  | "classic"
  | "accent"
  | "receipt";

export interface TemplateDesign {
  key: string;
  name: string;
  category: string;
  description?: string;
  style: TemplateStyle;
  accent: string;
  heading: string;
  intro: string;
}

export interface FormBranding {
  brandName?: string;
  accent?: string;
  footer?: string;
  theme?: "light" | "dark";
}

export interface RenderInput {
  design: TemplateDesign;
  branding?: FormBranding | null;
  subjectTemplate?: string | null;
  customHtml?: string | null;
  data: Record<string, string>;
  submittedAt?: string;
}

export interface RenderedEmail {
  subject: string;
  html: string;
  text: string;
}

export interface LayoutContext {
  brandName: string;
  accent: string;
  heading: string;
  intro: string;
  fieldsHtml: string;
  footer: string;
  submittedAt: string;
}
