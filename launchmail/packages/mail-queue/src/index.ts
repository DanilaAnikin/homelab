// Queue
export { mailQueue, enqueueEmail } from "./queue";
export type { EmailJobData } from "./queue";

// Worker
export { startWorker } from "./worker";

// Real-time inbox via IMAP IDLE + safety poll + backfill (worker process)
export { startInboxIdle } from "./inbox-idle";
export type { InboxIdleManager } from "./inbox-idle";

// SMTP transport
export {
  sendMail,
  testSmtpConnection,
  getTransport,
  invalidateTransport,
} from "./smtp";
export type { SendMailInput } from "./smtp";

// SMTP configs CRUD
export {
  listSmtpConfigs,
  getSmtpConfig,
  getSmtpConfigById,
  getDefaultSmtpConfig,
  createSmtpConfig,
  updateSmtpConfig,
  deleteSmtpConfig,
  listReceiveEnabledConfigs,
  updateImapState,
} from "./smtp-configs.service";
export type {
  SmtpConfig,
  CreateSmtpConfigInput,
  UpdateSmtpConfigInput,
} from "./smtp-configs.service";

// Incoming mail (IMAP) — fetch/parse/store + inbox queries
export {
  syncMailbox,
  backfillMailbox,
  fetchAttachment,
  testImapConnection,
} from "./imap.service";
export type { SyncResult, FetchedAttachment } from "./imap.service";
export {
  listMailboxes,
  listIncomingEmails,
  getIncomingEmail,
  setIncomingEmailSeen,
  setIncomingEmailStarred,
  setIncomingEmailArchived,
  deleteIncomingEmail,
  markIncomingEmailReplied,
} from "./inbox.service";
export type {
  Mailbox,
  IncomingEmailSummary,
  InboxFolder,
} from "./inbox.service";

// API tokens
export {
  createApiToken,
  listApiTokens,
  deleteApiToken,
  findApiTokenByHash,
  touchApiToken,
  hashToken,
} from "./api-tokens.service";
export type {
  ApiToken,
  ApiTokenWithPlaintext,
  ApiTokenRole,
  CreateApiTokenInput,
} from "./api-tokens.service";

// Forms (Formspree-style)
export {
  createForm,
  listForms,
  getForm,
  getFormByToken,
  updateForm,
  deleteForm,
  recordSubmission,
  listSubmissions,
} from "./forms.service";
export type {
  Form,
  FormSubmission,
  CreateFormInput,
  UpdateFormInput,
} from "./forms.service";

// Email templates (user-built)
export {
  createTemplate,
  listTemplates,
  getTemplate,
  updateTemplate,
  deleteTemplate,
} from "./templates.service";
export type {
  EmailTemplate,
  EmailBlock,
  CreateTemplateInput,
  UpdateTemplateInput,
} from "./templates.service";

// Suppression list
export {
  addSuppression,
  listSuppressions,
  removeSuppression,
  isSuppressed,
  filterSuppressed,
} from "./suppressions.service";
export type { Suppression } from "./suppressions.service";

// Sending domains (SPF/DKIM/DMARC)
export {
  createDomain,
  listDomains,
  getDomain,
  deleteDomain,
  verifyDomain,
  buildRecords,
  getDkimPrivateKey,
  findSigningDomain,
  domainDeliverability,
} from "./domains.service";
export type {
  Domain,
  DnsRecord,
  DkimSigningKey,
  DeliverabilityReport,
} from "./domains.service";

// Webhooks
export {
  createWebhook,
  listWebhooks,
  getWebhook,
  updateWebhook,
  deleteWebhook,
  dispatchEvent,
  pingWebhook,
} from "./webhooks.service";
export type {
  Webhook,
  WebhookEvent,
  CreateWebhookInput,
  UpdateWebhookInput,
} from "./webhooks.service";

// Open/click tracking + analytics
export {
  injectTracking,
  trackingEnabled,
  recordOpen,
  recordClick,
  getAnalytics,
} from "./tracking.service";
export type { Analytics, AnalyticsDay } from "./tracking.service";

// Audiences, contacts, broadcasts
export {
  createAudience,
  listAudiences,
  getAudience,
  deleteAudience,
  addContacts,
  listContacts,
  getSubscribedContacts,
  removeContact,
  unsubscribeContact,
  createBroadcast,
  completeBroadcast,
  listBroadcasts,
} from "./audiences.service";
export type {
  Audience,
  Contact,
  Broadcast,
  AddContactInput,
  CreateBroadcastInput,
} from "./audiences.service";

// Audit log
export { recordAudit, listAudit } from "./audit.service";
export type { AuditLog } from "./audit.service";

// Email logs + Sent view
export {
  listEmailLogs,
  getEmailLogsByConfig,
  getEmailLogsByConfigs,
  listSentEmails,
  getSentEmail,
} from "./email-logs.service";
export type { SentEmailSummary } from "./email-logs.service";

// Crypto
export {
  encrypt,
  decrypt,
  sign,
  verify,
  signId,
  parseSignedId,
} from "./crypto";

// Redis
export { getRedis, closeRedis } from "./redis";

// Types
export { sendEmailSchema, recipientSchema } from "./types";
export type { SendEmailInput, Recipient } from "./types";
