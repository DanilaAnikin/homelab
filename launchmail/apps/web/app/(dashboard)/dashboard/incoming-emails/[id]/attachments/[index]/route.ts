import { apiFetch } from "@/lib/api";

// Stream an incoming-email attachment through the web origin: apiFetch forwards
// the session bearer token to the API, so the browser never needs to talk to
// the API directly (and the file is always force-downloaded by the API's
// Content-Disposition).
export async function GET(
  _req: Request,
  { params }: { params: Promise<{ id: string; index: string }> },
) {
  const { id, index } = await params;
  const res = await apiFetch(
    `/api/incoming-emails/${encodeURIComponent(id)}/attachments/${encodeURIComponent(index)}`,
  );
  if (!res.ok) {
    return new Response("Attachment unavailable", { status: res.status });
  }
  const headers = new Headers();
  headers.set(
    "Content-Type",
    res.headers.get("content-type") ?? "application/octet-stream",
  );
  const cd = res.headers.get("content-disposition");
  if (cd) headers.set("Content-Disposition", cd);
  return new Response(res.body, { status: 200, headers });
}
