from __future__ import annotations

import argparse
import asyncio
import json

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from aidlc.config import Settings
from aidlc.tools.auth import discover_oidc, validate_service_url
from aidlc.tools.models import AnalysisReport, AnalysisRequest


class ToolsClient:
    """Service-account OAuth credentials live here, outside agent context and artifacts."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.resource = validate_service_url(settings.mcp_url)
        self.issuer = validate_service_url(settings.oidc_issuer)

    async def _token(self, scope: str) -> str:
        if not self.settings.mcp_client_secret:
            raise RuntimeError("Set AIDLC_MCP_CLIENT_SECRET for the development service account")
        try:
            async with httpx.AsyncClient(
                timeout=5, follow_redirects=False, trust_env=False
            ) as client:
                metadata = await discover_oidc(client, self.issuer)
                endpoint = validate_service_url(metadata["token_endpoint"])
                response = await client.post(
                    endpoint,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.settings.mcp_client_id,
                        "client_secret": self.settings.mcp_client_secret,
                        **({"scope": scope} if scope else {}),
                        "resource": self.resource,
                    },
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("token_type", "").lower() != "bearer":
                    raise ValueError
                token = payload["access_token"]
                if not isinstance(token, str) or not token:
                    raise ValueError
                return token
        except httpx.HTTPError, ValueError, KeyError, TypeError:
            raise RuntimeError("OIDC service-account token acquisition failed") from None

    async def analyze(self, request: AnalysisRequest) -> AnalysisReport:
        token = await self._token("analysis:run")
        try:
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                timeout=65,
                follow_redirects=False,
                trust_env=False,
            ) as http_client:
                async with Client(
                    streamable_http_client(self.resource, http_client=http_client)
                ) as client:
                    if client.protocol_version != "2026-07-28":
                        raise RuntimeError("Unexpected MCP protocol version")
                    result = await client.call_tool("analyze_code", request.model_dump())
                    if result.is_error or result.structured_content is None:
                        raise RuntimeError("MCP analysis was denied or failed")
                    return AnalysisReport.model_validate(result.structured_content)
        except Exception:
            # Provider and transport errors cannot carry credentials into A2A error artifacts.
            raise RuntimeError(
                "Authenticated MCP analysis failed; inspect the redacted tool audit"
            ) from None

    async def read(self, analysis_id: str) -> dict:
        from mcp.types import TextResourceContents

        from aidlc.tools.server import REPORT_ID

        if REPORT_ID.fullmatch(analysis_id) is None:
            raise ValueError("Invalid analysis ID")
        token = await self._token("analysis:read")
        try:
            async with httpx2.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
                follow_redirects=False,
                trust_env=False,
            ) as http_client:
                async with Client(
                    streamable_http_client(self.resource, http_client=http_client)
                ) as client:
                    result = await client.read_resource(f"analysis://reports/{analysis_id}")
                    content = result.contents[0]
                    if not isinstance(content, TextResourceContents):
                        raise ValueError
                    return json.loads(content.text)
        except Exception:
            raise RuntimeError("Authenticated MCP result read failed") from None


def run() -> None:
    parser = argparse.ArgumentParser(description="Call aidlc-tools using an OIDC service account")
    parser.add_argument("artifact_id", nargs="?")
    parser.add_argument("content_sha256", nargs="?")
    parser.add_argument("--read", metavar="ANALYSIS_ID")
    arguments = parser.parse_args()
    client = ToolsClient(Settings())
    if arguments.read:
        result = asyncio.run(client.read(arguments.read))
    else:
        if not arguments.artifact_id or not arguments.content_sha256:
            parser.error("Provide artifact_id and content_sha256, or --read ANALYSIS_ID")
        report = asyncio.run(
            client.analyze(
                AnalysisRequest(
                    artifact_id=arguments.artifact_id,
                    content_sha256=arguments.content_sha256,
                )
            )
        )
        result = report.model_dump(mode="json")
    print(json.dumps(result, indent=2))
