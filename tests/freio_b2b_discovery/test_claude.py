from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import research_bytes

from freio_b2b_discovery.discovery import (
    CLAUDE_TIMEOUT_SECONDS,
    MAX_PROMPT_BYTES,
    _extract_structured_output,
    _load_anthropic_api_key,
    _prepare_structured_schema,
    run_claude_discovery,
)
from freio_prospecting.common import ValidationError


class ClaudeDiscoveryTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path, Path, Path, str]:
        binary = root / "claude"
        prompt = root / "prompt.txt"
        schema = root / "schema.json"
        credential = root / "anthropic-api-key"
        state_home = root / "state"
        api_key = "sk-ant-" + "a" * 64
        output = json.dumps(
            {
                "is_error": False,
                "structured_output": json.loads(research_bytes()),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        binary.write_text(
            "#!/usr/bin/python3\n"
            "import hashlib\n"
            "import json\n"
            "import os\n"
            "import pathlib\n"
            "import sys\n"
            "payload = sys.stdin.buffer.read()\n"
            "record = {\n"
            "    'argv': sys.argv[1:],\n"
            "    'stdin': payload.decode('utf-8'),\n"
            "    'envKeys': sorted(os.environ),\n"
            "    'apiKeySha256': hashlib.sha256(\n"
            "        os.environ['ANTHROPIC_API_KEY'].encode('ascii')\n"
            "    ).hexdigest(),\n"
            "}\n"
            "pathlib.Path(os.environ['HOME'], 'record.json').write_text(\n"
            "    json.dumps(record), encoding='utf-8'\n"
            ")\n"
            f"sys.stdout.buffer.write({output!r})\n",
            encoding="utf-8",
        )
        os.chmod(binary, 0o700)
        prompt.write_text("PROMPT_SENTINEL", encoding="utf-8")
        schema.write_text('{"SCHEMA_SENTINEL":true}', encoding="utf-8")
        credential.write_text(api_key + "\n", encoding="ascii")
        os.chmod(credential, 0o600)
        return binary, prompt, schema, credential, state_home, api_key

    def test_uses_api_key_allowlist_and_bounded_stdin_without_secret_in_argv(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary, prompt, schema, credential, state_home, api_key = self._fixture(
                root
            )
            output = run_claude_discovery(
                claude_binary=binary,
                prompt_path=prompt,
                schema_path=schema,
                anthropic_api_key_path=credential,
                state_home=state_home,
                timeout_seconds=CLAUDE_TIMEOUT_SECONDS,
            )
            self.assertEqual(json.loads(output), json.loads(research_bytes()))
            record = json.loads((state_home / "record.json").read_text())
            self.assertEqual(
                record["envKeys"],
                [
                    "ANTHROPIC_API_KEY",
                    "HOME",
                    "LANG",
                    "LC_ALL",
                    "NO_COLOR",
                    "PATH",
                ],
            )
            self.assertEqual(
                record["apiKeySha256"],
                hashlib.sha256(api_key.encode("ascii")).hexdigest(),
            )
            self.assertIn("PROMPT_SENTINEL", record["stdin"])
            self.assertNotIn("SCHEMA_SENTINEL", record["stdin"])
            self.assertIn("SCHEMA_SENTINEL", " ".join(record["argv"]))
            self.assertNotIn(api_key, record["stdin"])
            self.assertNotIn(api_key, " ".join(record["argv"]))
            self.assertNotIn("PROMPT_SENTINEL", " ".join(record["argv"]))
            self.assertEqual(
                record["argv"],
                [
                    "--print",
                    "--model",
                    "claude-sonnet-5",
                    "--max-budget-usd",
                    "1.00",
                    "--permission-mode",
                    "dontAsk",
                    "--output-format",
                    "json",
                    "--input-format",
                    "text",
                    "--no-session-persistence",
                    "--safe-mode",
                    "--strict-mcp-config",
                    "--tools",
                    "WebSearch,WebFetch",
                    "--allowedTools",
                    "WebSearch,WebFetch",
                    "--json-schema",
                    '{"SCHEMA_SENTINEL":true}',
                ],
            )

    def test_rejects_invalid_or_failed_structured_output_envelopes(self) -> None:
        cases = (
            b"not-json",
            b'{"is_error":false,"is_error":false,"structured_output":{}}',
            b'{"is_error":true,"structured_output":{}}',
            b'{"is_error":false,"structured_output":null}',
            b'{"is_error":false,"structured_output":{"value":NaN}}',
        )
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(ValidationError):
                    _extract_structured_output(raw)

    def test_transforms_only_unsupported_structured_output_constraints(self) -> None:
        transformed = json.loads(
            _prepare_structured_schema(
                json.dumps(
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["email"],
                        "properties": {
                            "email": {
                                "type": "string",
                                "format": "email",
                                "pattern": "^[^@]+@[^@]+$",
                                "minLength": 3,
                                "maxLength": 320,
                            }
                        },
                    }
                )
            )
        )
        self.assertEqual(
            transformed,
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["email"],
                "properties": {"email": {"type": "string"}},
            },
        )

    def test_rejects_oversized_prompt_and_nonprivate_or_unsafe_key_before_spawn(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary, prompt, schema, credential, state_home, _api_key = self._fixture(
                root
            )
            cases: list[tuple[str, bytes, int]] = [
                ("oversized prompt", b"x" * (MAX_PROMPT_BYTES + 1), 0o600),
                ("public credential", b"sk-ant-" + b"a" * 64, 0o640),
                ("whitespace credential", b"sk-ant-" + b"a" * 32 + b" x", 0o600),
            ]
            for label, value, mode in cases:
                with self.subTest(label=label):
                    prompt.write_text("PROMPT_SENTINEL", encoding="utf-8")
                    credential.write_bytes(b"sk-ant-" + b"a" * 64)
                    os.chmod(credential, 0o600)
                    if label == "oversized prompt":
                        prompt.write_bytes(value)
                    else:
                        credential.write_bytes(value)
                        os.chmod(credential, mode)
                    with self.assertRaises(ValidationError):
                        run_claude_discovery(
                            claude_binary=binary,
                            prompt_path=prompt,
                            schema_path=schema,
                            anthropic_api_key_path=credential,
                            state_home=state_home,
                            timeout_seconds=10,
                        )
                    self.assertFalse((state_home / "record.json").exists())

    def test_accepts_systemd_delivered_key_only_from_exact_credential_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _binary, _prompt, _schema, credential, _state_home, api_key = self._fixture(
                root
            )
            os.chmod(credential, 0o440)
            with patch.dict(os.environ):
                os.environ.pop("CREDENTIALS_DIRECTORY", None)
                with self.assertRaises(ValidationError):
                    _load_anthropic_api_key(credential)
            with patch.dict(
                os.environ,
                {"CREDENTIALS_DIRECTORY": str(credential.parent)},
            ):
                self.assertEqual(_load_anthropic_api_key(credential), api_key)
                os.chmod(credential, 0o640)
                with self.assertRaises(ValidationError):
                    _load_anthropic_api_key(credential)


if __name__ == "__main__":
    unittest.main()
