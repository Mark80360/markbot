"""Web tools: web_fetch and http_request."""

import html
import json
import random
import re
from typing import Any
from urllib.parse import urlparse

import chardet
import httpx
from loguru import logger
from markdownify import markdownify as md
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from markbot.agent.tools.base import Tool

# Shared constants
MAX_REDIRECTS = 5

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
]


def _get_random_headers(custom_headers: dict[str, str] | None = None) -> dict[str, str]:
    """Generate realistic browser headers with optional custom overrides."""
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if custom_headers:
        headers.update(custom_headers)
    return headers


def _detect_encoding(content: bytes, content_type_header: str | None = None) -> str:
    """Detect encoding from content-type header or content bytes."""
    if content_type_header:
        charset_match = re.search(r"charset=([^\s;]+)", content_type_header, re.I)
        if charset_match:
            return charset_match.group(1).strip('"').strip("'")
    detected = chardet.detect(content)
    return detected.get("encoding") or "utf-8"


def _normalize(text: str) -> str:
    """Normalize whitespace."""
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _sanitize_for_xml(text: str) -> str:
    """Remove NULL bytes and invalid XML control characters."""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)


def _validate_url(url: str) -> tuple[bool, str]:
    """Validate URL: must be http(s) with valid domain."""
    try:
        p = urlparse(url)
        if p.scheme not in ('http', 'https'):
            return False, f"Only http/https allowed, got '{p.scheme or 'none'}'"
        if not p.netloc:
            return False, "Missing domain"
        return True, ""
    except Exception as e:
        return False, str(e)

class WebFetchTool(Tool):
    """Fetch and extract content from a URL with enhanced capabilities."""

    name = "web_fetch"
    description = "Fetch URL and extract readable content (HTML → markdown/text). Supports custom headers, cookies, and retry."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch"},
            "extractMode": {"type": "string", "enum": ["markdown", "text"], "default": "markdown"},
            "maxChars": {"type": "integer", "minimum": 100, "description": "Maximum characters to return"},
            "headers": {"type": "object", "description": "Custom HTTP headers (key-value pairs)"},
            "cookies": {"type": "object", "description": "Cookies to send (key-value pairs)"},
            "timeout": {"type": "number", "description": "Request timeout in seconds (default: 30)"},
            "retryCount": {"type": "integer", "minimum": 0, "maximum": 5, "description": "Number of retries on failure (default: 3)"},
        },
        "required": ["url"]
    }

    def __init__(self, max_chars: int = 50000, proxy: str | None = None):
        self.max_chars = max_chars
        self.proxy = proxy

    async def execute(
        self,
        url: str,
        extractMode: str = "markdown",
        maxChars: int | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        timeout: float = 30.0,
        retryCount: int = 3,
        **kwargs: Any,
    ) -> str:
        max_chars = maxChars or self.max_chars
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}", "url": url}, ensure_ascii=False)

        try:
            logger.debug("WebFetch: {} (retry={})", "proxy enabled" if self.proxy else "direct connection", retryCount)
            result = await self._fetch_with_retry(
                url=url,
                extract_mode=extractMode,
                max_chars=max_chars,
                custom_headers=headers,
                cookies=cookies,
                timeout=timeout,
                retry_count=retryCount,
            )
            return result
        except Exception as e:
            logger.error("WebFetch error for {}: {}", url, e)
            return json.dumps({"error": str(e), "url": url}, ensure_ascii=False)

    async def _fetch_with_retry(
        self,
        url: str,
        extract_mode: str,
        max_chars: int,
        custom_headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
        timeout: float,
        retry_count: int,
    ) -> str:
        """Fetch URL with retry logic."""

        @retry(
            stop=stop_after_attempt(retry_count + 1) if retry_count > 0 else stop_after_attempt(1),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout, httpx.NetworkError)),
            reraise=True,
        )
        async def _do_fetch():
            return await self._fetch_once(url, extract_mode, max_chars, custom_headers, cookies, timeout)

        return await _do_fetch()

    async def _fetch_once(
        self,
        url: str,
        extract_mode: str,
        max_chars: int,
        custom_headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
        timeout: float,
    ) -> str:
        """Perform a single fetch attempt."""
        from readability import Document

        request_headers = _get_random_headers(custom_headers)

        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=MAX_REDIRECTS,
            timeout=timeout,
            proxy=self.proxy,
            cookies=cookies,
        ) as client:
            r = await client.get(url, headers=request_headers)
            r.raise_for_status()

        ctype = r.headers.get("content-type", "")
        content_bytes = r.content

        if "application/json" in ctype:
            text, extractor = json.dumps(r.json(), indent=2, ensure_ascii=False), "json"
        elif "text/html" in ctype or content_bytes[:256].lower().startswith((b"<!doctype", b"<html")):
            encoding = _detect_encoding(content_bytes, ctype)
            try:
                html_content = content_bytes.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                html_content = content_bytes.decode("utf-8", errors="replace")

            html_content = _sanitize_for_xml(html_content)
            doc = Document(html_content)
            summary_html = doc.summary()
            title = doc.title()

            min_content_len = 100
            if len(self._strip_tags(summary_html)) < min_content_len:
                summary_html = html_content
                extractor_name = "fallback+markdownify" if extract_mode == "markdown" else "fallback"
            else:
                extractor_name = "readability+markdownify" if extract_mode == "markdown" else "readability"

            if extract_mode == "markdown":
                content = _normalize(md(summary_html, heading_style="atx", bullets="-"))
            else:
                content = self._strip_tags(summary_html)

            text = f"# {title}\n\n{content}" if title else content
            extractor = extractor_name
        else:
            encoding = _detect_encoding(content_bytes, ctype)
            try:
                text = content_bytes.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                text = content_bytes.decode("utf-8", errors="replace")
            extractor = "raw"

        truncated = len(text) > max_chars
        if truncated:
            text = text[:max_chars]

        return json.dumps(
            {
                "url": url,
                "finalUrl": str(r.url),
                "status": r.status_code,
                "extractor": extractor,
                "truncated": truncated,
                "length": len(text),
                "text": text,
            },
            ensure_ascii=False,
        )

    def _strip_tags(self, html_content: str) -> str:
        """Remove HTML tags and decode entities."""
        text = re.sub(r"<script[\s\S]*?</script>", "", html_content, flags=re.I)
        text = re.sub(r"<style[\s\S]*?</style>", "", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        return html.unescape(text).strip()


class HttpRequestTool(Tool):
    """Tool for making HTTP API requests with full control."""

    name = "http_request"
    description = "Make HTTP API requests (GET/POST/PUT/DELETE/PATCH). Supports JSON, form-data, and custom headers."
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Request URL"},
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"], "default": "GET"},
            "headers": {"type": "object", "description": "Custom headers (e.g., {\"Authorization\": \"Bearer token\"})"},
            "body": {"type": "string", "description": "Request body (JSON string or form data)"},
            "timeout": {"type": "integer", "minimum": 1, "maximum": 60, "default": 30}
        },
        "required": ["url"]
    }

    def __init__(self, proxy: str | None = None):
        self.proxy = proxy

    async def execute(self, url: str, method: str = "GET", headers: dict | None = None, 
                     body: str | None = None, timeout: int = 30, **kwargs: Any) -> str:
        is_valid, error_msg = _validate_url(url)
        if not is_valid:
            return json.dumps({"error": f"URL validation failed: {error_msg}"}, ensure_ascii=False)

        try:
            req_headers = {"User-Agent": USER_AGENT}
            if headers:
                req_headers.update(headers)

            # Parse body as JSON if it looks like JSON
            req_body = None
            if body:
                try:
                    req_body = json.loads(body)
                    if "Content-Type" not in req_headers:
                        req_headers["Content-Type"] = "application/json"
                except json.JSONDecodeError:
                    req_body = body

            async with httpx.AsyncClient(proxy=self.proxy, timeout=timeout) as client:
                r = await client.request(
                    method=method.upper(),
                    url=url,
                    headers=req_headers,
                    json=req_body if isinstance(req_body, dict) else None,
                    content=req_body if isinstance(req_body, str) else None
                )

            # Try to parse response as JSON
            try:
                response_data = r.json()
                return json.dumps({
                    "status": r.status_code,
                    "headers": dict(r.headers),
                    "body": response_data
                }, indent=2, ensure_ascii=False)
            except:
                return json.dumps({
                    "status": r.status_code,
                    "headers": dict(r.headers),
                    "body": r.text[:10000]  # Limit text response
                }, indent=2, ensure_ascii=False)

        except httpx.TimeoutException:
            return json.dumps({"error": f"Request timed out after {timeout}s"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
