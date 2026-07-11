export interface InvitationEmailInput {
  organizationName: string;
  inviterName: string;
  inviteUrl: string;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function renderInvitationEmail(input: InvitationEmailInput): {
  subject: string;
  html: string;
  text: string;
} {
  const org = escapeHtml(input.organizationName);
  const inviter = escapeHtml(input.inviterName);
  const url = input.inviteUrl;

  const subject = `${inviter} invited you to ${input.organizationName} on LaunchMail`;

  const html = `<!doctype html>
<html>
  <body style="margin:0;background:#f4f4f7;font-family:Helvetica,Arial,sans-serif;color:#1a1a1a;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f7;padding:32px 0;">
      <tr><td align="center">
        <table role="presentation" width="480" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;overflow:hidden;border:1px solid #e6e6eb;">
          <tr><td style="padding:32px 32px 8px;">
            <h1 style="margin:0;font-size:20px;">You're invited</h1>
          </td></tr>
          <tr><td style="padding:8px 32px 24px;font-size:14px;line-height:22px;color:#444;">
            <strong>${inviter}</strong> invited you to join the
            <strong>${org}</strong> workspace on LaunchMail.
          </td></tr>
          <tr><td style="padding:0 32px 32px;">
            <a href="${url}" style="display:inline-block;background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 20px;border-radius:8px;font-size:14px;font-weight:600;">Accept invitation</a>
          </td></tr>
          <tr><td style="padding:0 32px 32px;font-size:12px;color:#888;">
            Or paste this link into your browser:<br />
            <a href="${url}" style="color:#2563eb;">${url}</a>
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>`;

  const text = `${input.inviterName} invited you to join the "${input.organizationName}" workspace on LaunchMail.\n\nAccept the invitation: ${url}`;

  return { subject, html, text };
}
