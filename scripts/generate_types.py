#!/usr/bin/env python3
"""Generate TypeScript types from OpenAPI specification.

This script fetches the OpenAPI spec from a running AnkaLoop server
and generates TypeScript type definitions using openapi-typescript.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import httpx


def fetch_openapi_spec(server_url: str) -> dict:
    """Fetch OpenAPI spec from server.

    Args:
        server_url: The AnkaLoop server URL.

    Returns:
        OpenAPI specification dictionary.
    """
    url = f"{server_url.rstrip('/')}/openapi.json"
    print(f"Fetching OpenAPI spec from {url}...")

    with httpx.Client(timeout=10) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


def save_openapi_spec(spec: dict, output_path: Path) -> None:
    """Save OpenAPI spec to a file.

    Args:
        spec: The OpenAPI specification.
        output_path: Path to save the JSON file.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    print(f"Saved OpenAPI spec to {output_path}")


def generate_typescript_types(spec_path: Path, output_path: Path) -> bool:
    """Generate TypeScript types using openapi-typescript.

    Args:
        spec_path: Path to the OpenAPI JSON file.
        output_path: Path for the output TypeScript file.

    Returns:
        True if successful, False otherwise.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        # Check if npx is available
        subprocess.run(
            ["npx", "--version"],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Error: npx is not available. Please install Node.js.")
        return False

    print(f"Generating TypeScript types to {output_path}...")

    try:
        result = subprocess.run(
            [
                "npx",
                "-y",
                "openapi-typescript",
                str(spec_path),
                "-o",
                str(output_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if result.stdout:
            print(result.stdout)
        print(f"Generated TypeScript types at {output_path}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error generating types: {e.stderr}")
        return False


def generate_manual_types(spec: dict, output_path: Path) -> None:
    """Generate TypeScript types manually (fallback if npx unavailable).

    Args:
        spec: The OpenAPI specification.
        output_path: Path for the output TypeScript file.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    schemas = spec.get("components", {}).get("schemas", {})

    lines = [
        "/**",
        " * AnkaLoop API Type Definitions",
        " * ",
        " * Auto-generated from OpenAPI specification.",
        f" * Generated: {spec.get('info', {}).get('version', 'unknown')}",
        " */",
        "",
        "// ============================================================================",
        "// Enums",
        "// ============================================================================",
        "",
    ]

    # Generate enums
    for name, schema in schemas.items():
        if "enum" in schema:
            lines.append(f"export type {name} = {' | '.join(repr(v) for v in schema['enum'])};")
            lines.append("")

    lines.extend([
        "",
        "// ============================================================================",
        "// Models",
        "// ============================================================================",
        "",
    ])

    # Generate interfaces
    for name, schema in schemas.items():
        if "enum" in schema:
            continue  # Already handled

        if schema.get("type") == "object":
            lines.append(f"export interface {name} {{")

            properties = schema.get("properties", {})
            required = set(schema.get("required", []))

            for prop_name, prop_schema in properties.items():
                ts_type = _json_schema_to_ts(prop_schema)
                optional = "" if prop_name in required else "?"
                description = prop_schema.get("description", "")
                if description:
                    lines.append(f"  /** {description} */")
                lines.append(f"  {prop_name}{optional}: {ts_type};")

            lines.append("}")
            lines.append("")

    lines.extend([
        "",
        "// ============================================================================",
        "// API Paths",
        "// ============================================================================",
        "",
        "export interface paths {",
    ])

    # Generate path types
    paths = spec.get("paths", {})
    for path, methods in paths.items():
        lines.append(f"  '{path}': {{")
        for method, details in methods.items():
            if method in ("get", "post", "put", "delete", "patch"):
                lines.append(f"    {method}: {{")

                # Request body
                request_body = details.get("requestBody", {})
                if request_body:
                    content = request_body.get("content", {})
                    json_content = content.get("application/json", {})
                    if json_content.get("schema"):
                        ref = json_content["schema"].get("$ref", "")
                        if ref:
                            type_name = ref.split("/")[-1]
                            lines.append(f"      requestBody: {type_name};")

                # Response
                responses = details.get("responses", {})
                if "200" in responses:
                    resp = responses["200"]
                    content = resp.get("content", {})
                    json_content = content.get("application/json", {})
                    if json_content.get("schema"):
                        ref = json_content["schema"].get("$ref", "")
                        if ref:
                            type_name = ref.split("/")[-1]
                            lines.append(f"      response: {type_name};")

                lines.append("    };")
        lines.append("  };")

    lines.append("}")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Generated manual TypeScript types at {output_path}")


def _json_schema_to_ts(schema: dict) -> str:
    """Convert JSON Schema to TypeScript type."""
    if "$ref" in schema:
        return schema["$ref"].split("/")[-1]

    schema_type = schema.get("type")

    if schema_type == "string":
        if "enum" in schema:
            return " | ".join(repr(v) for v in schema["enum"])
        return "string"
    elif schema_type == "integer" or schema_type == "number":
        return "number"
    elif schema_type == "boolean":
        return "boolean"
    elif schema_type == "array":
        items = schema.get("items", {})
        return f"Array<{_json_schema_to_ts(items)}>"
    elif schema_type == "object":
        additional = schema.get("additionalProperties")
        if additional:
            return f"Record<string, {_json_schema_to_ts(additional)}>"
        return "Record<string, unknown>"
    elif schema_type is None:
        # anyOf, oneOf, allOf
        if "anyOf" in schema:
            return " | ".join(_json_schema_to_ts(s) for s in schema["anyOf"])
        if "allOf" in schema:
            return " & ".join(_json_schema_to_ts(s) for s in schema["allOf"])

    return "unknown"


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate TypeScript types from AnkaLoop OpenAPI spec"
    )
    parser.add_argument(
        "--server",
        "-s",
        default="http://localhost:8080",
        help="AnkaLoop server URL (default: http://localhost:8080)",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="types/ankaloop-api.d.ts",
        help="Output TypeScript file path (default: types/ankaloop-api.d.ts)",
    )
    parser.add_argument(
        "--spec-output",
        default="openapi.json",
        help="OpenAPI spec output path (default: openapi.json)",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Use manual type generation (no npx required)",
    )

    args = parser.parse_args()

    try:
        # Fetch spec from server
        spec = fetch_openapi_spec(args.server)

        # Save spec
        spec_path = Path(args.spec_output)
        save_openapi_spec(spec, spec_path)

        # Generate TypeScript types
        output_path = Path(args.output)

        if args.manual:
            generate_manual_types(spec, output_path)
        else:
            if not generate_typescript_types(spec_path, output_path):
                print("Falling back to manual type generation...")
                generate_manual_types(spec, output_path)

        return 0

    except httpx.HTTPError as e:
        print(f"Error fetching OpenAPI spec: {e}")
        print("Make sure the AnkaLoop server is running: anka serve")
        return 1
    except Exception as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
