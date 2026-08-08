"""
Failure-signature detection for the autonomous test agent.

This module implements repeated-failure detection to avoid wasting iterations
on the same unconverged failure. When the failure signature (pytest error output)
repeats without changing, the agent should change strategy or bail early.

Usage in agent.py:

    from failure_signature_detection import FailureSignatureTracker

    tracker = FailureSignatureTracker(max_repeated_signatures=3)
    
    # After each failed iteration:
    signature = extract_failure_signature(pytest_output)
    is_repeated, count = tracker.add_signature(signature)
    
    if is_repeated and count >= tracker.max_repeated_signatures:
        # Stop or change strategy
        return AgentStop(
            reason="repeated_failure_signature",
            message=f"Same failure seen {count} times; bailing early"
        )
"""

import hashlib
from typing import NamedTuple, Optional


class FailureSignature(NamedTuple):
    """
    Normalized failure signature extracted from pytest output.
    
    Attributes:
        test_name: Name of the first failing test
        error_type: Type of error (AssertionError, TypeError, etc.)
        error_line: First line of the error message
        hash: SHA256 of the normalized signature (for quick comparison)
    """
    test_name: str
    error_type: str
    error_line: str
    hash: str


class FailureSignatureTracker:
    """
    Tracks pytest failure signatures across iterations.
    
    When the same failure signature appears multiple times without changing,
    it indicates the model is not converging. This tracker detects that pattern
    and signals early stopping or strategy change.
    
    Attributes:
        max_repeated_signatures: Number of identical signatures before stopping
        history: List of (iteration, signature_hash) tuples
    """
    
    def __init__(self, max_repeated_signatures: int = 3):
        self.max_repeated_signatures = max_repeated_signatures
        self.history: list[tuple[int, str]] = []
    
    def add_signature(self, signature: FailureSignature, iteration: int) -> tuple[bool, int]:
        """
        Add a failure signature and check if it repeats.
        
        Args:
            signature: FailureSignature object from extract_failure_signature()
            iteration: Current iteration number (1-indexed)
        
        Returns:
            (is_repeated, count) where:
            - is_repeated: True if this signature has been seen before
            - count: Number of times this signature has appeared
        """
        sig_hash = signature.hash
        self.history.append((iteration, sig_hash))
        
        # Count occurrences of this signature
        count = sum(1 for _, h in self.history if h == sig_hash)
        is_repeated = count > 1
        
        return is_repeated, count
    
    def should_stop(self) -> bool:
        """
        Check if the most recent signature has repeated too many times.
        
        Returns True if stopping is recommended.
        """
        if len(self.history) < self.max_repeated_signatures:
            return False
        
        # Get the last signature hash
        recent_hashes = [h for _, h in self.history[-self.max_repeated_signatures:]]
        
        # If the last N signatures are all identical, stop
        return len(set(recent_hashes)) == 1
    
    def reset(self):
        """Clear history for a new run."""
        self.history.clear()


def extract_failure_signature(pytest_output: str) -> FailureSignature:
    """
    Extract a normalized failure signature from pytest output.
    
    Parses pytest failure output to extract:
    - The name of the first failing test
    - The error type (AssertionError, TypeError, etc.)
    - The first line of the error message
    
    Then hashes these components for quick comparison across iterations.
    
    Args:
        pytest_output: Raw pytest output (stdout + stderr)
    
    Returns:
        FailureSignature with test name, error type, error line, and hash
    
    Example:
        >>> output = '''
        ... FAILED test_example.py::test_something - AssertionError: x != y
        ... '''
        >>> sig = extract_failure_signature(output)
        >>> print(sig.test_name)
        test_something
        >>> print(sig.error_type)
        AssertionError
    """
    lines = pytest_output.strip().split('\n')
    
    test_name = "unknown"
    error_type = "unknown"
    error_line = "unknown"
    
    # Find the first FAILED line
    for line in lines:
        if "FAILED" in line:
            # Example: "FAILED test_example.py::test_something - AssertionError: x != y"
            parts = line.split("::")
            if len(parts) > 1:
                test_part = parts[-1]
                if " - " in test_part:
                    test_name, error_part = test_part.split(" - ", 1)
                    test_name = test_name.strip()
                    if ": " in error_part:
                        error_type, error_msg = error_part.split(": ", 1)
                        error_type = error_type.strip()
                        error_line = error_msg.strip()[:100]  # First 100 chars
                    else:
                        error_type = error_part.strip()
                break
    
    # Normalize: remove line numbers, assertion details that may change
    # Keep only the structural failure signature
    normalized = f"{test_name}::{error_type}::{error_line}"
    sig_hash = hashlib.sha256(normalized.encode()).hexdigest()
    
    return FailureSignature(
        test_name=test_name,
        error_type=error_type,
        error_line=error_line,
        hash=sig_hash
    )


# Integration example for agent.py:
#
# In the main loop, after pytest execution:
#
#     if pytest_returncode == 1:  # Tests failed
#         signature = extract_failure_signature(pytest_output)
#         is_repeated, count = tracker.add_signature(signature, iteration)
#         
#         if tracker.should_stop():
#             return AgentStop(
#                 reason="repeated_failure_signature",
#                 message=f"Failed with the same signature {count} times; stopping"
#             )
#         
#         # Continue to next iteration with the failure
#         feedback = format_feedback(pytest_output, reason="test_failure")
