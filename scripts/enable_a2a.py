"""Enable incoming A2A on the deployed hosted agent (ADR-0010).

Foundry bridges the A2A protocol onto any agent endpoint that implements the
responses protocol: one PATCH publishes an agent card and enables the a2a
endpoint alongside responses. `azd deploy` resets the endpoint's protocol
configuration, so CD re-runs this (idempotent) after every deploy.

Callers authenticate with Entra ID and need the Foundry Agent Consumer role
on the project; anonymous discovery is not supported by the platform.
"""

import os
import sys

import httpx
from azure.identity import DefaultAzureCredential

AGENT_NAME = "memory-first-agent"

AGENT_CARD = {
    "description": (
        "Memory-first web agent: answers from a two-tier Redis vector memory, "
        "falling back to live web search on a miss; every answer cites its sources."
    ),
    "version": "1.0",
    "skills": [
        {
            "id": "memory-first-qa",
            "name": "Memory-first Q&A",
            "description": (
                "Answers factual questions from governed vector memory or the "
                "live web, returning grounded answers with source URLs."
            ),
        }
    ],
}


def main() -> None:
    endpoint = os.environ.get("FOUNDRY_PROJECT_ENDPOINT")
    if not endpoint:
        sys.exit("set FOUNDRY_PROJECT_ENDPOINT (azd env get-value FOUNDRY_PROJECT_ENDPOINT)")
    endpoint = endpoint.rstrip("/")

    with DefaultAzureCredential() as credential:
        token = credential.get_token("https://ai.azure.com/.default").token

    with httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=30.0) as client:
        patched = client.patch(
            f"{endpoint}/agents/{AGENT_NAME}",
            params={"api-version": "v1"},
            json={
                "agent_card": AGENT_CARD,
                "agent_endpoint": {"protocol_configuration": {"responses": {}, "a2a": {}}},
            },
        )
        patched.raise_for_status()
        print(f"A2A enabled on {AGENT_NAME}")

        # Verify discovery end-to-end: fetch the card other agents will resolve.
        card = client.get(f"{endpoint}/agents/{AGENT_NAME}/endpoint/protocols/a2a/agentCard/v1.0")
        card.raise_for_status()
        data = card.json()
        skills = ", ".join(s.get("id", "?") for s in data.get("skills", []))
        version = data.get("protocolVersion")
        print(f"agent card v1.0 live: protocolVersion={version} skills=[{skills}]")
        print(f"A2A base: {endpoint}/agents/{AGENT_NAME}/endpoint/protocols/a2a")


if __name__ == "__main__":
    main()
