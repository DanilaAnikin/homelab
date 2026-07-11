export interface SmtpConfigDetail {
  id: string;
  name: string;
  host: string;
  port: number;
  secure?: boolean;
  username: string;
  fromAddress: string;
  fromName: string | null;
  isDefault: boolean;
  // Incoming mail (IMAP). Present when receiving is configured. The IMAP
  // password is never returned by the API.
  imapHost?: string | null;
  imapPort?: number | null;
  imapUsername?: string | null;
  createdAt?: string;
}
