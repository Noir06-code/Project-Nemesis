import ast
import ipaddress
import json
import operator as op
import os
import re
import socket
import subprocess
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from ddgs import DDGS
from langchain_core.tools import tool
import requests

# ----------------------------------------------------------------------------
# In-memory Notes Storage
# ----------------------------------------------------------------------------
_NOTES_FILE = "data/notes.json"


def _load_notes() -> dict:
    if os.path.exists(_NOTES_FILE):
        try:
            with open(_NOTES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_notes(notes: dict):
    os.makedirs(os.path.dirname(_NOTES_FILE) or ".", exist_ok=True)
    with open(_NOTES_FILE, "w", encoding="utf-8") as f:
        json.dump(notes, f, indent=2)


# ----------------------------------------------------------------------------
# Tool Definitions
# ----------------------------------------------------------------------------

@tool
def system_command(command: str) -> str:
    """Run a shell command on the host system and return its output (stdout + stderr).
    Use for terminal tasks, file system exploration, diagnostics, or script execution.
    """
    MAX_CHARS = 10_000
    TIMEOUT = 60
    output = ""
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
        )
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        output += f"\n[exit code: {result.returncode}]"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {TIMEOUT}s"
    except Exception as e:
        return f"Error running command: {e}"

    if len(output) > MAX_CHARS:
        output = output[:MAX_CHARS] + f"\n...[truncated, {len(output) - MAX_CHARS} more chars]"
    return output.strip() or "Command completed with no output."


@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web for up-to-date information, facts, or websites using DuckDuckGo.
    Returns title, URL, and snippet for each match.
    """
    results = None
    for _ in range(3):
        try:
            results = DDGS().text(query, max_results=max_results)
            break
        except Exception as e:
            return f"Error searching: {e}"

    if not results:
        return "No results found."

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r.get('title')}\n   URL: {r.get('href')}\n   Summary: {r.get('body')}")
    return "\n\n".join(lines)


@tool
def web_fetch(url: str) -> str:
    """Fetch a public webpage and extract readable text with SSRF protection.
    Use this to read articles, documentation, and web pages in detail.
    """
    MAX_CHARS = 15_000
    MAX_BYTES = 3_000_000

    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; WebFetcher/1.0)"
    }

    try:
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            return "Error: Only HTTP and HTTPS URLs are supported."

        if not parsed.hostname:
            return "Error: Invalid URL."

        hostname = parsed.hostname.lower()

        if hostname == "localhost" or hostname.endswith(".localhost"):
            return "Error: Private addresses are not allowed."

        try:
            addresses = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            for address in addresses:
                ip = ipaddress.ip_address(address[4][0])
                if not ip.is_global:
                    return "Error: Private or restricted IP address."
        except Exception:
            return "Error: Could not validate hostname."

        with requests.get(
            url,
            headers=headers,
            timeout=(5, 15),
            stream=True,
            allow_redirects=False
        ) as resp:
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type", "")
            if "html" not in content_type.lower() and "text" not in content_type.lower():
                return f"Unsupported content type: {content_type}"

            content_length = resp.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_BYTES:
                return "Error: Webpage is too large."

            chunks = []
            size = 0
            for chunk in resp.iter_content(8192):
                size += len(chunk)
                if size > MAX_BYTES:
                    return "Error: Webpage exceeded size limit."
                chunks.append(chunk)

            resp._content = b"".join(chunks)
            resp._content_consumed = True
            html = resp.text

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "svg", "iframe"]):
            tag.decompose()

        text = soup.get_text(separator="\n")
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text).strip()

        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS] + "\n...[truncated]"

        return text or "No readable text found."

    except requests.RequestException as e:
        return f"Fetch error: {e}"
    except Exception as e:
        return f"Extraction error: {e}"


# Safe arithmetic operator mapping for calculator
_OP_MAP = {
    ast.Add: op.add,
    ast.Sub: op.sub,
    ast.Mult: op.mul,
    ast.Div: op.truediv,
    ast.Pow: op.pow,
    ast.Mod: op.mod,
    ast.USub: op.neg,
    ast.UAdd: op.pos,
}


def _safe_eval_node(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise TypeError("Only numbers allowed in expressions")
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        if op_type not in _OP_MAP:
            raise TypeError(f"Operator {op_type} not supported")
        left = _safe_eval_node(node.left)
        right = _safe_eval_node(node.right)
        return _OP_MAP[op_type](left, right)
    if isinstance(node, ast.UnaryOp):
        op_type = type(node.op)
        if op_type not in _OP_MAP:
            raise TypeError(f"Operator {op_type} not supported")
        operand = _safe_eval_node(node.operand)
        return _OP_MAP[op_type](operand)
    raise TypeError(f"Unsupported syntax: {type(node).__name__}")


@tool
def calculator(expression: str) -> str:
    """Safely compute mathematical calculations and arithmetic expressions.
    Example: '(15 * 8) + (120 / 4)'.
    """
    clean_expr = expression.strip()
    try:
        tree = ast.parse(clean_expr, mode="eval")
        result = _safe_eval_node(tree.body)
        return str(result)
    except Exception as e:
        return f"Calculation error: {e}"


@tool
def save_note(title: str, content: str) -> str:
    """Save a note or key insight with a given title and content.
    Persists across sessions.
    """
    notes = _load_notes()
    notes[title.strip()] = {
        "title": title.strip(),
        "content": content.strip()
    }
    _save_notes(notes)
    return f"Note '{title.strip()}' saved successfully."


@tool
def read_notes(title: str = "") -> str:
    """Read saved notes. If title is provided, returns that specific note.
    If title is empty or omitted, returns a list of all saved note titles and summaries.
    """
    notes = _load_notes()
    if not notes:
        return "No notes currently saved."

    if title and title.strip():
        target = title.strip()
        if target in notes:
            note = notes[target]
            return f"Title: {note.get('title')}\nContent: {note.get('content')}"
        # Case-insensitive search
        for k, v in notes.items():
            if k.lower() == target.lower():
                return f"Title: {v.get('title')}\nContent: {v.get('content')}"
        return f"Note '{title}' not found. Available notes: {', '.join(notes.keys())}"

    lines = []
    for k, v in notes.items():
        snippet = v.get("content", "").replace("\n", " ")[:80]
        lines.append(f"- **{k}**: {snippet}...")
    return "Saved notes:\n" + "\n".join(lines)


# ----------------------------------------------------------------------------
# Available Tools Registry & Metadata
# ----------------------------------------------------------------------------

AVAILABLE_TOOLS = {
    "web_search": {
        "name": "web_search",
        "tool": web_search,
        "description": "Search the web for up-to-date facts, information, and citations via DuckDuckGo.",
        "category": "Web",
        "default_guarded": False,
        "parameters": {
            "query": {"type": "string", "description": "The search query keywords"},
            "max_results": {"type": "integer", "description": "Max results to return (default 5)"}
        }
    },
    "web_fetch": {
        "name": "web_fetch",
        "tool": web_fetch,
        "description": "Fetch and extract clean readable text from any public URL (with SSRF protection).",
        "category": "Web",
        "default_guarded": False,
        "parameters": {
            "url": {"type": "string", "description": "Full HTTP/HTTPS URL"}
        }
    },
    "system_command": {
        "name": "system_command",
        "tool": system_command,
        "description": "Execute shell commands on the host machine. Guarded by default for human verification.",
        "category": "System",
        "default_guarded": True,
        "parameters": {
            "command": {"type": "string", "description": "Shell command line to execute"}
        }
    },
    "calculator": {
        "name": "calculator",
        "tool": calculator,
        "description": "Safely compute mathematical calculations and arithmetic expressions.",
        "category": "Utility",
        "default_guarded": False,
        "parameters": {
            "expression": {"type": "string", "description": "Math expression (e.g. '(15 * 8) + 40')"}
        }
    },
    "save_note": {
        "name": "save_note",
        "tool": save_note,
        "description": "Save important notes, facts, and task findings into memory.",
        "category": "Memory",
        "default_guarded": False,
        "parameters": {
            "title": {"type": "string", "description": "Note title"},
            "content": {"type": "string", "description": "Note content"}
        }
    },
    "read_notes": {
        "name": "read_notes",
        "tool": read_notes,
        "description": "Read all saved notes or retrieve a specific note by title.",
        "category": "Memory",
        "default_guarded": False,
        "parameters": {
            "title": {"type": "string", "description": "Specific note title (optional)"}
        }
    }
}

# Backward compatibility list:
tools = [item["tool"] for item in AVAILABLE_TOOLS.values()]


def get_tool_by_name(name: str):
    item = AVAILABLE_TOOLS.get(name)
    return item["tool"] if item else None


def get_tools_by_names(names: list):
    res = []
    for name in names:
        tool_obj = get_tool_by_name(name)
        if tool_obj is not None:
            res.append(tool_obj)
    return res


def get_all_tools_metadata() -> list:
    return [
        {
            "name": v["name"],
            "description": v["description"],
            "category": v["category"],
            "default_guarded": v["default_guarded"],
            "parameters": v.get("parameters", {})
        }
        for v in AVAILABLE_TOOLS.values()
    ]
