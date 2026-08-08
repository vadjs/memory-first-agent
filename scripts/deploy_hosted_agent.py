"""Deploy the memory-first agent as a Foundry Hosted Agent (ADR-0009).

Packages foundry/main.py + requirements + the `agent` package as a code zip,
creates a hosted-agent version (remote build), waits for it to become active,
routes 100% of traffic to it, and invokes it once end-to-end.

All secrets arrive from the caller's environment — nothing is read from files
except the source code being packaged.
"""

import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AgentEndpointConfig,
    CodeConfiguration,
    CodeDependencyResolution,
    FixedRatioVersionSelectionRule,
    HostedAgentDefinition,
    ProtocolConfiguration,
    ProtocolVersionRecord,
    ResponsesProtocolConfiguration,
    VersionSelector,
)
from azure.identity import DefaultAzureCredential

ROOT = Path(__file__).parent.parent
AGENT_NAME = "memory-first-agent"

REQUIRED_ENV = [
    "FOUNDRY_PROJECT_ENDPOINT",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_KEY",
    "TAVILY_API_KEY",
    "REDIS_URL",
]


def build_zip() -> Path:
    zip_path = Path(tempfile.gettempdir()) / f"{AGENT_NAME}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(ROOT / "foundry" / "main.py", "main.py")
        zf.write(ROOT / "foundry" / "requirements.txt", "requirements.txt")
        for path in (ROOT / "src" / "agent").rglob("*.py"):
            zf.write(path, Path("agent") / path.relative_to(ROOT / "src" / "agent"))
    return zip_path


def main() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        sys.exit(f"missing env: {missing}")

    endpoint = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
    hosted_env = {
        "AZURE_OPENAI_ENDPOINT": os.environ["AZURE_OPENAI_ENDPOINT"],
        "AZURE_OPENAI_API_KEY": os.environ["AZURE_OPENAI_API_KEY"],
        "CHAT_DEPLOYMENT": "gpt-5.6-luna",
        "UTILITY_DEPLOYMENT": "gpt-5-nano",
        "EMBED_DEPLOYMENT": "text-embedding-3-small",
        "TAVILY_API_KEY": os.environ["TAVILY_API_KEY"],
        "REDIS_URL": os.environ["REDIS_URL"],
    }

    zip_path = build_zip()
    print(f"packaged {zip_path} ({zip_path.stat().st_size // 1024} KB)")

    with (
        zip_path.open("rb") as code_stream,
        DefaultAzureCredential() as credential,
        AIProjectClient(endpoint=endpoint, credential=credential) as client,
    ):
        created = client.agents.create_version_from_code(
            agent_name=AGENT_NAME,
            description="Memory-first web agent (hosted variant): two-tier Redis vector "
            "memory with live-web fallback and cited answers.",
            definition=HostedAgentDefinition(
                cpu="0.5",
                memory="1Gi",
                code_configuration=CodeConfiguration(
                    runtime="python_3_14",
                    entry_point=["python", "main.py"],
                    dependency_resolution=CodeDependencyResolution.REMOTE_BUILD,
                ),
                environment_variables=hosted_env,
                protocol_versions=[ProtocolVersionRecord(protocol="responses", version="2.0.0")],
            ),
            code=code_stream,
        )
        print(f"created version {created.version}; waiting for active…")

        for attempt in range(60):
            time.sleep(10)
            details = client.agents.get_version(
                agent_name=AGENT_NAME, agent_version=created.version
            )
            status = details["status"]
            print(f"  status={status} ({attempt + 1}/60)")
            if status == "active":
                break
            if status == "failed":
                sys.exit(f"provisioning failed: {dict(details)}")
        else:
            sys.exit("timed out waiting for active")

        client.agents.update_details(
            agent_name=AGENT_NAME,
            agent_endpoint=AgentEndpointConfig(
                version_selector=VersionSelector(
                    version_selection_rules=[
                        FixedRatioVersionSelectionRule(
                            agent_version=created.version, traffic_percentage=100
                        )
                    ]
                ),
                protocol_configuration=ProtocolConfiguration(
                    responses=ResponsesProtocolConfiguration()
                ),
            ),
        )
        print(f"routed 100% of traffic to version {created.version}")

        with client.get_openai_client(agent_name=AGENT_NAME) as openai_client:
            response = openai_client.responses.create(
                input="What is the strangler fig pattern in software architecture?"
            )
        print(f"invoke ok: {response.output_text[:160]}")


if __name__ == "__main__":
    main()
