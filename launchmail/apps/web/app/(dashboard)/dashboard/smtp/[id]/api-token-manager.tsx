"use client";

import { useState, useEffect, useCallback } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@workspace/ui/components/card";
import { Button } from "@workspace/ui/components/button";
import { Input } from "@workspace/ui/components/input";
import { createApiTokenAction, deleteApiTokenAction, listApiTokensAction } from "./actions";
import { Trash2Icon, PlusIcon, KeyIcon, CopyIcon, CheckIcon } from "lucide-react";

interface Token {
  id: string;
  name: string;
  tokenPrefix: string;
  createdAt: string;
  lastUsedAt: string | null;
}

export function ApiTokenManager({ configId }: { configId: string }) {
  const [tokens, setTokens] = useState<Token[]>([]);
  const [loading, setLoading] = useState(true);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const [newToken, setNewToken] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Manual reload used after create/delete. setState happens after the await,
  // i.e. asynchronously — never synchronously inside an effect body.
  const loadTokens = useCallback(async () => {
    const result = await listApiTokensAction(configId);
    setTokens(result);
    setLoading(false);
  }, [configId]);

  useEffect(() => {
    let active = true;
    listApiTokensAction(configId).then((result) => {
      if (!active) return;
      setTokens(result);
      setLoading(false);
    });
    return () => {
      active = false;
    };
  }, [configId]);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    setCreating(true);
    setError(null);
    const result = await createApiTokenAction(configId, newName);
    if (result.token) {
      setNewToken(result.token);
      setNewName("");
      loadTokens();
    } else {
      setError(result.error ?? "Failed to create token");
    }
    setCreating(false);
  }

  async function handleDelete(tokenId: string) {
    await deleteApiTokenAction(tokenId);
    loadTokens();
  }

  async function copyToken() {
    if (newToken) {
      await navigator.clipboard.writeText(newToken);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle>API tokens</CardTitle>
        <p className="text-caption text-muted-foreground">
          Bearer tokens for sending email through this server via the API
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {newToken && (
          <div className="rounded-lg border border-success/30 bg-success-tint p-4">
            <div className="mb-2 flex items-center gap-2">
              <KeyIcon className="size-4 text-success-on" />
              <span className="text-body-strong text-success-on">Token created</span>
            </div>
            <p className="mb-2 text-caption text-muted-foreground">
              Copy this token now — it won&apos;t be shown again.
            </p>
            <div className="flex gap-2">
              <code className="flex-1 break-all rounded bg-background px-3 py-2 text-mono text-xs">
                {newToken}
              </code>
              <Button size="sm" variant="outline" onClick={copyToken}>
                {copied ? (
                  <CheckIcon className="size-4" />
                ) : (
                  <CopyIcon className="size-4" />
                )}
              </Button>
            </div>
          </div>
        )}

        <form onSubmit={handleCreate} className="flex gap-2">
          <Input
            placeholder="Token name (e.g. Production)"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            className="flex-1"
          />
          <Button type="submit" size="sm" loading={creating} disabled={!newName.trim()}>
            {!creating && <PlusIcon className="mr-1 size-4" />}
            {creating ? "Creating..." : "Create"}
          </Button>
        </form>
        {error && <p className="text-caption text-destructive">{error}</p>}

        {loading ? (
          <p className="text-body text-muted-foreground">Loading tokens...</p>
        ) : tokens.length === 0 ? (
          <p className="text-body text-muted-foreground">
            No API tokens yet. Create one to start sending emails via the API.
          </p>
        ) : (
          <div className="space-y-2">
            {tokens.map((token) => (
              <div
                key={token.id}
                className="flex items-center justify-between rounded-lg border px-3 py-2"
              >
                <div>
                  <p className="text-body-strong">{token.name}</p>
                  <p className="text-mono text-xs text-muted-foreground">
                    {token.tokenPrefix}...
                    {token.lastUsedAt && (
                      <span className="ml-2">
                        Last used: {new Date(token.lastUsedAt).toLocaleDateString()}
                      </span>
                    )}
                  </p>
                </div>
                <Button
                  variant="ghost"
                  size="icon"
                  onClick={() => handleDelete(token.id)}
                >
                  <Trash2Icon className="size-4" />
                </Button>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
