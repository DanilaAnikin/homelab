import {
  pgTable,
  text,
  jsonb,
  timestamp,
  uuid,
  boolean,
  index,
} from "drizzle-orm/pg-core";
import { relations } from "drizzle-orm";
import { organization, user } from "./auth-schema";
import { smtpConfigs } from "./mail-schema";
import { emailTemplates } from "./templates-schema";

export interface FormBranding {
  brandName?: string;
  accent?: string;
  footer?: string;
  theme?: "light" | "dark";
}

export interface FormSettings {
  honeypot?: boolean;
  redirectUrl?: string;
  allowedOrigins?: string[];
  storeSubmissions?: boolean;
  maxPerHour?: number;
  autoRespond?: boolean;
  autoRespondSubject?: string;
  autoRespondMessage?: string;
}

export interface SubmissionMeta {
  ip?: string;
  userAgent?: string;
  referer?: string;
}

export const forms = pgTable(
  "forms",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    organizationId: text("organization_id")
      .notNull()
      .references(() => organization.id, { onDelete: "cascade" }),
    smtpConfigId: uuid("smtp_config_id").references(() => smtpConfigs.id, {
      onDelete: "set null",
    }),
    templateId: uuid("template_id").references(() => emailTemplates.id, {
      onDelete: "set null",
    }),
    name: text("name").notNull(),
    slug: text("slug").notNull(),
    endpointToken: text("endpoint_token").notNull().unique(),
    templateKey: text("template_key").notNull().default("contact"),
    subject: text("subject"),
    recipients: jsonb("recipients").notNull().$type<string[]>(),
    replyToField: text("reply_to_field"),
    branding: jsonb("branding").$type<FormBranding>(),
    customHtml: text("custom_html"),
    settings: jsonb("settings").$type<FormSettings>(),
    enabled: boolean("enabled").notNull().default(true),
    createdByUserId: text("created_by_user_id").references(() => user.id, {
      onDelete: "set null",
    }),
    createdAt: timestamp("created_at").defaultNow().notNull(),
    updatedAt: timestamp("updated_at").defaultNow().notNull(),
  },
  (table) => [
    index("forms_organizationId_idx").on(table.organizationId),
    index("forms_endpointToken_idx").on(table.endpointToken),
  ],
);

export const formSubmissions = pgTable(
  "form_submissions",
  {
    id: uuid("id").primaryKey().defaultRandom(),
    formId: uuid("form_id")
      .notNull()
      .references(() => forms.id, { onDelete: "cascade" }),
    data: jsonb("data").notNull().$type<Record<string, string>>(),
    meta: jsonb("meta").$type<SubmissionMeta>(),
    emailStatus: text("email_status").notNull().default("queued"),
    createdAt: timestamp("created_at").defaultNow().notNull(),
  },
  (table) => [index("form_submissions_formId_idx").on(table.formId)],
);

export const formRelations = relations(forms, ({ many, one }) => ({
  submissions: many(formSubmissions),
  organization: one(organization, {
    fields: [forms.organizationId],
    references: [organization.id],
  }),
}));

export const formSubmissionRelations = relations(
  formSubmissions,
  ({ one }) => ({
    form: one(forms, {
      fields: [formSubmissions.formId],
      references: [forms.id],
    }),
  }),
);
