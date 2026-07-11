import type { TemplateDesign } from "./types";

export const DESIGNS: TemplateDesign[] = [
  { key: "contact", name: "Contact Form", category: "Contact", style: "modern", accent: "#2563eb", heading: "New contact message", intro: "Someone reached out through your contact form." },
  { key: "feedback", name: "Feedback / NPS", category: "Feedback", style: "card", accent: "#7c3aed", heading: "New feedback received", intro: "A customer just shared feedback with you." },
  { key: "support", name: "Support Ticket", category: "Support", style: "accent", accent: "#dc2626", heading: "New support request", intro: "A new support ticket was submitted." },
  { key: "bug", name: "Bug Report", category: "Support", style: "dark", accent: "#f97316", heading: "New bug report", intro: "Someone reported an issue." },
  { key: "waitlist", name: "Waitlist Signup", category: "Growth", style: "branded", accent: "#0ea5e9", heading: "New waitlist signup", intro: "A new person joined your waitlist." },
  { key: "newsletter", name: "Newsletter Subscribe", category: "Growth", style: "minimal", accent: "#16a34a", heading: "New subscriber", intro: "Someone subscribed to your newsletter." },
  { key: "demo", name: "Demo Request", category: "Sales", style: "branded", accent: "#2563eb", heading: "New demo request", intro: "A prospect requested a demo." },
  { key: "quote", name: "Quote Request", category: "Sales", style: "receipt", accent: "#0891b2", heading: "New quote request", intro: "Someone requested a quote." },
  { key: "booking", name: "Booking / Appointment", category: "Scheduling", style: "card", accent: "#9333ea", heading: "New booking request", intro: "A new appointment was requested." },
  { key: "rsvp", name: "Event RSVP", category: "Events", style: "branded", accent: "#db2777", heading: "New RSVP", intro: "Someone responded to your event." },
  { key: "job", name: "Job Application", category: "HR", style: "classic", accent: "#1d4ed8", heading: "New job application", intro: "A candidate applied for a role." },
  { key: "lead", name: "Lead Capture", category: "Sales", style: "modern", accent: "#059669", heading: "New lead captured", intro: "A new lead came in." },
  { key: "partnership", name: "Partnership Inquiry", category: "Sales", style: "accent", accent: "#4f46e5", heading: "New partnership inquiry", intro: "Someone is interested in partnering." },
  { key: "donation", name: "Donation", category: "Nonprofit", style: "receipt", accent: "#16a34a", heading: "New donation", intro: "A donation was submitted." },
  { key: "survey", name: "Survey Response", category: "Feedback", style: "card", accent: "#0d9488", heading: "New survey response", intro: "A survey response was submitted." },
  { key: "order", name: "Order Request", category: "Commerce", style: "receipt", accent: "#ca8a04", heading: "New order request", intro: "A new order request arrived." },
  { key: "review", name: "Review / Testimonial", category: "Feedback", style: "classic", accent: "#d97706", heading: "New review", intro: "Someone left a review." },
  { key: "beta", name: "Beta Access Request", category: "Growth", style: "dark", accent: "#8b5cf6", heading: "New beta request", intro: "Someone requested beta access." },
  { key: "abandoned", name: "Abandoned Form Follow-up", category: "Growth", style: "minimal", accent: "#ef4444", heading: "Incomplete submission", intro: "A partially completed submission was captured." },
  { key: "generic", name: "Generic Submission", category: "General", style: "minimal", accent: "#334155", heading: "New form submission", intro: "You received a new submission." },
  { key: "branded-card", name: "Branded Card", category: "General", style: "branded", accent: "#111827", heading: "New submission", intro: "A new submission was received." },
  { key: "dark-card", name: "Dark Card", category: "General", style: "dark", accent: "#22d3ee", heading: "New submission", intro: "A new submission was received." },
  { key: "invoice", name: "Invoice Style", category: "Commerce", style: "receipt", accent: "#0f766e", heading: "New request", intro: "A new request was submitted." },
  { key: "plain", name: "Plain & Simple", category: "General", style: "minimal", accent: "#6b7280", heading: "New submission", intro: "You have a new form submission." },
];

const byKey = new Map(DESIGNS.map((d) => [d.key, d]));

export function getDesign(key: string): TemplateDesign | undefined {
  return byKey.get(key);
}

export function listDesigns(): TemplateDesign[] {
  return DESIGNS;
}

export const DEFAULT_DESIGN_KEY = "contact";
