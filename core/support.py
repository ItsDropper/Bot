"""Validation and formatting for structured Vanta / Minecraft support intake."""

from __future__ import annotations

from urllib.parse import urlsplit


def build_technical_report(
    *,
    minecraft_version: str = "",
    loader: str = "",
    launcher_version: str = "",
    summary: str,
    log_url: str = "",
) -> tuple[str, list[tuple[str, str]]]:
    """Return a bounded D1 report and displayable, non-empty metadata fields."""
    summary = str(summary or "").strip()
    if not 10 <= len(summary) <= 300:
        raise ValueError("Describe the issue in 10–300 characters.")

    values = [
        ("Minecraft version", str(minecraft_version or "").strip()),
        ("Loader", str(loader or "").strip()),
        ("Vanta Launcher version", str(launcher_version or "").strip()),
        ("Log / crash report", str(log_url or "").strip()),
    ]
    for label, value in values:
        if len(value) > 300:
            raise ValueError(f"{label} must be 300 characters or fewer.")

    if values[-1][1]:
        parsed = urlsplit(values[-1][1])
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("The log link must be a valid http:// or https:// URL.")

    display_fields = [(label, value) for label, value in values if value]
    lines = [f"Issue: {summary}"]
    lines.extend(f"{label}: {value}" for label, value in display_fields)
    report = "\n".join(lines)
    if len(report) > 1000:
        raise ValueError("The report is too long. Shorten the issue summary or log link.")
    return report, display_fields
