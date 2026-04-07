"""MemorySanitizer - Three-stage quality gate for memory entries.

Implements the quality control pipeline inspired by CoPaw's ReMeLight:
1. Noise Filter: Reject internal monologue, conversational filler
2. Secret Redaction: Auto-redact API keys, tokens, passwords
3. Deduplication: Reject near-duplicates (>85% similarity)

All entries to L2 Hot Memory MUST pass through this sanitizer.
"""

from __future__ import annotations

import re
import string
from difflib import SequenceMatcher
from typing import List, Optional

from loguru import logger


class MemorySanitizer:
    """
    Quality gate for memory entries.
    
    Purpose:
    - Ensure only high-quality content enters persistent memory
    - Protect sensitive information (API keys, tokens, passwords)
    - Prevent duplicate or near-duplicate entries
    
    Usage:
        sanitizer = MemorySanitizer()
        cleaned = sanitizer.clean_entry(content, existing_contents)
        
        if cleaned is None:
            # Entry was rejected
        else:
            # Entry passed quality gate (may be modified)
            use(cleaned)
    
    Configuration:
        All thresholds are configurable via constructor parameters.
        Default values are tuned for general-purpose AI assistant usage.
    """
    
    # Stage 1: Noise Filter patterns
    NOISE_PATTERNS = [
        r'^\s*(ok|okay|sure|yes|no|got it|understood|thanks|thank you)[\s.!?,]*$',
        r'^\s*(i think|i believe|maybe|perhaps|probably)[\s,.]*$',
        r'^\s*(let me see|i\'ll check|i will look into)',
        r'^\s*(here is|here are|this is|these are)\s+(the|a|an)\s+',
        r'^\s*(as i mentioned|as discussed earlier|like I said)',
        r'^\s*(to summarize|in summary|in conclusion)',
        r'^\s*\*+\s*$',  # Just asterisks/bullets
        r'^\s*(-|\*)\s*$',  # Empty bullet points
        r'^\s*(please |can you |could you )',  # Too conversational
    ]
    
    MIN_CONTENT_LENGTH = 15       # Minimum characters after cleaning
    MAX_CONTENT_LENGTH = 1000     # Maximum characters (will truncate)
    
    # Stage 2: Secret patterns to redact
    SECRET_PATTERNS = {
        'api_key': [
            r'(api[_-]?key|apikey)["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_\-]{20,})',
            r'(["\'])([a-zA-Z0-9]{32,})(["\'])',  # Long alphanumeric strings in quotes
        ],
        'token': [
            r'(token|access_token|auth_token)["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_.\-]{20,})',
            r'Bearer\s+([a-zA-Z0-9_.\-]{30,})',
        ],
        'password': [
            r'(password|passwd|pwd)["\']?\s*[:=]\s*["\']?([^\s"\']+)',
        ],
        'secret_key': [
            r'(secret[_-]?key|secretkey|private[_-]?key)["\']?\s*[:=]\s*["\']?([a-zA-Z0-9_\-]{20,})',
        ],
        'url_with_credentials': [
            r'https?://[^:\s]+:[^@\s]+@',  # URLs with embedded credentials
        ],
    }
    
    REDACTION_PLACEHOLDER = "[REDACTED]"
    
    # Stage 3: Deduplication settings
    SIMILARITY_THRESHOLD = 0.85      # Reject if similarity > this
    SIMILARITY_CHECK_WINDOW = 50     # Only check against last N entries
    
    def __init__(
        self,
        min_length: Optional[int] = None,
        max_length: Optional[int] = None,
        similarity_threshold: Optional[float] = None,
        enable_noise_filter: bool = True,
        enable_secret_redaction: bool = True,
        enable_deduplication: bool = True,
    ):
        """
        Initialize the sanitizer with optional custom configuration.
        
        Args:
            min_length: Minimum content length (default: 15)
            max_length: Maximum content length before truncation (default: 1000)
            similarity_threshold: Reject if similarity > threshold (default: 0.85)
            enable_noise_filter: Enable stage 1 noise filtering (default: True)
            enable_secret_redaction: Enable stage 2 secret redaction (default: True)
            enable_deduplication: Enable stage 3 deduplication (default: True)
        """
        self.min_length = min_length or self.MIN_CONTENT_LENGTH
        self.max_length = max_length or self.MAX_CONTENT_LENGTH
        self.similarity_threshold = similarity_threshold or self.SIMILARITY_THRESHOLD
        
        self.enable_noise_filter = enable_noise_filter
        self.enable_secret_redaction = enable_secret_redaction
        self.enable_deduplication = enable_deduplication
        
        # Compile regex patterns for performance
        self._noise_regexes = [re.compile(p, re.IGNORECASE) for p in self.NOISE_PATTERNS]
        self._secret_patterns = self._compile_secret_patterns()
        
        logger.debug(
            f"[MemorySanitizer] Initialized: "
            f"noise={enable_noise_filter}, "
            f"secrets={enable_secret_redaction}, "
            f"dedup={enable_deduplication}"
        )
    
    def _compile_secret_patterns(self) -> dict:
        """Compile secret detection regex patterns."""
        compiled = {}
        
        for category, patterns in self.SECRET_PATTERNS.items():
            compiled[category] = [re.compile(p, re.IGNORECASE) for p in patterns]
        
        return compiled
    
    def clean_entry(
        self,
        content: str,
        existing_contents: Optional[List[str]] = None
    ) -> Optional[str]:
        """
        Apply full three-stage quality gate to content.
        
        This is the main entry point for sanitizing memory entries.
        
        Args:
            content: Raw content to sanitize
            existing_contents: List of existing entry contents for dedup check
            
        Returns:
            Cleaned content string if entry passes all stages
            None if entry should be rejected
        """
        original_content = content
        
        # Stage 1: Noise Filter
        if self.enable_noise_filter:
            content, rejected = self._filter_noise(content)
            
            if rejected:
                logger.debug(
                    f"[Sanitizer] ❌ Stage 1 REJECTED (noise): "
                    f"{original_content[:60]}..."
                )
                return None
        
        # Length validation
        if len(content.strip()) < self.min_length:
            logger.debug(
                f"[Sanitizer] ❌ REJECTED (too short): "
                f"{len(content)} < {self.min_length} chars"
            )
            return None
        
        # Truncate if too long
        if len(content) > self.max_length:
            content = content[:self.max_length] + "..."
            logger.debug(f"[Sanitizer] ⚠️ Truncated to {self.max_length} chars")
        
        # Stage 2: Secret Redaction
        if self.enable_secret_redaction:
            content, redactions_made = self._redact_secrets(content)
            
            if redactions_made > 0:
                logger.debug(
                    f"[Sanitizer] 🔒 Redacted {redactions_made} secret(s)"
                )
        
        # Stage 3: Deduplication Check
        if self.enable_deduplication and existing_contents:
            is_duplicate, similarity = self._check_duplicates(
                content, 
                existing_contents
            )
            
            if is_duplicate:
                logger.debug(
                    f"[Sanitizer] ❌ Stage 3 REJECTED (duplicate): "
                    f"similarity={similarity:.2f}"
                )
                return None
        
        # Final cleanup
        content = self._final_cleanup(content)
        
        if not content or len(content.strip()) < self.min_length:
            return None
        
        logger.debug(
            f"[Sanitizer] ✓ PASSED ({len(content)} chars): "
            f"{content[:60]}..."
        )
        
        return content
    
    def _filter_noise(self, content: str) -> tuple:
        """
        Stage 1: Filter out noise and low-value content.
        
        Returns:
            Tuple of (cleaned_content, is_rejected)
        """
        cleaned = content.strip()
        
        # Check against noise patterns
        for pattern in self._noise_regexes:
            if pattern.match(cleaned):
                return cleaned, True
        
        # Remove excessive whitespace and newlines
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        cleaned = re.sub(r'[ \t]+', ' ', cleaned)
        
        # Remove markdown formatting artifacts that add no value
        cleaned = re.sub(r'^#+\s*', '', cleaned, flags=re.MULTILINE)
        
        return cleaned.strip(), False
    
    def _redact_secrets(self, content: str) -> tuple:
        """
        Stage 2: Detect and redact sensitive information.
        
        Returns:
            Tuple of (redacted_content, number_of_redactions)
        """
        redaction_count = 0
        redacted_content = content
        
        for category, patterns in self._secret_patterns.items():
            for pattern in patterns:
                matches = pattern.findall(redacted_content)
                
                for match in matches:
                    if isinstance(match, tuple):
                        # Pattern has groups - replace the captured group
                        secret_value = match[-1]  # Usually the last group is the value
                        
                        if len(secret_value) >= 8:  # Don't redact very short strings
                            redacted_content = redacted_content.replace(
                                secret_value,
                                self.REDACTION_PLACEHOLDER,
                                1
                            )
                            redaction_count += 1
                    else:
                        # Simple match
                        if len(match) >= 8:
                            redacted_content = redacted_content.replace(
                                match,
                                self.REDACTION_PLACEHOLDER,
                                1
                            )
                            redaction_count += 1
        
        return redacted_content, redaction_count
    
    def _check_duplicates(
        self,
        content: str,
        existing_contents: List[str]
    ) -> tuple:
        """
        Stage 3: Check for near-duplicate content.
        
        Uses sequence matching algorithm to compute similarity.
        Only checks against recent entries (configurable window).
        
        Returns:
            Tuple of (is_duplicate, max_similarity_found)
        """
        if not existing_contents:
            return False, 0.0
        
        # Limit check window for performance
        check_against = existing_contents[-self.SIMILARITY_CHECK_WINDOW:]
        
        max_similarity = 0.0
        
        for existing in check_against:
            similarity = self._compute_similarity(content, existing)
            
            if similarity > max_similarity:
                max_similarity = similarity
            
            if similarity > self.similarity_threshold:
                return True, similarity
        
        return False, max_similarity
    
    def _compute_similarity(self, text1: str, text2: str) -> float:
        """
        Compute similarity ratio between two texts.
        
        Uses SequenceMatcher from difflib for robust comparison.
        Normalizes whitespace and case before comparison.
        """
        # Normalize both texts
        norm1 = ' '.join(text1.lower().split())
        norm2 = ' '.join(text2.lower().split())
        
        # Quick length check (if one is much longer, they're probably not duplicates)
        len_ratio = min(len(norm1), len(norm2)) / max(len(norm1), len(norm2))
        
        if len_ratio < 0.5:
            return 0.0
        
        # Use SequenceMatcher for detailed comparison
        matcher = SequenceMatcher(None, norm1, norm2)
        return matcher.ratio()
    
    def _final_cleanup(self, content: str) -> str:
        """
        Final cleanup pass after all other stages.
        
        Ensures consistent formatting and removes any artifacts.
        """
        # Normalize line endings
        content = content.replace('\r\n', '\n').replace('\r', '\n')
        
        # Strip leading/trailing whitespace per line
        lines = [line.rstrip() for line in content.split('\n')]
        
        # Remove empty lines at start/end
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        
        return '\n'.join(lines)
    
    def validate_entry_quality(
        self,
        content: str,
        category: Optional[str] = None
    ) -> dict:
        """
        Validate an entry and return detailed quality report.
        
        Useful for debugging and understanding why entries are accepted/rejected.
        
        Returns:
            Dictionary with quality metrics and issues found
        """
        report = {
            "content": content[:100],
            "length": len(content),
            "passes": True,
            "issues": [],
            "scores": {}
        }
        
        # Check length
        if len(content) < self.min_length:
            report["passes"] = False
            report["issues"].append("too_short")
        
        if len(content) > self.max_length:
            report["issues"].append("too_long")
        
        # Check noise level
        noise_score = self._calculate_noise_score(content)
        report["scores"]["noise_level"] = noise_score
        
        if noise_score > 0.8:
            report["passes"] = False
            report["issues"].append("high_noise")
        
        # Check for secrets
        _, secret_count = self._redact_secrets(content)
        report["scores"]["secrets_detected"] = secret_count
        
        if secret_count > 0:
            report["issues"].append("contains_secrets")
        
        # Information density score
        info_density = self._calculate_information_density(content)
        report["scores"]["information_density"] = info_density
        
        if info_density < 0.3:
            report["passes"] = False
            report["issues"].append("low_information_density")
        
        return report
    
    def _calculate_noise_score(self, content: str) -> float:
        """
        Calculate noise score (0-1, higher = more noise).
        
        Based on pattern matches and content characteristics.
        """
        score = 0.0
        total_patterns = len(self.NOISE_PATTERNS)
        
        matched_patterns = sum(
            1 for p in self._noise_regexes 
            if p.match(content.strip())
        )
        
        if total_patterns > 0:
            score += (matched_patterns / total_patterns) * 0.6
        
        # Penalize very short content
        if len(content) < 30:
            score += 0.3
        
        # Penalize content with mostly common words
        words = content.lower().split()
        common_words = {'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 
                       'been', 'being', 'have', 'has', 'had', 'do', 'does',
                       'did', 'will', 'would', 'could', 'should', 'may',
                       'might', 'must', 'shall', 'can', 'need', 'dare',
                       'ought', 'used', 'to', 'of', 'in', 'for', 'on',
                       'with', 'at', 'by', 'from', 'as', 'into', 'through',
                       'during', 'before', 'after', 'above', 'below',
                       'between', 'out', 'off', 'over', 'under', 'again',
                       'further', 'then', 'once', 'here', 'there', 'when',
                       'where', 'why', 'how', 'all', 'each', 'few', 'more',
                       'most', 'other', 'some', 'such', 'no', 'nor', 'not',
                       'only', 'own', 'same', 'so', 'than', 'too', 'very'}
        
        if words:
            common_ratio = sum(1 for w in words if w in common_words) / len(words)
            score += common_ratio * 0.4
        
        return min(score, 1.0)
    
    def _calculate_information_density(self, content: str) -> float:
        """
        Calculate information density (0-1, higher = more informative).
        
        Based on unique word ratio, technical term presence, etc.
        """
        if not content or not content.strip():
            return 0.0
        
        words = content.split()
        if not words:
            return 0.0
        
        # Unique word ratio
        unique_words = set(w.lower() for w in words if len(w) > 2)
        uniqueness = len(unique_words) / len(words) if words else 0
        
        # Technical terms bonus
        tech_terms = {
            'function', 'class', 'method', 'variable', 'api', 'endpoint',
            'database', 'query', 'error', 'exception', 'bug', 'fix',
            'implementation', 'algorithm', 'pattern', 'architecture',
            'configuration', 'deployment', 'testing', 'debugging',
            'performance', 'optimization', 'security', 'authentication'
        }
        
        tech_term_count = sum(1 for w in unique_words if w in tech_terms)
        tech_bonus = min(tech_term_count / 10, 0.3)
        
        # Content length factor (longer tends to be more detailed)
        length_factor = min(len(content) / 200, 0.3)
        
        # Combine scores
        density = uniqueness * 0.4 + tech_bonus + length_factor
        
        return min(density, 1.0)
    
    def get_stats(self) -> dict:
        """Get sanitizer statistics and configuration."""
        return {
            "stages_enabled": {
                "noise_filter": self.enable_noise_filter,
                "secret_redaction": self.enable_secret_redaction,
                "deduplication": self.enable_deduplication,
            },
            "thresholds": {
                "min_content_length": self.min_length,
                "max_content_length": self.max_length,
                "similarity_threshold": self.similarity_threshold,
                "dedup_check_window": self.SIMILARITY_CHECK_WINDOW,
            },
            "patterns_loaded": {
                "noise_patterns": len(self.NOISE_PATTERNS),
                "secret_categories": len(self.SECRET_PATTERNS),
            }
        }
