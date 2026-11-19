
from langchain_core.tools import tool
import subprocess
import re
import requests
from bs4 import BeautifulSoup
from langchain_core.tools import tool
from ddgs import DDGS
import subprocess
import os

@tool
def system_command(command: str) -> str:
    """Run a shell command on the system and return its output (stdout + stderr)."""
    MAX_CHARS = 10_000
    TIMEOUT = 60
    output = ""  # always defined, even if something goes wrong
    
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
    return output.strip()



@tool
def web_search(query: str, max_results: int = 5) -> str:
    """Search the web and return a list of results (title, URL, snippet).
    Use this to find pages, then use web_fetch to read a page in full."""
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
        lines.append(f"{i}. {r.get('title')}\n   {r.get('href')}\n   {r.get('body')}")
    return "\n\n".join(lines)


@tool

def web_fetch(url: str) -> str:
    """
    Fetch a public webpage and extract readable text.

    Input:
        url: HTTP or HTTPS webpage URL.

    Output:
        Extracted webpage text, limited to 15,000 characters.
    """
    import requests
    import re
    import ipaddress
    import socket
    from urllib.parse import urlparse
    from bs4 import BeautifulSoup

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
            addresses = socket.getaddrinfo(
                hostname, None, type=socket.SOCK_STREAM
            )

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

            if "html" not in content_type.lower():
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

        for tag in soup([
            "script", "style", "nav", "footer",
            "header", "aside", "noscript",
            "svg", "iframe"
        ]):
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


tools = [system_command, web_fetch,web_search]
