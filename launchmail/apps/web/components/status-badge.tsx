import { Badge } from "@workspace/ui/components/badge";

// The Badge tonal variants this badge maps onto.
type BadgeVariant =
  | "success"
  | "warning"
  | "info"
  | "destructive-tonal"
  | "pending";

// The five logical tones this badge exposes, mapped onto the single Badge pill
// system (tonal *-tint/*-on variants, AA-compliant, consistent geometry).
type Tone = "success" | "danger" | "warning" | "info" | "neutral";

const TONE_VARIANT: Record<Tone, BadgeVariant> = {
  success: "success",
  danger: "destructive-tonal",
  warning: "warning",
  info: "info",
  neutral: "pending",
};

const STATUS_TONE: Record<string, Tone> = {
  sent: "success",
  delivered: "success",
  active: "success",
  verified: "success",
  failed: "danger",
  bounced: "danger",
  bounce: "danger",
  complaint: "danger",
  error: "danger",
  pending: "warning",
  suppressed: "warning",
  unverified: "warning",
  queued: "info",
  processing: "info",
  manual: "neutral",
  disabled: "neutral",
};

export function StatusBadge({
  status,
  className,
}: {
  status: string;
  className?: string;
}) {
  const tone = STATUS_TONE[status.toLowerCase()] ?? "neutral";
  return (
    <Badge variant={TONE_VARIANT[tone]} className={`capitalize ${className ?? ""}`}>
      {status}
    </Badge>
  );
}
