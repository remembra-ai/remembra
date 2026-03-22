"""
Changelog Parser - Extract structured releases from CHANGELOG.md files.

Supports the Keep a Changelog format (https://keepachangelog.com/).
"""

import contextlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import structlog

log = structlog.get_logger(__name__)


@dataclass
class ChangelogRelease:
    """A single release from a changelog."""
    
    version: str
    date: datetime | None
    sections: dict[str, list[str]] = field(default_factory=dict)
    raw_content: str = ""
    
    def to_memory_content(self) -> str:
        """
        Convert to a memory-friendly content string.
        
        Format: "Version X.Y.Z (YYYY-MM-DD): Summary of changes"
        """
        date_str = self.date.strftime("%Y-%m-%d") if self.date else "Unreleased"
        
        parts = [f"Version {self.version} ({date_str}):"]
        
        for section, items in self.sections.items():
            if items:
                parts.append(f"\n{section}:")
                for item in items[:5]:  # Limit to 5 items per section
                    # Clean up the item
                    clean_item = item.strip().lstrip("- *")
                    if clean_item:
                        parts.append(f"  - {clean_item}")
        
        return "\n".join(parts)
    
    def to_metadata(self) -> dict[str, Any]:
        """Convert to memory metadata."""
        return {
            "type": "changelog_release",
            "version": self.version,
            "date": self.date.isoformat() if self.date else None,
            "sections": list(self.sections.keys()),
            "item_count": sum(len(items) for items in self.sections.values()),
        }


class ChangelogParser:
    """
    Parse CHANGELOG.md files into structured releases.
    
    Supports:
    - Keep a Changelog format (https://keepachangelog.com/)
    - Conventional Changelog format
    - Most markdown changelog formats with ## headings for versions
    
    Example:
        parser = ChangelogParser()
        releases = parser.parse(changelog_content)
        for release in releases:
            print(f"{release.version}: {len(release.sections)} sections")
    """
    
    # Pattern for version headers like "## [1.0.0] - 2024-01-15"
    VERSION_PATTERN = re.compile(
        r"^##\s*\[?([Uu]nreleased|v?\d+\.\d+(?:\.\d+)?(?:-[\w.]+)?)\]?"
        r"(?:\s*[-–—]\s*(\d{4}-\d{2}-\d{2}))?",
        re.MULTILINE,
    )
    
    # Pattern for section headers like "### Added"
    SECTION_PATTERN = re.compile(r"^###\s*(.+)", re.MULTILINE)
    
    # Common section names (case-insensitive)
    KNOWN_SECTIONS = {
        "added", "changed", "deprecated", "removed", "fixed", "security",
        "breaking", "features", "bug fixes", "improvements", "notes",
    }
    
    def parse(self, content: str) -> list[ChangelogRelease]:
        """
        Parse changelog content into a list of releases.
        
        Args:
            content: Raw markdown content of the changelog
            
        Returns:
            List of ChangelogRelease objects, newest first
        """
        releases: list[ChangelogRelease] = []
        
        # Find all version headers
        version_matches = list(self.VERSION_PATTERN.finditer(content))
        
        if not version_matches:
            log.warning("no_versions_found_in_changelog")
            return releases
        
        for i, match in enumerate(version_matches):
            version = match.group(1)
            date_str = match.group(2)
            
            # Parse date if present
            release_date = None
            if date_str:
                with contextlib.suppress(ValueError):
                    release_date = datetime.strptime(date_str, "%Y-%m-%d")
            
            # Extract content between this version and the next
            start = match.end()
            end = version_matches[i + 1].start() if i + 1 < len(version_matches) else len(content)
            raw_content = content[start:end].strip()
            
            # Parse sections within the release
            sections = self._parse_sections(raw_content)
            
            releases.append(ChangelogRelease(
                version=version,
                date=release_date,
                sections=sections,
                raw_content=raw_content,
            ))
            
            log.debug(
                "parsed_changelog_release",
                version=version,
                date=date_str,
                section_count=len(sections),
            )
        
        return releases
    
    def _parse_sections(self, content: str) -> dict[str, list[str]]:
        """Parse sections (### headings) within a release."""
        sections: dict[str, list[str]] = {}
        
        # Find all section headers
        section_matches = list(self.SECTION_PATTERN.finditer(content))
        
        if not section_matches:
            # No sections, treat entire content as a single section
            items = self._parse_list_items(content)
            if items:
                sections["Changes"] = items
            return sections
        
        for i, match in enumerate(section_matches):
            section_name = match.group(1).strip()
            
            # Extract content between this section and the next
            start = match.end()
            end = section_matches[i + 1].start() if i + 1 < len(section_matches) else len(content)
            section_content = content[start:end].strip()
            
            items = self._parse_list_items(section_content)
            if items:
                sections[section_name] = items
        
        return sections
    
    def _parse_list_items(self, content: str) -> list[str]:
        """Extract list items from content."""
        items = []
        
        # Match lines starting with - or *
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith(("- ", "* ", "• ")):
                item = line.lstrip("-*• ").strip()
                if item:
                    items.append(item)
        
        return items
    
    def parse_file(self, file_path: str) -> list[ChangelogRelease]:
        """
        Parse a changelog from a file path.
        
        Args:
            file_path: Path to the CHANGELOG.md file
            
        Returns:
            List of ChangelogRelease objects
        """
        with open(file_path, encoding="utf-8") as f:
            content = f.read()
        return self.parse(content)


# Convenience function
def parse_changelog(content_or_path: str) -> list[ChangelogRelease]:
    """
    Parse a changelog from content or file path.
    
    Automatically detects if input is a file path or raw content.
    """
    parser = ChangelogParser()
    
    # Check if it looks like a file path
    if content_or_path.endswith(".md") and "\n" not in content_or_path:
        return parser.parse_file(content_or_path)
    
    return parser.parse(content_or_path)
